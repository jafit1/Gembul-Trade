"""The loop. Everything else is a component this file wires together."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

from gembul.core.models import Fill, OrderIntent, Side, Signal, Venue
from gembul.core.risk import RiskEngine, _row_to_position
from gembul.core.schemes import SCHEMES, get_scheme
from gembul.core.shadow import ACTIVE_PLANS, ShadowBook
from gembul.db import Database
from gembul.llm.client import LlmClient
from gembul.llm.judge import AiJudge
from gembul.memory.journal import JournalManager
from gembul.memory.reflect import Reflector
from gembul.memory.store import MemoryStore
from gembul.safety.token_scan import TokenScanner
from gembul.settings import Settings
from gembul.signals.meme import MemeScanner
from gembul.signals.whale import WhaleTracker
from gembul.venues.cex import CexVenue
from gembul.venues.dex import DexRouter
from gembul.venues.dex.chains import get_chain
from gembul.venues.freqtrade_bridge import FreqtradeBridge

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.db = Database(settings.db_path)
        self.memory = MemoryStore(self.db)
        self.llm = LlmClient(settings.llm)
        self.judge = AiJudge(self.llm)
        self.risk = RiskEngine(settings.risk, self.db)
        self.reflector = Reflector(self.db, self.memory, self.llm)
        self.scanner = TokenScanner(self.db, settings.meme)
        self.meme = MemeScanner(settings)
        self.whale = WhaleTracker(self.db, settings)
        self.cex = CexVenue(settings)
        self.dex = DexRouter(settings)
        self.shadow = ShadowBook(self.db, self.dex, self.scanner)
        self.journal = JournalManager(self.db, self.memory, self.llm, shadow=self.shadow)
        self._price_cache: dict[tuple[str, str], tuple[float, float]] = {}
        self._last_shadow_candidate_scan = 0.0
        self.ft = FreqtradeBridge(settings)
        self.notifier: Any = None

        self.equity_usd: float = settings.paper_equity_usd
        self._running = False
        self._last_reflect = 0.0
        self._last_equity = 0.0
        self._last_whale_refresh_ts = time.time() - 3400.0
        self._startup_notified = False
        self.stats: dict[str, Any] = {}

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.shadow.notifier = self.notifier
        await self.db.connect()
        await self.whale.init_db()
        await self.shadow.init()
        await self.cex.start()
        await self.ft.ping()
        start = await self.db.kv_get("day_start_equity")
        if start is None:
            await self.db.kv_set("day_start_equity", self.equity_usd)
        ks = await self.db.kv_get("kill_switch")
        if ks and ks.get("until"):
            try:
                until = datetime.fromisoformat(ks["until"])
                if datetime.now(timezone.utc) < until:
                    self.risk._halted_until = until
                    self.risk._halt_reason = ks.get("reason", "")
            except Exception:
                pass
        log.info("engine siap | mode=%s live=%s equity=$%.2f",
                 self.s.mode, self.s.real_trading_enabled, self.equity_usd)
        if self.notifier and not self._startup_notified:
            self._startup_notified = True
            try:
                comp = await self.journal.compare_three_plans(period="daily")
                p1 = comp.get("plan1", {})
                ft_online = self.ft.online
                await self.notifier(
                    f"⚡️ <b>GEMBUL AUTO-TRADE SCALPING AKTIF (SALDO UJI)</b>\n"
                    f"🟣 <b>DEX Solana Scalping (1m/5m · Scan 2m):</b> "
                    f"<b>${p1.get('current_equity', 1000):,.2f}</b> ({p1.get('open_count', 0)} open · stake $10/slot)\n"
                    f"🎯 Rule Scalp: SL ATR -5.5..-7.5% · TP1 +10% (jual 50%, BE +1.5%) · TP2 +20% · Time-stop 45m\n"
                    f"⚙️ <b>CEX Freqtrade (1h GembulSwing):</b> {'🟢 Online' if ft_online else '⚪ Offline'}\n"
                    f"📬 Semua sinyal OPEN/TP/CLOSE dikirim realtime ke chat ini."
                )
            except Exception as exc:
                log.warning("gagal kirim startup notice: %s", exc)

    async def stop(self) -> None:
        self._running = False
        await self.cex.close()
        await self.dex.close()
        await self.shadow.close()
        await self.scanner.close()
        await self.meme.close()
        await self.whale.close()
        await self.ft.close()
        await self.llm.close()
        await self.db.close()

    # ------------------------------------------------------------ user profiles

    async def get_user_profile(self, user_id: str | int = "default") -> dict[str, Any]:
        uid = str(user_id)
        row = await self.db.fetchone("SELECT * FROM user_profiles WHERE user_id=?", (uid,))
        if row:
            return dict(row)
        # Default profile: Scalping, auto_trade=1, size=$50
        await self.db.execute(
            "INSERT INTO user_profiles(user_id, scheme, auto_trade, trade_size) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (uid, "scalp", 1, 100.0),
        )
        row2 = await self.db.fetchone("SELECT * FROM user_profiles WHERE user_id=?", (uid,))
        return dict(row2) if row2 else {"user_id": uid, "scheme": "scalp", "auto_trade": 1, "trade_size": 100.0}

    async def set_user_scheme(self, user_id: str | int, scheme: str) -> None:
        uid = str(user_id)
        valid_scheme = "safe" if scheme.lower() == "safe" else "scalp"
        await self.db.execute(
            "INSERT INTO user_profiles(user_id, scheme, auto_trade, trade_size) VALUES(?,?,1,100.0) "
            "ON CONFLICT(user_id) DO UPDATE SET scheme=excluded.scheme, updated_at=datetime('now')",
            (uid, valid_scheme),
        )
        # also update default if it's primary user
        if uid != "default":
            await self.db.execute("UPDATE user_profiles SET scheme=? WHERE user_id='default'", (valid_scheme,))

    async def set_user_auto_trade(self, user_id: str | int, enabled: bool) -> None:
        uid = str(user_id)
        val = 1 if enabled else 0
        await self.db.execute(
            "INSERT INTO user_profiles(user_id, scheme, auto_trade, trade_size) VALUES(?, 'scalp', ?, 100.0) "
            "ON CONFLICT(user_id) DO UPDATE SET auto_trade=excluded.auto_trade, updated_at=datetime('now')",
            (uid, val),
        )
        if uid != "default":
            await self.db.execute("UPDATE user_profiles SET auto_trade=? WHERE user_id='default'", (val,))

    async def set_user_trade_size(self, user_id: str | int, size: float) -> None:
        uid = str(user_id)
        await self.db.execute(
            "INSERT INTO user_profiles(user_id, scheme, auto_trade, trade_size) VALUES(?, 'scalp', 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET trade_size=excluded.trade_size, updated_at=datetime('now')",
            (uid, size),
        )
        if uid != "default":
            await self.db.execute("UPDATE user_profiles SET trade_size=? WHERE user_id='default'", (size,))

    async def run(self) -> None:
        self._running = True
        await self.start()
        while self._running:
            t0 = time.time()
            try:
                await self.cycle()
            except Exception:
                log.exception("cycle gagal")
            elapsed = time.time() - t0
            await asyncio.sleep(max(1.0, self.s.loop_interval_sec - elapsed))
        await self.stop()

    # ------------------------------------------------------------ one cycle

    async def cycle(self) -> None:
        await self.refresh_equity_state()
        await self.manage_open_positions()

        # Research pathway is isolated; it can progress while wallet/order automation remains halted.
        await self.hunt()
        await self.shadow.mark_and_exit()
        await self.sync_freqtrade_alerts()

        halted, why = await self.risk.is_halted()
        if halted:
            log.info("wallet trading halted; shadow research continues: %s", why)
        else:
            await self.check_whales()
            await self.check_1h_whale_refresh()
        # Shadow reports are research-only and must continue even while wallet trading is halted.
        await self.check_14wib_report()

        if time.time() - self._last_reflect > 300:
            self._last_reflect = time.time()
            n = await self.reflector.run_once()
            if n:
                log.info("refleksi selesai untuk %d episode", n)

        self.stats = await self.memory.stats()

    # ------------------------------------------------------------ equity

    async def refresh_equity_state(self) -> None:
        # Paper equity is fixed seed plus realized legacy-wallet PnL plus mark-to-executable unrealized PnL.
        realized = await self.db.fetchone(
            "SELECT COALESCE(SUM(pnl_usd),0) s FROM positions WHERE status='closed'"
        )
        realized_usd = float(realized["s"] or 0)
        base = self.s.paper_equity_usd if not self.s.live else 0.0
        if self.s.live:
            try:
                bal = await self.cex.balance(self.s.base_currency)
                base = bal if bal > 0 else await self.db.kv_get("last_equity", 0.0)
            except Exception:
                base = await self.db.kv_get("last_equity", 0.0)

        unreal = 0.0
        rows = await self.db.fetchall("SELECT * FROM positions WHERE status='open'")
        for row in rows:
            pos = _row_to_position(row)
            px = await self._price_for(pos.symbol, pos.chain, pos.venue, pos.id)
            if px and px > 0:
                unreal += pos.unrealized_usd(px)

        self.equity_usd = round(base + realized_usd + unreal, 2)
        await self.db.kv_set("last_equity", self.equity_usd)

        if abs(self.equity_usd - self._last_equity) > 0.5:
            self._last_equity = self.equity_usd
            await self.db.execute(
                "INSERT INTO equity(equity_usd,realized,unrealized) VALUES(?,?,?)",
                (self.equity_usd, realized_usd, unreal),
            )

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if await self.db.kv_get("equity_day") != day:
            await self.db.kv_set("equity_day", day)
            await self.db.kv_set("day_start_equity", self.equity_usd)

    # ------------------------------------------------------------ positions

    async def _price_for(self, symbol: str, chain: str | None,
                         venue: Venue, pos_id: int) -> float | None:
        """Legacy position mark. Use mint and pool identity; never key cache by ticker."""
        try:
            if venue is Venue.DEX:
                row = await self.db.fetchone("SELECT meta,qty FROM positions WHERE id=?", (pos_id,))
                if not row or not row["meta"] or not chain:
                    return None
                meta = json.loads(row["meta"])
                addr = meta.get("address")
                if not addr:
                    return None
                qty = float(row["qty"] or 0.0)
                if qty > 0:
                    return await self.dex.sell_price(chain, addr, qty)
                return await self.dex.price(chain, addr)
            return await self.cex.price(symbol)
        except Exception as exc:
            log.debug("harga %s #%s gagal: %s", symbol, pos_id, exc)
            return None

    async def manage_open_positions(self) -> None:
        rows = await self.db.fetchall("SELECT * FROM positions WHERE status='open'")
        if not rows:
            return

        # IMPORTANT: key by position ID, not ticker; different mints can share a symbol.
        feed: dict[int, float] = {}
        for row in rows:
            pos = _row_to_position(row)
            px = await self._price_for(pos.symbol, pos.chain, pos.venue, pos.id)
            if px and px > 0:
                feed[pos.id] = px

        for pos, reason, fraction in await self.risk.refresh_stops(feed):
            px = feed.get(pos.id)
            if px:
                await self.close_position(pos.id, px, reason, fraction=fraction)

    async def close_position(
        self, pos_id: int, price: float, reason: str, fraction: float = 1.0
    ) -> None:
        row = await self.db.fetchone("SELECT * FROM positions WHERE id=?", (pos_id,))
        if not row:
            return
        pos = _row_to_position(row)
        if pos.status != "open":
            return

        fraction = max(0.01, min(1.0, fraction))
        close_qty = pos.qty * fraction
        is_full = fraction >= 0.999

        fill_price = price
        tx: str | None = None
        fee = 0.0
        if self.s.live:
            try:
                import json as _json
                meta = _json.loads(row["meta"]) if row["meta"] else {}
                intent = OrderIntent(
                    venue=pos.venue,
                    side=Side.SELL,
                    symbol=pos.symbol,
                    usd_amount=close_qty * price,
                    chain=pos.chain,
                    address=meta.get("address"),
                    slippage_bps=300,
                    reason=f"exit: {reason} ({fraction*100:.0f}%)",
                )
                fill = (
                    await self.dex.execute(intent)
                    if pos.venue is Venue.DEX
                    else await self.cex.execute(intent)
                )
                if fill.price > 0:
                    fill_price = fill.price
                fee = fill.fee_usd
                tx = fill.tx_hash
            except Exception as exc:
                log.error("gagal eksekusi penjualan #%d: %s", pos_id, exc)
                return

        pnl_usd = (fill_price - pos.entry_price) * close_qty - fee
        pnl_pct = (fill_price - pos.entry_price) / pos.entry_price * 100

        if is_full:
            await self.db.execute(
                "UPDATE positions SET status='closed', closed_at=datetime('now'), "
                "exit_price=?, pnl_usd=?, pnl_pct=?, fees_usd=? WHERE id=?",
                (fill_price, round(pnl_usd, 4), round(pnl_pct, 4), fee, pos_id),
            )
        else:
            rem_qty = pos.qty - close_qty
            rem_usd = rem_qty * pos.entry_price
            await self.db.execute(
                "UPDATE positions SET qty=?, entry_usd=? WHERE id=?",
                (rem_qty, round(rem_usd, 4), pos_id),
            )

        await self.db.execute(
            "INSERT INTO fills(position_id,side,qty,price,fee_usd,tx_hash,venue) "
            "VALUES(?,?,?,?,?,?,?)",
            (pos_id, "sell", close_qty, fill_price, fee, tx, pos.venue.value),
        )

        await self.memory.record_episode(
            kind="trade_close",
            context={
                "reason": reason,
                "entry": pos.entry_price,
                "exit": fill_price,
                "fraction": fraction,
            },
            action={"pos_id": pos_id, "held_pct": pnl_pct},
            symbol=pos.symbol,
            chain=pos.chain,
            outcome="win" if pnl_usd > 0 else "loss",
            pnl_pct=pnl_pct,
        )
        log.info(
            "tutup #%d %s (%.0f%%) pnl %+.2f%% (%s)",
            pos_id, pos.symbol, fraction * 100, pnl_pct, reason
        )

        if self.notifier:
            icon = "🟢" if pnl_usd > 0 else "🔴"
            action_label = "TAKE PROFIT" if pnl_usd > 0 else "STOP LOSS"
            pct_txt = "100%" if fraction >= 0.999 else f"{fraction*100:.0f}%"
            try:
                await self.notifier(
                    f"{icon} <b>AUTO-CLOSE: {action_label} ({pct_txt})</b>\n"
                    f"💊 Token: <b>#{pos_id} {pos.symbol}</b>\n"
                    f"🎯 Keluar: ${fill_price:.8f} (Masuk: ${pos.entry_price:.8f})\n"
                    f"📈 Hasil PnL: <b>{pnl_pct:+.2f}%</b> (<b>${pnl_usd:+.2f}</b>)\n"
                    f"🏷️ Pemicu: <i>{reason}</i>"
                )
            except Exception as exc:
                log.debug("auto-close notify error: %s", exc)

    # ------------------------------------------------------------ hunting

    async def hunt(self) -> None:
        # Isolated shadow experiment only. The legacy position executor stays unreachable.
        now = time.time()
        if now - self._last_shadow_candidate_scan < 120.0:
            return
        self._last_shadow_candidate_scan = now

        signals: list[Signal] = []
        if self.s.meme.enabled:
            for chain in self.s.meme.allow_chains:
                try:
                    signals.extend(await self.meme.discover(chain, limit=60))
                except Exception as exc:
                    log.debug("shadow scan %s gagal: %s", chain, exc)

        sol_sigs = [s for s in signals if s.chain == "solana" and s.address]
        opened_count = 0
        for sig in sol_sigs:
            # Remove generic symbols that collide with majors or ticker-indexing errors.
            base = sig.symbol.split("/")[0].upper()
            if base in {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "TRUMP", "USD1", "USDC", "USDT"}:
                continue
            for plan in ACTIVE_PLANS:
                try:
                    if await self.shadow.consider(plan, sig):
                        opened_count += 1
                except Exception as exc:
                    log.debug("shadow candidate %s/%s error: %s", plan, sig.symbol, exc)

        log.info("shadow scan selesai: %d kandidat solana, %d posisi baru dibuka", len(sol_sigs), opened_count)
        return

        p1_cfg = get_scheme("plan1")
        p2_cfg = get_scheme("plan2")
        p3_cfg = get_scheme("plan3")

        for orig_sig in signals:
            if not self._running:
                break

            # Protect benchmark from meme scanner ticker collisions; create a distinct virtual market.
            if orig_sig.symbol.split("/")[0].upper() in {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "TRUMP", "USD1", "USDC", "USDT"}:
                continue

            liq = float(orig_sig.features.get("liquidity_usd") or 0.0)
            vol24 = float(orig_sig.features.get("volume_24h") or 0.0)
            buys = float(orig_sig.features.get("buys_1h") or 0.0)
            sells = float(orig_sig.features.get("sells_1h") or 1.0)
            age = float(orig_sig.features.get("age_minutes") or 0.0)
            chg1 = float(orig_sig.features.get("chg_1h") or 0.0)
            buy_ratio = buys / max(sells, 1.0)

            # --- RENCANA 1: TP LADDER (Bot Utama) ---
            if liq >= p1_cfg.min_liquidity_usd:
                import copy
                s1 = copy.deepcopy(orig_sig)
                s1.features["scheme"] = "plan1"
                s1.features["account_id"] = "default"
                try:
                    await self.pursue(s1)
                except Exception:
                    pass

            # --- RENCANA 2: BREAKOUT MOMENTUM (Shadow Paper) ---
            # Syarat: buyer dominance >= 1.7x dan volume sehat
            if liq >= p2_cfg.min_liquidity_usd and vol24 >= 50_000 and buy_ratio >= 1.7:
                import copy
                s2 = copy.deepcopy(orig_sig)
                s2.features["scheme"] = "plan2"
                s2.features["account_id"] = "shadow_p2"
                try:
                    await self.pursue(s2)
                except Exception:
                    pass

            # --- RENCANA 3: VOLUME-DELTA SNIPER (Shadow Paper) ---
            # Syarat: koin baru (age <= 180m), likuiditas tebal, momentum terkendali (5% - 35%)
            if liq >= p3_cfg.min_liquidity_usd and 10 <= age <= 180 and 5.0 <= chg1 <= 35.0:
                import copy
                s3 = copy.deepcopy(orig_sig)
                s3.features["scheme"] = "plan3"
                s3.features["account_id"] = "shadow_p3"
                try:
                    await self.pursue(s3)
                except Exception:
                    pass

    async def pursue(self, sig: Signal) -> int | None:
        state = await self.risk.account_state(self.equity_usd)
        scope = sig.chain or "cex"

        penalty, lessons = await self.memory.score_penalty(
            f"{sig.symbol} {sig.chain} {sig.strategy} {sig.features.get('reasons')}", scope
        )

        target_scheme = sig.features.get("scheme") or "scalp"
        account_id = sig.features.get("account_id") or "default"
        scheme_cfg = get_scheme(target_scheme)

        if sig.strategy == "whale_follow" and "copy_usd" in sig.features:
            want_usd = float(sig.features["copy_usd"])
        elif "usd_amount" in sig.features:
            want_usd = float(sig.features["usd_amount"])
        else:
            prof = await self.get_user_profile(account_id)
            want_usd = float(prof.get("trade_size") or scheme_cfg.default_size_usd)
            want_usd = min(
                want_usd,
                self.equity_usd * (self.s.risk.max_position_pct / 100),
            )

        intent = OrderIntent(
            venue=sig.venue,
            side=Side.BUY,
            symbol=sig.symbol,
            usd_amount=want_usd,
            chain=sig.chain,
            address=sig.address,
            slippage_bps=250,
            reason=f"{sig.strategy} [{scheme_cfg.name}] score={sig.score}",
        )

        decision = await self.risk.evaluate(intent, state, extra={
            "memory_penalty": penalty,
            "max_position_usd": want_usd if sig.strategy == "whale_follow" else self.s.meme.max_position_usd,
            "stop_pct": scheme_cfg.stop_loss_pct,
        })

        sig_id = await self.db.execute(
            "INSERT INTO signals(strategy,symbol,chain,score,features,accepted,reject_reason) "
            "VALUES(?,?,?,?,?,?,?)",
            (sig.strategy, sig.symbol, sig.chain, sig.score,
             str(sig.features)[:4000], 1 if decision.approved else 0, decision.reason),
        )

        if not decision.approved:
            await self.memory.record_episode(
                kind="signal_rejected",
                context={"features": sig.features, "lessons": lessons,
                         "checks": decision.checks},
                action={"reason": decision.reason, "score": sig.score},
                symbol=sig.symbol, chain=sig.chain, outcome="blocked",
            )
            return None

        if sig.venue is Venue.DEX and sig.chain and sig.address:
            chain = get_chain(sig.chain)
            report = await self.scanner.scan(chain, sig.address)
            if not report.ok:
                await self.db.execute(
                    "UPDATE signals SET accepted=0, reject_reason=? WHERE id=?",
                    ("safety: " + ", ".join(report.reasons), sig_id),
                )
                await self.memory.record_episode(
                    kind="safety_block",
                    context={"features": sig.features, "reasons": report.reasons,
                             "safety_score": report.score},
                    action={"address": sig.address},
                    symbol=sig.symbol, chain=sig.chain, outcome="blocked",
                )
                return None

        # AI Judge Decision Gate: AI mengevaluasi dan memutuskan apakah token layak dibeli
        ai_verdict = await self.judge.evaluate_entry(
            sig,
            report=report if (sig.venue is Venue.DEX and sig.chain and sig.address) else None,
            lessons=lessons,
            default_tp=scheme_cfg.take_profit_pct,
            default_sl=scheme_cfg.stop_loss_pct,
        )

        if not ai_verdict.approved:
            log.info("AI JUDGE MENOLAK %s: %s (keyakinan: %.2f)", sig.symbol, ai_verdict.reasoning, ai_verdict.confidence)
            await self.db.execute(
                "UPDATE signals SET accepted=0, reject_reason=? WHERE id=?",
                (f"ai_rejected: {ai_verdict.reasoning}", sig_id),
            )
            await self.memory.record_episode(
                kind="ai_rejected",
                context={"features": sig.features, "reasoning": ai_verdict.reasoning, "confidence": ai_verdict.confidence},
                action={"address": sig.address},
                symbol=sig.symbol, chain=sig.chain, outcome="ai_rejected",
            )
            return None

        intent.usd_amount = decision.usd_amount
        intent.signal_id = sig_id

        fill: Fill = await self.dex.execute(intent) if sig.venue is Venue.DEX \
            else await self.cex.execute(intent)

        if fill.qty <= 0 or fill.price <= 0:
            log.warning("fill tidak valid untuk %s", sig.symbol)
            return None

        stop = fill.price * (1 - scheme_cfg.stop_loss_pct / 100)
        tp = fill.price * (1 + scheme_cfg.take_profit_pct / 100)
        trailing = fill.price * (1 + scheme_cfg.trailing_activation_pct / 100)
        import json

        pos_id = await self.db.execute(
            "INSERT INTO positions(venue,chain,symbol,side,qty,entry_price,entry_usd,"
            "stop_loss,take_profit,trailing_stop,peak_price,signal_id,meta,scheme,account_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sig.venue.value, sig.chain, sig.symbol, "buy", fill.qty, fill.price,
                decision.usd_amount, stop, tp, trailing, fill.price, sig_id,
                json.dumps({"address": sig.address, "tx": fill.tx_hash, "reason": intent.reason, "whale_address": sig.features.get("whale")}),
                target_scheme, account_id,
            ),
        )
        await self.db.execute(
            "INSERT INTO fills(side,qty,price,fee_usd,tx_hash,venue) VALUES(?,?,?,?,?,?)",
            ("buy", fill.qty, fill.price, fill.fee_usd, fill.tx_hash, sig.venue.value),
        )
        await self.memory.record_episode(
            kind="trade_open",
            context={"features": sig.features, "checks": decision.checks,
                     "lessons": lessons, "scheme": target_scheme},
            action={"usd": decision.usd_amount, "price": fill.price,
                    "stop": stop, "tp": tp, "tx": fill.tx_hash},
            symbol=sig.symbol, chain=sig.chain,
        )
        log.info("BUKA #%d [%s] %s %.4f @ %.6f ($%.2f) score=%.2f",
                 pos_id, target_scheme.upper(), sig.symbol, fill.qty, fill.price, decision.usd_amount, sig.score)

        if self.notifier and sig.strategy != "whale_follow":
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            chain_name = (sig.chain or "solana").lower()
            addr_str = f"CA: <code>{sig.address}</code>\n" if sig.address else ""
            gmgn_url = f"https://gmgn.ai/{'sol' if chain_name=='solana' else chain_name}/token/{sig.address}" if sig.address else ""

            buttons = [
                [
                    InlineKeyboardButton(text="💰 Cek di Posisi", callback_data="rf:pos"),
                    InlineKeyboardButton(text=f"🔴 Tutup #{pos_id}", callback_data=f"pos:close:{pos_id}:1.0"),
                ]
            ]
            if gmgn_url:
                buttons.append([InlineKeyboardButton(text="🔍 Chart GMGN", url=gmgn_url)])

            try:
                await self.notifier(
                    f"🤖 <b>AI AUTO-BUY TERISI (PAPER)</b>\n"
                    f"💊 Token: <b>{sig.symbol}</b> · {scheme_cfg.badge}\n"
                    f"💵 Nominal: <b>${decision.usd_amount:.2f}</b> ({fill.qty:,.2f} token)\n"
                    f"🎯 Entry: <b>${fill.price:.8f}</b>\n"
                    f"🛑 Stop Loss: ${stop:.8f} (-{scheme_cfg.stop_loss_pct:.0f}%)\n"
                    f"🎯 Take Profit: ${tp:.8f} (+{scheme_cfg.take_profit_pct:.0f}%)\n"
                    f"📂 Posisi: <b>#{pos_id}</b>\n"
                    f"{addr_str}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                )
            except Exception as exc:
                log.debug("auto-buy notify error: %s", exc)

        return pos_id

    async def sync_freqtrade_alerts(self) -> None:
        """Relay newly opened/closed Freqtrade dry-run CEX trades to Telegram."""
        if not self.notifier or not await self.ft.ping():
            return
        try:
            open_trades = await self.ft.open_trades()
            closed_trades = await self.ft.closed_trades(limit=15)
        except Exception as exc:
            log.debug("sync_freqtrade_alerts gagal: %s", exc)
            return

        seen_open = set(await self.db.kv_get("ft_seen_open", []) or [])
        seen_closed = set(await self.db.kv_get("ft_seen_closed", []) or [])
        first_boot = not await self.db.kv_get("ft_alerts_initialized", False)
        if first_boot:
            seen_open |= {int(t["trade_id"]) for t in open_trades if "trade_id" in t}
            seen_closed |= {int(t["trade_id"]) for t in closed_trades if "trade_id" in t}
            await self.db.kv_set("ft_seen_open", sorted(seen_open)[-200:])
            await self.db.kv_set("ft_seen_closed", sorted(seen_closed)[-200:])
            await self.db.kv_set("ft_alerts_initialized", True)
            return

        changed_open = False
        for t in open_trades:
            tid = int(t.get("trade_id") or 0)
            if tid and tid not in seen_open:
                seen_open.add(tid)
                changed_open = True
                await self.notifier(
                    f"⚙️ <b>AUTO-TRADE CEX SALDO UJI (OPEN)</b>\n"
                    f"📌 <b>Freqtrade 1h (GembulSwing)</b> · <b>{t.get('pair')}</b> (<code>#{tid}</code>)\n"
                    f"💵 Stake Uji: <b>${float(t.get('stake_amount') or 0):.2f}</b> · Entry: <b>${float(t.get('open_rate') or 0):.4f}</b>\n"
                    f"🏷️ Tag: <i>{t.get('enter_tag') or 'z20_snapback'}</i>"
                )

        changed_closed = False
        for t in closed_trades:
            tid = int(t.get("trade_id") or 0)
            if tid and tid not in seen_closed:
                seen_closed.add(tid)
                changed_closed = True
                prof_abs = float(t.get("profit_abs") or 0.0)
                prof_pct = float(t.get("profit_pct") or 0.0)
                icon = "🟢" if prof_abs >= 0 else "🔴"
                await self.notifier(
                    f"{icon} <b>AUTO-TRADE CEX SALDO UJI (CLOSED)</b>\n"
                    f"📌 <b>Freqtrade 1h (GembulSwing)</b> · <b>{t.get('pair')}</b> (<code>#{tid}</code>)\n"
                    f"🏷️ Exit: <i>{t.get('exit_reason') or 'closed'}</i>\n"
                    f"💵 Realized: <b>${prof_abs:+.2f}</b> (<b>{prof_pct:+.2f}%</b>)"
                )

        if changed_open:
            await self.db.kv_set("ft_seen_open", sorted(seen_open)[-200:])
        if changed_closed:
            await self.db.kv_set("ft_seen_closed", sorted(seen_closed)[-200:])

    async def check_14wib_report(self) -> None:
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        wib_dt = now_utc + timedelta(hours=7)
        if wib_dt.hour >= 14:
            today_str = wib_dt.strftime("%Y-%m-%d")
            key = f"reported_14wib_{today_str}"
            if not await self.db.kv_get(key):
                await self.send_14wib_summary()
                await self.db.kv_set(key, True)

    async def send_14wib_summary(self) -> None:
        if not self.notifier:
            return
        data = await self.journal.compare_three_plans(period="daily")
        p1 = data["plan1"]
        p2 = data["plan2"]
        p3 = data["plan3"]

        def rank_plan(p):
            return p["total_pnl_usd"] + p.get("unrealized_usd", 0)

        plans = [
            ("Rencana 1 (TP Ladder Axiom)", p1, "⚡️"),
            ("Rencana 2 (Breakout Momentum)", p2, "🚀"),
            ("Rencana 3 (Volume-Delta Sniper)", p3, "🎯"),
        ]
        plans.sort(key=lambda x: rank_plan(x[1]), reverse=True)
        winner_name = plans[0][0]

        txt = (
            f"📢 <b>LAPORAN EVALUASI TRADING JAM 14:00 WIB</b>\n"
            f"📅 Tanggal: <code>{p1['period_key']}</code>\n"
            f"💵 Modal Awal: <b>$1,000.00</b> (Disamakan Masing-Masing)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡️ <b>1. Rencana 1 (TP Ladder Axiom)</b> — [Bot Aktif]\n"
            f"   • Saldo: <b>${p1['current_equity']:,.2f}</b> ({p1['total_pnl_usd']:+,.2f} USD)\n"
            f"   • Win Rate: <b>{p1['win_rate']:.1f}%</b> ({p1['win_trades']}W / {p1['loss_trades']}L · {p1['total_trades']} trade)\n"
            f"   • Posisi Aktif: {p1['open_count']} koin (${p1['unrealized_usd']:+,.2f})\n\n"
            f"🚀 <b>2. Rencana 2 (Breakout Momentum)</b> — [Shadow Test]\n"
            f"   • Saldo: <b>${p2['current_equity']:,.2f}</b> ({p2['total_pnl_usd']:+,.2f} USD)\n"
            f"   • Win Rate: <b>{p2['win_rate']:.1f}%</b> ({p2['win_trades']}W / {p2['loss_trades']}L · {p2['total_trades']} trade)\n"
            f"   • Posisi Aktif: {p2['open_count']} koin (${p2['unrealized_usd']:+,.2f})\n\n"
            f"🎯 <b>3. Rencana 3 (Volume-Delta Sniper)</b> — [Shadow Test]\n"
            f"   • Saldo: <b>${p3['current_equity']:,.2f}</b> ({p3['total_pnl_usd']:+,.2f} USD)\n"
            f"   • Win Rate: <b>{p3['win_rate']:.1f}%</b> ({p3['win_trades']}W / {p3['loss_trades']}L · {p3['total_trades']} trade)\n"
            f"   • Posisi Aktif: {p3['open_count']} koin (${p3['unrealized_usd']:+,.2f})\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 <b>Peringkat Terunggul Saat Ini:</b>\n"
            f"👉 <b>{winner_name}</b>\n"
        )
        try:
            await self.notifier(txt)
            log.info("Laporan 14:00 WIB terkirim ke Telegram.")
        except Exception as exc:
            log.warning("Gagal kirim laporan 14:00 WIB: %s", exc)

    async def check_1h_whale_refresh(self) -> None:
        if time.time() - self._last_whale_refresh_ts >= 3600.0:
            self._last_whale_refresh_ts = time.time()
            await self.run_1h_whale_refresh()

    async def run_1h_whale_refresh(self) -> None:
        try:
            added = await self.whale.auto_refresh_whales_1h()
            if added and self.notifier:
                lines = [
                    "🐋 <b>RADAR 1 JAM: WHALE BARU LOLOS STANDAR & DIPANTAU</b>",
                    "<i>Sistem otomatis menyeleksi maksimal 5 paus teratas (rasio beli sehat & anti-MEV):</i>",
                    "━━━━━━━━━━━━━━━━━━━━",
                ]
                for i, w in enumerate(added, 1):
                    addr_short = f"{w['address'][:4]}...{w['address'][-4:]}"
                    label = w.get('label') or f"SmartWhale-{i}"
                    lines.append(f"{i}· <b>{label}</b> (<code>{addr_short}</code>) · Auto $100")
                lines.append("━━━━━━━━━━━━━━━━━━━━\n<i>Whale yang tidak bertumbuh/rugi akan otomatis diprune sistem.</i>")
                await self.notifier("\n".join(lines))
        except Exception as exc:
            log.debug("run_1h_whale_refresh error: %s", exc)

    async def check_whales(self) -> None:
        try:
            events = await self.whale.poll_new_buys()
        except Exception as exc:
            log.debug("check_whales gagal: %s", exc)
            return

        for ev in events:
            whale_tag = ev["label"]
            addr_short = f"{ev['whale_address'][:4]}...{ev['whale_address'][-4:]}"
            sym = ev["symbol"]
            mint = ev["mint"]
            chain = ev["chain"]
            usd = ev["copy_usd"]
            auto_copy = ev["auto_copy"]
            liq = float(ev.get("liquidity") or 0.0)
            chg1 = float(ev.get("chg_1h") or 0.0)

            log.info("WHALE DETECTED: %s (%s) beli %s (Liq: $%s)", whale_tag, addr_short, sym, f"{liq:,.0f}")

            prof = await self.get_user_profile("default")
            active_scheme = prof.get("scheme", "scalp")
            scheme_cfg = get_scheme(active_scheme)

            # Smart Screening Meme Coin: Longgar & Fleksibel (hanya blok rugpull / likuiditas nol)
            screening_passed = True
            reject_reason = ""

            if liq > 0 and liq < 8_000.0:
                screening_passed = False
                reject_reason = f"Likuiditas sangat tipis (${liq:,.0f} < $8k)"
            elif chg1 < -65.0:
                screening_passed = False
                reject_reason = f"Token sedang dump ekstrem (1h: {chg1:+.1f}%)"

            if screening_passed:
                report = await self.scanner.scan(get_chain(chain), mint)
                if not report.ok:
                    screening_passed = False
                    reject_reason = f"Gagal Safety: {', '.join(report.reasons[:2])}"

            if not screening_passed:
                log.info("WHALE TOKEN DITOLAK SCREENING: %s beli %s (%s)", whale_tag, sym, reject_reason)
                if self.notifier:
                    try:
                        await self.notifier(
                            f"🐋 <b>WHALE TERDETEKSI (DIABAIKAN)</b>\n"
                            f"👤 Whale: <b>{whale_tag}</b> (<code>{addr_short}</code>)\n"
                            f"💊 Token: <b>{sym}</b>\n"
                            f"🛡️ <b>Hasil Screening:</b> 🚫 <b>Ditolak AI</b>\n"
                            f"⚠️ Alasan: <i>{reject_reason}</i>\n"
                            f"CA: <code>{mint}</code>"
                        )
                    except Exception as exc:
                        log.debug("whale notify reject gagal: %s", exc)
                continue

            if auto_copy:
                prof = await self.get_user_profile("default")
                active_scheme = prof.get("scheme", "scalp")
                sig = Signal(
                    strategy="whale_follow",
                    symbol=sym,
                    score=0.88,
                    venue=Venue.DEX,
                    chain=chain,
                    address=mint,
                    price=ev.get("price"),
                    features={
                        "whale": ev["whale_address"],
                        "whale_label": whale_tag,
                        "whale_tx": ev["tx_hash"],
                        "copy_usd": usd,
                        "scheme": active_scheme,
                        "account_id": "default",
                    },
                )
                pos_id = None
                try:
                    pos_id = await self.pursue(sig)
                except Exception as exc:
                    log.warning("gagal pursue whale signal %s: %s", sym, exc)

                if self.notifier:
                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                    if pos_id:
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [
                                InlineKeyboardButton(text="💰 Cek di Posisi", callback_data="rf:pos"),
                                InlineKeyboardButton(text=f"🔴 Tutup #{pos_id}", callback_data=f"pos:close:{pos_id}:1.0"),
                            ],
                            [
                                InlineKeyboardButton(text="🔍 GMGN", url=f"https://gmgn.ai/sol/token/{mint}"),
                            ]
                        ])
                        try:
                            await self.notifier(
                                f"🐋 <b>AUTO-COPY WHALE BERHASIL!</b>\n"
                                f"👤 Whale: <b>{whale_tag}</b> (<code>{addr_short}</code>)\n"
                                f"💊 Token: <b>{sym}</b>\n"
                                f"💵 Nominal: <b>${usd:.0f}</b> masuk ke <b>Posisi Aktif #{pos_id}</b>\n"
                                f"🛑 Stop Loss: -{scheme_cfg.stop_loss_pct:.0f}% · 🎯 Take Profit: +{scheme_cfg.take_profit_pct:.0f}%\n"
                                f"CA: <code>{mint}</code>",
                                reply_markup=kb,
                            )
                        except Exception as exc:
                            log.debug("whale notify gagal: %s", exc)
                    else:
                        try:
                            await self.notifier(
                                f"🐋 <b>WHALE ALERT (TIDAK DIBELI)</b>\n"
                                f"👤 Whale: <b>{whale_tag}</b> (<code>{addr_short}</code>) beli <b>{sym}</b>\n"
                                f"🚫 Pembelian otomatis ditolak oleh Safety / Risk Engine (Kill Switch / LP / Limit).",
                            )
                        except Exception as exc:
                            log.debug("whale notify gagal: %s", exc)
            else:
                if self.notifier:
                    import secrets
                    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                    rid = secrets.token_urlsafe(6)
                    await self.db.kv_set(
                        f"buy_req:{rid}",
                        {"chain": chain, "address": mint, "usd": usd, "symbol": sym},
                    )
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text=f"🟢 Ikut Beli {sym} ${usd:.0f}",
                            callback_data=f"buy:{rid}",
                        )]
                    ])
                    try:
                        await self.notifier(
                            f"🐋 <b>WHALE ALERT</b>\n"
                            f"👤 Whale: <b>{whale_tag}</b> (<code>{addr_short}</code>) baru beli <b>{sym}</b>\n"
                            f"CA: <code>{mint}</code>",
                            reply_markup=kb,
                        )
                    except Exception as exc:
                        log.debug("whale notify gagal: %s", exc)

    # ------------------------------------------------------------ reporting

    async def snapshot(self) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT * FROM positions WHERE status='open'")
        positions = []
        for row in rows:
            pos = _row_to_position(row)
            px = await self._price_for(pos.symbol, pos.chain, pos.venue, pos.id)
            positions.append({
                "id": pos.id,
                "venue": pos.venue.value,
                "chain": pos.chain,
                "symbol": pos.symbol,
                "qty": pos.qty,
                "entry": pos.entry_price,
                "price": px,
                "scheme": row["scheme"] if "scheme" in row.keys() else "scalp",
                "upnl_pct": round(pos.unrealized_pct(px), 2) if px else None,
                "upnl_usd": round(pos.unrealized_usd(px), 2) if px else None,
            })

        halted, why = await self.risk.is_halted()
        return {
            "mode": self.s.mode,
            "live": self.s.real_trading_enabled,
            "equity_usd": self.equity_usd,
            "halted": halted,
            "halt_reason": why,
            "positions": positions,
            "stats": self.stats or await self.memory.stats(),
            "freqtrade": await self.ft.summary(),
        }
