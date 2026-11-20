"""Isolated, quote-aware shadow portfolios for strategy research.

No wallet transactions are sent. Entries/exits require executable Jupiter quotes;
missing data means skip, never substitute a chart mark. Each strategy has a separate
$1,000 virtual book and identical $10 test clips to prevent cross-contamination.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from gembul.core.models import Signal
from gembul.core.schemes import get_scheme
from gembul.db import Database
from gembul.venues.dex import DexRouter
from gembul.venues.dex.chains import get_chain

log = logging.getLogger(__name__)

PLANS = ("plan1", "plan2", "plan3")
ACTIVE_PLANS = ("plan1",)
INITIAL_EQUITY = 1000.0
TEST_CLIP_USD = 10.0
MAX_OPEN_PER_PLAN = 5
MAX_ROUNDTRIP_COST_PCT = 1.5
MAX_PRICE_IMPACT_PCT = 0.5
LOSS_COOLDOWN_MINUTES = 90
ANY_CLOSE_COOLDOWN_MINUTES = 45
GT_API = "https://api.geckoterminal.com/api/v2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_runs (
    scheme TEXT PRIMARY KEY,
    initial_equity REAL NOT NULL DEFAULT 1000,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS shadow_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheme TEXT NOT NULL,
    chain TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_usd REAL NOT NULL,
    opened_at TEXT NOT NULL DEFAULT (datetime('now')),
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    trailing_activation_pct REAL NOT NULL,
    trailing_distance_pct REAL NOT NULL,
    peak_price REAL NOT NULL,
    stage INTEGER NOT NULL DEFAULT 0,
    realized_pnl_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TEXT,
    exit_price REAL,
    pnl_usd REAL,
    pnl_pct REAL,
    signal_score REAL NOT NULL,
    signal_features TEXT NOT NULL,
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_open_mint
ON shadow_positions(scheme, chain, mint) WHERE status='open';
CREATE INDEX IF NOT EXISTS idx_shadow_scheme_status ON shadow_positions(scheme, status);
CREATE TABLE IF NOT EXISTS shadow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheme TEXT NOT NULL,
    chain TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    event TEXT NOT NULL,
    reason TEXT,
    payload TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _ema(vals: list[float], period: int) -> float:
    if not vals:
        return 0.0
    k = 2.0 / (period + 1)
    cur = vals[0]
    for v in vals[1:]:
        cur = v * k + cur * (1.0 - k)
    return cur


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_g = sum(max(d, 0.0) for d in deltas[:period]) / period
    avg_l = sum(max(-d, 0.0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l <= 1e-12:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _atr_pct(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1 or closes[-1] <= 0:
        return 3.0
    trs = [
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(1, len(closes))
    ]
    atr = sum(trs[-period:]) / period
    return (atr / closes[-1]) * 100.0


def analyze_ohlcv(candles: list[list[float]]) -> dict[str, float] | None:
    """Compute 5m trend, RSI, ATR%, extension, and volume acceleration from raw OHLCV."""
    if len(candles) < 22:
        return None
    ordered = sorted(candles, key=lambda x: x[0])
    highs = [float(c[2]) for c in ordered]
    lows = [float(c[3]) for c in ordered]
    closes = [float(c[4]) for c in ordered]
    vols = [float(c[5]) for c in ordered]
    last_close = closes[-1]
    if last_close <= 0:
        return None

    e9 = _ema(closes, 9)
    e20 = _ema(closes, 20)
    r14 = _rsi(closes, 14)
    atrp = _atr_pct(highs, lows, closes, 14)
    ext_pct = ((last_close - e20) / e20 * 100.0) if e20 > 0 else 0.0

    # Compare recent 2 completed bars vs preceding 12-bar baseline
    v_recent = sum(vols[-3:-1]) / 2.0 if len(vols) >= 4 else vols[-1]
    base_slice = vols[-15:-3] if len(vols) >= 15 else vols[:-2]
    v_base = sum(base_slice) / max(len(base_slice), 1)
    vol_ratio = v_recent / max(v_base, 1e-9)

    return {
        "ema9": e9,
        "ema20": e20,
        "close": last_close,
        "rsi14": round(r14, 2),
        "atr_pct": round(atrp, 2),
        "ext_pct": round(ext_pct, 2),
        "vol_ratio": round(vol_ratio, 2),
        "trend_up": 1.0 if e9 >= e20 * 0.995 and last_close >= e20 * 0.99 else 0.0,
    }


class ShadowBook:
    def __init__(self, db: Database, dex: DexRouter, scanner: Any, notifier: Any = None) -> None:
        self.db = db
        self.dex = dex
        self.scanner = scanner
        self.notifier = notifier
        self._last_entry_ts: dict[str, float] = {}
        self._ohlcv_cache: dict[str, tuple[float, dict[str, float] | None]] = {}
        self._http = httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": "gembul/1.0", "Accept": "application/json"}
        )

    async def _notify(self, text: str, **kw: Any) -> None:
        if self.notifier:
            try:
                await self.notifier(text, **kw)
            except Exception as exc:
                log.debug("shadow notify gagal: %s", exc)

    async def init(self) -> None:
        conn = await self.db.connect()
        await conn.executescript(SCHEMA)
        for plan in PLANS:
            en = 1 if plan in ACTIVE_PLANS else 0
            await self.db.execute(
                "INSERT INTO shadow_runs(scheme,initial_equity,enabled) VALUES(?,?,?) "
                "ON CONFLICT(scheme) DO UPDATE SET enabled=excluded.enabled",
                (plan, INITIAL_EQUITY, en),
            )
        await conn.commit()

    async def close(self) -> None:
        await self._http.aclose()

    async def _attach_1m_confirmation(self, chain: str, mint: str, metrics: dict[str, float]) -> None:
        """Attach optional 1m scalping momentum check without blocking on 1m endpoint gaps."""
        try:
            r1 = await self._http.get(
                "https://dquery.sintral.io/u-kline/v1/k-line/candles",
                params={"platform": chain, "address": mint, "interval": "1min", "limit": 20},
                headers={"Accept-Encoding": "identity", "User-Agent": "binance-web3/2.0 (Skill)"},
            )
            if r1.status_code != 200:
                return
            rows = (r1.json() or {}).get("data") or []
            candles = sorted(
                [[c[5], float(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4])] for c in rows if len(c) >= 6],
                key=lambda x: x[0],
            )
            if len(candles) < 12:
                return
            closes = [c[4] for c in candles if c[4] > 0]
            if len(closes) < 12:
                return
            e9_1m = _ema(closes, 9)
            r14_1m = _rsi(closes, 10)
            last_1m = closes[-1]
            metrics["rsi_1m"] = round(r14_1m, 2)
            metrics["ema9_1m_ok"] = 1.0 if (last_1m >= e9_1m * 0.992 and 38.0 <= r14_1m <= 78.0) else 0.0
        except Exception:
            pass

    async def _fetch_5m_indicators(self, chain: str, mint: str, pool_addr: str | None) -> dict[str, float] | None:
        cache_key = mint or (pool_addr or "")
        if not cache_key:
            return None
        now = time.time()
        cached = self._ohlcv_cache.get(cache_key)
        if cached and (now - cached[0]) < 60.0:
            return cached[1]

        # 1. Primary: Binance Web3 / dquery kline by mint (fast, high rate limit)
        try:
            r_k = await self._http.get(
                "https://dquery.sintral.io/u-kline/v1/k-line/candles",
                params={"platform": chain, "address": mint, "interval": "5min", "limit": 40},
                headers={"Accept-Encoding": "identity", "User-Agent": "binance-web3/2.0 (Skill)"},
            )
            if r_k.status_code == 200:
                rows = (r_k.json() or {}).get("data") or []
                # Convert [o, h, l, c, v, ts_ms, count] -> [ts_ms, o, h, l, c, v]
                converted = [[c[5], c[0], c[1], c[2], c[3], c[4]] for c in rows if len(c) >= 6]
                metrics = analyze_ohlcv(converted)
                if metrics is not None:
                    await self._attach_1m_confirmation(chain, mint, metrics)
                    self._ohlcv_cache[cache_key] = (now, metrics)
                    return metrics
        except Exception as exc:
            log.debug("kline 5m fetch gagal %s: %s", mint[:8], exc)

        # 2. Fallback: GeckoTerminal pool OHLCV
        if not pool_addr:
            return None
        try:
            r = await self._http.get(
                f"{GT_API}/networks/{chain}/pools/{pool_addr}/ohlcv/minute",
                params={"aggregate": 5, "limit": 40},
            )
            if r.status_code != 200:
                self._ohlcv_cache[cache_key] = (now, None)
                return None
            raw = ((r.json() or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
            metrics = analyze_ohlcv(raw)
            self._ohlcv_cache[cache_key] = (now, metrics)
            return metrics
        except Exception as exc:
            log.debug("5m ohlcv fetch gagal %s: %s", pool_addr[:8], exc)
            return None

    async def record_skip(self, plan: str, sig: Signal, reason: str, payload: dict | None = None) -> None:
        await self.db.execute(
            "INSERT INTO shadow_events(scheme,chain,mint,symbol,event,reason,payload) VALUES(?,?,?,?,?,?,?)",
            (plan, sig.chain or "solana", sig.address or "", sig.symbol, "skip", reason,
             json.dumps(payload or {}, default=str)[:3000]),
        )

    async def consider(self, plan: str, sig: Signal) -> bool:
        """Apply microstructure, 1m/5m OHLCV, safety, cooldown, and executable round-trip quote gates."""
        if plan not in ACTIVE_PLANS or sig.chain != "solana" or not sig.address:
            return False

        # Cap at max 1 new entry per plan per 2-minute scalping scan cycle (without blocking skips).
        now = time.time()
        if now - self._last_entry_ts.get(plan, 0.0) < 110.0:
            return False

        features = sig.features or {}
        liq = float(features.get("liquidity_usd") or 0)
        vol24 = float(features.get("volume_24h") or 0)
        vol1 = float(features.get("volume_1h") or 0)
        age = float(features.get("age_minutes") or 0)
        chg5 = float(features.get("chg_5m") or 0)
        chg1 = float(features.get("chg_1h") or 0)
        chg6 = float(features.get("chg_6h") or 0)
        buys = float(features.get("buys_1h") or 0)
        sells = float(features.get("sells_1h") or 0)
        buys5 = float(features.get("buys_5m") or 0)
        sells5 = float(features.get("sells_5m") or 0)
        fdv = float(features.get("fdv") or 0)
        smart_inflow = features.get("smart_money_inflow")
        buy_ratio = buys / max(sells, 1.0)
        turnover = vol24 / liq if liq > 0 else 0.0

        # Check open slots first to avoid unnecessary API calls
        existing = await self.db.fetchone(
            "SELECT id FROM shadow_positions WHERE scheme=? AND chain=? AND mint=? AND status='open'",
            (plan, sig.chain, sig.address),
        )
        if existing:
            return False
        count_row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM shadow_positions WHERE scheme=? AND status='open'", (plan,)
        )
        if int(count_row["n"] or 0) >= MAX_OPEN_PER_PLAN:
            await self.record_skip(plan, sig, "shadow_slots_full")
            return False

        # Persistent per-mint cooldown (90m after closed loss, 45m after any close)
        recent_closed = await self.db.fetchone(
            "SELECT pnl_usd, closed_at FROM shadow_positions "
            "WHERE scheme=? AND chain=? AND mint=? AND status='closed' "
            "ORDER BY id DESC LIMIT 1",
            (plan, sig.chain, sig.address),
        )
        if recent_closed and recent_closed["closed_at"]:
            was_loss = float(recent_closed["pnl_usd"] or 0.0) <= 0.0
            cd_min = LOSS_COOLDOWN_MINUTES if was_loss else ANY_CLOSE_COOLDOWN_MINUTES
            hit = await self.db.fetchone(
                f"SELECT 1 FROM shadow_positions WHERE scheme=? AND chain=? AND mint=? "
                f"AND status='closed' AND closed_at > datetime('now', '-{cd_min} minutes') LIMIT 1",
                (plan, sig.chain, sig.address),
            )
            if hit:
                await self.record_skip(plan, sig, f"mint_cooldown_{cd_min}m")
                return False

        # Common market-quality & anti-knife / anti-top-tick guards
        if age < 30:
            await self.record_skip(plan, sig, f"pool_too_new:{age:.1f}m")
            return False
        deep_pool_ok = liq >= 250_000 and vol24 >= 250_000 and turnover >= 0.75
        if vol24 <= 0 or (turnover < 1.2 and not deep_pool_ok):
            await self.record_skip(plan, sig, "low_turnover", {"vol24": vol24, "liq": liq, "turn": round(turnover, 2)})
            return False
        if vol1 > 0 and vol1 < 10_000:
            await self.record_skip(plan, sig, f"low_1h_volume:{vol1:.0f}")
            return False
        if (buys + sells) < 20 or buy_ratio < 1.04:
            await self.record_skip(plan, sig, "weak_1h_buy_pressure", {"buys": buys, "sells": sells, "buy_ratio": round(buy_ratio, 2)})
            return False
        if (buys5 + sells5) >= 5 and (buys5 < sells5 * 0.80 or chg5 < -3.0 or chg5 > 12.0):
            await self.record_skip(plan, sig, "weak_or_extended_5m", {"buys5": buys5, "sells5": sells5, "chg5": chg5})
            return False
        if chg6 < -15.0:
            await self.record_skip(plan, sig, f"falling_knife_6h:{chg6:.1f}%")
            return False
        if smart_inflow is not None and float(smart_inflow) < -400.0:
            await self.record_skip(plan, sig, f"smart_money_outflow:{float(smart_inflow):.0f}")
            return False
        if fdv > 0 and liq > 0 and (fdv / liq) > 120.0:
            await self.record_skip(plan, sig, f"fdv_liq_ratio_high:{fdv/liq:.1f}")
            return False

        # Per-plan selection rules (Scalping primary)
        if plan == "plan1":
            if liq < 60_000 or not (-5.0 <= chg1 <= 35.0) or chg6 < -12.0 or buy_ratio < 1.05:
                await self.record_skip(plan, sig, "scalp_conditions_not_met", {"liq": liq, "chg1": chg1, "chg6": chg6, "buy_ratio": round(buy_ratio, 2)})
                return False
        elif plan == "plan2":
            if liq < 120_000 or vol24 < 100_000 or turnover < 2.0 or buy_ratio < 1.25 or not (2.5 <= chg1 <= 28.0):
                await self.record_skip(plan, sig, "breakout_conditions_not_met", {"liq": liq, "vol24": vol24, "turn": round(turnover, 2), "buy_ratio": round(buy_ratio, 2), "chg1": chg1})
                return False
        elif plan == "plan3":
            if not (30.0 <= age <= 360.0) or liq < 60_000 or buy_ratio < 1.20 or not (5.0 <= chg1 <= 35.0):
                await self.record_skip(plan, sig, "sniper_conditions_not_met", {"age": age, "liq": liq, "buy_ratio": round(buy_ratio, 2), "chg1": chg1})
                return False

        # 5m + optional 1m OHLCV technical confirmation
        ohlcv = await self._fetch_5m_indicators(sig.chain, sig.address, features.get("pool"))
        if ohlcv is None:
            await self.record_skip(plan, sig, "ohlcv_unavailable")
            return False
        if ohlcv["rsi14"] < 40.0 or ohlcv["rsi14"] > 75.0:
            await self.record_skip(plan, sig, "ohlcv_rsi_out_of_range", ohlcv)
            return False
        if ohlcv["close"] < ohlcv["ema20"] * 0.985:
            await self.record_skip(plan, sig, "ohlcv_below_ema20", ohlcv)
            return False
        if ohlcv["ext_pct"] > 15.0:
            await self.record_skip(plan, sig, "ohlcv_overextended", ohlcv)
            return False
        if ohlcv["vol_ratio"] < 0.45:
            await self.record_skip(plan, sig, "ohlcv_volume_drying", ohlcv)
            return False
        if chg1 < 0 and ohlcv.get("trend_up", 1.0) < 0.5 and ohlcv["rsi14"] < 46.0:
            await self.record_skip(plan, sig, "ohlcv_pullback_not_stabilized", ohlcv)
            return False
        if ohlcv.get("ema9_1m_ok", 1.0) < 0.5:
            await self.record_skip(plan, sig, "ohlcv_1m_weak_or_overbought", ohlcv)
            return False

        # Safety audit
        try:
            report = await self.scanner.scan(get_chain(sig.chain), sig.address)
        except Exception as exc:
            await self.record_skip(plan, sig, f"safety_api_unavailable:{type(exc).__name__}")
            return False
        if not report.ok:
            await self.record_skip(plan, sig, "safety_rejected", {"score": report.score, "reasons": report.reasons})
            return False

        # Executable Jupiter round-trip quote & spread guard
        try:
            venue = self.dex.venue(sig.chain)
            decimals = await venue.decimals(sig.address)
            raw_in = int(TEST_CLIP_USD * 1_000_000)
            buy_quote = await venue.quote(get_chain(sig.chain).usdc, sig.address, raw_in, slippage_bps=100)
            out_raw = int(buy_quote.get("outAmount") or 0)
            if out_raw <= 0:
                raise ValueError("zero_buy_output")
            qty = out_raw / (10 ** decimals)
            entry_spent = int(buy_quote.get("inAmount") or raw_in) / 1_000_000
            entry_price = entry_spent / qty
            price_impact_pct = float(buy_quote.get("priceImpactPct") or 0) * 100.0

            sell_quote = await venue.quote(sig.address, get_chain(sig.chain).usdc, out_raw, slippage_bps=100)
            exit_usd = int(sell_quote.get("outAmount") or 0) / 1_000_000
            if exit_usd <= 0:
                raise ValueError("zero_sell_output")
            roundtrip_cost = (entry_spent - exit_usd) / max(entry_spent, 1e-9) * 100.0
            if price_impact_pct > MAX_PRICE_IMPACT_PCT or roundtrip_cost > MAX_ROUNDTRIP_COST_PCT:
                await self.record_skip(
                    plan, sig, "roundtrip_cost_too_high",
                    {"price_impact_pct": round(price_impact_pct, 3), "roundtrip_cost_pct": round(roundtrip_cost, 3)},
                )
                return False
        except Exception as exc:
            await self.record_skip(plan, sig, f"executable_quote_unavailable:{type(exc).__name__}")
            return False

        cfg = get_scheme(plan)
        sl_pct = cfg.stop_loss_pct
        if plan == "plan1" and ohlcv is not None:
            # Tight ATR-scaled scalping stop (clears <=1.5% roundtrip + normal 5m noise)
            sl_pct = max(5.5, min(7.5, round(ohlcv["atr_pct"] * 2.0, 2)))

        stop = entry_price * (1.0 - sl_pct / 100.0)
        take = entry_price * (1.0 + cfg.take_profit_pct / 100.0)
        meta_obj = {
            "entry_quote": buy_quote,
            "initial_entry_usd": entry_spent,
            "roundtrip_cost_pct": round(roundtrip_cost, 4),
            "price_impact_pct": round(price_impact_pct, 4),
            "sl_pct": sl_pct,
            "decimals": decimals,
            "ohlcv": ohlcv,
        }
        try:
            pos_id = await self.db.execute(
                "INSERT INTO shadow_positions(scheme,chain,mint,symbol,qty,entry_price,entry_usd,stop_loss,take_profit,"
                "trailing_activation_pct,trailing_distance_pct,peak_price,signal_score,signal_features,meta) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan, sig.chain, sig.address, sig.symbol, qty, entry_price, entry_spent, stop, take,
                 cfg.trailing_activation_pct, cfg.trailing_distance_pct, entry_price, sig.score,
                 json.dumps(features, default=str)[:4000],
                 json.dumps(meta_obj, default=str)[:4000]),
            )
        except Exception as exc:
            log.debug("shadow insert failed: %s", exc)
            return False

        self._last_entry_ts[plan] = now
        await self.db.execute(
            "INSERT INTO shadow_events(scheme,chain,mint,symbol,event,reason,payload) VALUES(?,?,?,?,?,?,?)",
            (plan, sig.chain, sig.address, sig.symbol, "entry", "shadow_executable_quote",
             json.dumps({
                 "entry_usd": entry_spent, "qty": qty, "entry_price": entry_price,
                 "sl_pct": sl_pct, "price_impact_pct": round(price_impact_pct, 4),
                 "roundtrip_cost_pct": round(roundtrip_cost, 4), "ohlcv": ohlcv,
             })),
        )
        log.info("SHADOW OPEN [%s] %s stake=$%.2f entry=$%.10f sl=-%.1f%% rt=%.2f%%",
                 plan, sig.symbol, entry_spent, entry_price, sl_pct, roundtrip_cost)
        await self._notify(
            f"🤖 <b>AUTO-TRADE SALDO UJI (OPEN)</b>\n"
            f"📌 <b>{cfg.badge}</b> · <b>{sig.symbol}</b> (<code>#{pos_id}</code>)\n"
            f"💵 Stake Uji: <b>${entry_spent:.2f}</b> ({qty:,.2f} token)\n"
            f"🎯 Entry Quote: <b>${entry_price:.8f}</b> · Spread RT: <b>{roundtrip_cost:.2f}%</b>\n"
            f"🛑 Stop Loss: <b>-{sl_pct:.1f}%</b> · 🎯 Target: <b>+{cfg.take_profit_pct:.0f}%</b>\n"
            f"CA: <code>{sig.address}</code>\n"
            f"🔗 <a href='https://gmgn.ai/sol/token/{sig.address}'>Lihat di GMGN</a>"
        )
        return True

    async def mark_and_exit(self) -> None:
        """Update virtual portfolios only when fresh full-size quotes are available."""
        rows = await self.db.fetchall("SELECT * FROM shadow_positions WHERE status='open'")
        now_utc = datetime.now(timezone.utc)
        for row in rows:
            plan, chain, mint = row["scheme"], row["chain"], row["mint"]
            qty, entry = float(row["qty"]), float(row["entry_price"])
            try:
                meta = json.loads(row["meta"] or "{}")
                decimals = int(meta.get("decimals") or 0)
                if decimals <= 0:
                    decimals = await self.dex.venue(chain).decimals(mint)
                    meta["decimals"] = decimals
                raw_qty = int(round(qty * (10 ** decimals)))
                if raw_qty <= 0:
                    continue
                quote = await self.dex.venue(chain).quote(mint, get_chain(chain).usdc, raw_qty, slippage_bps=100)
                exit_usd = int(quote.get("outAmount") or 0) / 1e6
                if exit_usd <= 0:
                    continue
                mark = exit_usd / qty
                peak = max(float(row["peak_price"] or entry), mark)
                pnl_pct = (mark - entry) / entry * 100.0 if entry > 0 else 0.0
                await self.db.execute("UPDATE shadow_positions SET peak_price=? WHERE id=?", (peak, row["id"]))

                cfg = get_scheme(plan)
                stop_price = float(row["stop_loss"] or (entry * (1.0 - cfg.stop_loss_pct / 100.0)))
                reason = None
                fraction = 1.0
                stage = int(row["stage"])
                new_stop = stop_price

                if plan == "plan1":
                    if mark <= stop_price:
                        reason = "stop_loss_be" if stage >= 1 else "stop_loss"
                    elif stage == 0 and mark >= entry * 1.10:
                        reason, fraction, stage = "tp1_10pct_sell50", 0.50, 1
                        new_stop = max(stop_price, entry * 1.015)
                    elif stage == 1 and mark >= entry * 1.20:
                        # 25% of original position = 50% of remaining 50%
                        reason, fraction, stage = "tp2_20pct_sell25", 0.50, 2
                        new_stop = max(stop_price, entry * 1.10)
                    elif stage >= 2 and mark <= peak * 0.95:
                        reason = "runner_trail_5pct"
                elif plan == "plan3":
                    if mark <= stop_price:
                        reason = "stop_loss_be" if stage >= 1 else "stop_loss"
                    elif stage == 0 and mark >= entry * 1.25:
                        reason, fraction, stage = "tp1_25pct_sell50", 0.50, 1
                        new_stop = max(stop_price, entry * 1.015)
                    elif stage >= 1 and mark <= peak * (1.0 - cfg.trailing_distance_pct / 100.0):
                        reason = "runner_trail_7pct"
                else:
                    if mark <= stop_price:
                        reason = "stop_loss"
                    elif mark >= float(row["take_profit"] or (entry * (1.0 + cfg.take_profit_pct / 100.0))):
                        reason = "take_profit"
                    elif (
                        peak >= entry * (1.0 + cfg.trailing_activation_pct / 100.0)
                        and mark <= peak * (1.0 - cfg.trailing_distance_pct / 100.0)
                    ):
                        reason = "trailing_stop"

                # Time-based stale exit for flat/chop trades (stage 0 only)
                if not reason and stage == 0 and cfg.max_hold_minutes > 0 and row["opened_at"]:
                    try:
                        opened_dt = datetime.fromisoformat(str(row["opened_at"]).replace(" ", "T")).replace(tzinfo=timezone.utc)
                        held_min = (now_utc - opened_dt).total_seconds() / 60.0
                        if held_min >= cfg.max_hold_minutes and peak < entry * 1.04 and mark < entry * 1.015:
                            reason = f"time_stop_stale_{int(cfg.max_hold_minutes)}m"
                    except Exception:
                        pass

                meta["last_quote_at"] = time.time()
                meta["last_quote_out_usd"] = exit_usd
                if not reason:
                    await self.db.execute("UPDATE shadow_positions SET meta=? WHERE id=?", (json.dumps(meta), row["id"]))
                    continue

                close_qty = qty * fraction
                if fraction < 0.999:
                    qpart = await self.dex.venue(chain).quote(
                        mint, get_chain(chain).usdc, int(round(close_qty * (10 ** decimals))), slippage_bps=100
                    )
                    close_usd = int(qpart.get("outAmount") or 0) / 1e6
                    if close_usd <= 0:
                        continue
                else:
                    close_usd = exit_usd

                cost_basis = float(row["entry_usd"]) * fraction
                realized_delta = close_usd - cost_basis
                remaining_qty = qty - close_qty
                new_realized = float(row["realized_pnl_usd"] or 0.0) + realized_delta
                initial_entry_usd = float(meta.get("initial_entry_usd") or TEST_CLIP_USD)
                meta["last_exit_reason"] = reason
                meta["last_exit_quote"] = quote

                if fraction >= 0.999:
                    true_roi_pct = round((new_realized / max(initial_entry_usd, 1e-9)) * 100.0, 4)
                    await self.db.execute(
                        "UPDATE shadow_positions SET status='closed',closed_at=datetime('now'),exit_price=?,pnl_usd=?,pnl_pct=?,realized_pnl_usd=?,stage=?,meta=? WHERE id=?",
                        (mark, round(new_realized, 6), true_roi_pct, new_realized, stage, json.dumps(meta, default=str), row["id"]),
                    )
                else:
                    await self.db.execute(
                        "UPDATE shadow_positions SET qty=?,entry_usd=?,realized_pnl_usd=?,stage=?,stop_loss=?,meta=? WHERE id=?",
                        (remaining_qty, max(0.0, float(row["entry_usd"]) - cost_basis), new_realized, stage,
                         new_stop, json.dumps(meta, default=str), row["id"]),
                    )

                await self.db.execute(
                    "INSERT INTO shadow_events(scheme,chain,mint,symbol,event,reason,payload) VALUES(?,?,?,?,?,?,?)",
                    (plan, chain, mint, row["symbol"], "exit", reason,
                     json.dumps({"fraction": fraction, "close_usd": close_usd, "realized_delta": realized_delta, "mark_pct": pnl_pct})),
                )
                rep = await self.get_report(plan)
                icon = "🟢" if realized_delta >= 0 else "🔴"
                kind_lbl = "PARTIAL TP" if fraction < 0.999 else "CLOSED"
                tot_roi = (new_realized / max(initial_entry_usd, 1e-9)) * 100.0
                await self._notify(
                    f"{icon} <b>AUTO-TRADE SALDO UJI ({kind_lbl})</b>\n"
                    f"📌 <b>{cfg.badge}</b> · <b>{row['symbol']}</b> (<code>#{row['id']}</code>)\n"
                    f"🏷️ Pemicu: <i>{reason}</i> ({fraction * 100:.0f}%)\n"
                    f"💵 Realized: <b>${realized_delta:+.2f}</b> (Total Trade: <b>{tot_roi:+.1f}%</b>)\n"
                    f"🏦 Saldo {plan.upper()}: <b>${rep['current_equity']:,.2f}</b>"
                )
            except Exception as exc:
                log.debug("shadow quote/exit failed for %s %s: %s", plan, row["symbol"], exc)

    async def close_manual(self, pos_id: int) -> dict[str, Any] | None:
        """Manually close an open shadow position using a live executable quote."""
        row = await self.db.fetchone("SELECT * FROM shadow_positions WHERE id=? AND status='open'", (pos_id,))
        if not row:
            return None
        plan, chain, mint = row["scheme"], row["chain"], row["mint"]
        qty, entry = float(row["qty"]), float(row["entry_price"])
        meta = json.loads(row["meta"] or "{}")
        decimals = int(meta.get("decimals") or 0)
        if decimals <= 0:
            decimals = await self.dex.venue(chain).decimals(mint)
            meta["decimals"] = decimals
        raw_qty = int(round(qty * (10 ** decimals)))
        quote = await self.dex.venue(chain).quote(mint, get_chain(chain).usdc, raw_qty, slippage_bps=150)
        close_usd = int(quote.get("outAmount") or 0) / 1e6
        if close_usd <= 0:
            raise RuntimeError("quote jual manual tidak tersedia")
        mark = close_usd / qty
        cost_basis = float(row["entry_usd"])
        realized_delta = close_usd - cost_basis
        new_realized = float(row["realized_pnl_usd"] or 0.0) + realized_delta
        initial_entry_usd = float(meta.get("initial_entry_usd") or TEST_CLIP_USD)
        true_roi_pct = round((new_realized / max(initial_entry_usd, 1e-9)) * 100.0, 4)
        meta["last_exit_reason"] = "manual_telegram"
        meta["last_quote_out_usd"] = close_usd
        meta["last_exit_quote"] = quote
        await self.db.execute(
            "UPDATE shadow_positions SET status='closed',closed_at=datetime('now'),exit_price=?,pnl_usd=?,pnl_pct=?,realized_pnl_usd=?,meta=? WHERE id=?",
            (mark, round(new_realized, 6), true_roi_pct, new_realized, json.dumps(meta, default=str), pos_id),
        )
        await self.db.execute(
            "INSERT INTO shadow_events(scheme,chain,mint,symbol,event,reason,payload) VALUES(?,?,?,?,?,?,?)",
            (plan, chain, mint, row["symbol"], "exit", "manual_telegram",
             json.dumps({"fraction": 1.0, "close_usd": close_usd, "realized_delta": realized_delta})),
        )
        return {"id": pos_id, "symbol": row["symbol"], "scheme": plan, "pnl_usd": round(new_realized, 4), "pnl_pct": true_roi_pct}

    async def get_report(self, plan: str) -> dict[str, Any]:
        run = await self.db.fetchone("SELECT * FROM shadow_runs WHERE scheme=?", (plan,))
        rows = await self.db.fetchall("SELECT * FROM shadow_positions WHERE scheme=?", (plan,))
        closed = [r for r in rows if r["status"] == "closed"]
        opened = [r for r in rows if r["status"] == "open"]
        realized = sum(float(r["realized_pnl_usd"] or 0) for r in rows)
        unrealized = 0.0
        for r in opened:
            try:
                meta = json.loads(r["meta"] or "{}")
                liquidation = float(meta.get("last_quote_out_usd") or 0.0)
                remaining_basis = float(r["entry_usd"] or 0.0)
                if liquidation > 0:
                    unrealized += liquidation - remaining_basis
            except Exception:
                pass
        wins = sum(1 for r in closed if float(r["pnl_usd"] or 0) > 0)
        losses = len(closed) - wins
        total = len(closed)
        win_rate = round(100 * wins / total, 2) if total else 0.0
        avg_pct = round(sum(float(r["pnl_pct"] or 0) for r in closed) / total, 4) if total else 0.0
        pnl_total = round(realized + unrealized, 4)
        initial = float(run["initial_equity"]) if run else INITIAL_EQUITY
        best = max(closed, key=lambda r: float(r["pnl_usd"] or 0), default=None)
        worst = min(closed, key=lambda r: float(r["pnl_usd"] or 0), default=None)
        return {
            "scheme": plan,
            "enabled": plan in ACTIVE_PLANS,
            "period_key": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "initial_equity": initial,
            "total_pnl_usd": pnl_total,
            "realized_pnl_usd": round(realized, 4),
            "unrealized_usd": round(unrealized, 4),
            "equity": round(initial + pnl_total, 4),
            "current_equity": round(initial + pnl_total, 4),
            "closed_trades": total,
            "total_trades": total,
            "open_positions": len(opened),
            "open_count": len(opened),
            "wins": wins,
            "win_trades": wins,
            "losses": losses,
            "loss_trades": losses,
            "win_rate": win_rate,
            "avg_pnl_pct": avg_pct,
            "best_trade": f"{best['symbol']} ({best['pnl_pct']:+.1f}%)" if best else "-",
            "worst_trade": f"{worst['symbol']} ({worst['pnl_pct']:+.1f}%)" if worst else "-",
            "trades": [{"id": r["id"], "symbol": r["symbol"], "scheme": r["scheme"], "pnl_usd": r["pnl_usd"], "pnl_pct": r["pnl_pct"]} for r in closed],
        }

    async def compare(self) -> list[dict[str, Any]]:
        return [await self.get_report(p) for p in PLANS]
