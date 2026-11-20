"""Multi-source Solana/DEX meme discovery: GeckoTerminal + DexScreener + Binance Web3.

Combines:
1. GeckoTerminal trending_pools (p1-2) and new_pools (p1)
2. DexScreener token-boosts (top/latest) enriched with live pair metrics (m5/h1/h6)
3. Binance Web3 Solana trending rank & smart-money net inflow (CT_501)
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from gembul.core.models import Signal, Venue
from gembul.settings import Settings

log = logging.getLogger(__name__)

GT = "https://api.geckoterminal.com/api/v2"
DEXS = "https://api.dexscreener.com"
BW3_RANK = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/pulse/unified/rank/list"
BW3_INFLOW = "https://web3.binance.com/bapi/defi/v1/public/wallet-direct/tracker/wallet/token/inflow/rank/query"

NETWORKS = {
    "solana": "solana",
    "base": "base",
    "bsc": "bsc",
    "ethereum": "eth",
    "arbitrum": "arbitrum",
    "polygon": "polygon_pos",
}

BW3_CHAINS = {
    "solana": "CT_501",
    "bsc": "56",
    "base": "8453",
    "ethereum": "1",
}


class MemeScanner:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._http = httpx.AsyncClient(
            timeout=25.0, headers={"User-Agent": "gembul/1.0", "Accept": "application/json"}
        )
        self._seen: dict[str, float] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ discovery

    async def discover(self, chain_name: str, limit: int = 60, page: int = 1) -> list[Signal]:
        network = NETWORKS.get(chain_name)
        if not network:
            return []

        gt_task = self._fetch_pools(network, page=page)
        sm_task = self._fetch_smart_money_inflow(chain_name)
        extra_task = self._fetch_extra_mints(chain_name)
        pools, inflow_map, extra_mints = await asyncio.gather(
            gt_task, sm_task, extra_task, return_exceptions=True
        )
        if isinstance(pools, Exception):
            pools = []
        if isinstance(inflow_map, Exception):
            inflow_map = {}
        if isinstance(extra_mints, Exception):
            extra_mints = []

        # Enrich extra mints (DexScreener boosts/profiles + Binance Web3 rank + smart-money inflow)
        known_mints = {p["mint"] for p in pools}
        combined_extra: list[str] = []
        seen_ex: set[str] = set()
        for m in extra_mints:
            if m and m not in known_mints and m not in seen_ex:
                seen_ex.add(m)
                combined_extra.append(m)
        for m, inf in sorted(inflow_map.items(), key=lambda kv: kv[1], reverse=True):
            if inf > 0 and m not in known_mints and m not in seen_ex:
                seen_ex.add(m)
                combined_extra.append(m)

        missing_mints = combined_extra[:50]
        if missing_mints:
            ds_pools = await self._fetch_dexscreener_tokens(chain_name, missing_mints)
            pools.extend(ds_pools)

        # Enrich top GeckoTerminal pools with DexScreener m5/h1/h6 data
        mints_for_ds = [
            p["mint"] for p in pools
            if p.get("source") != "dexscreener"
            and (_f(p["attr"].get("reserve_in_usd")) or 0.0) >= self.s.risk.min_liquidity_usd
        ][:25]
        ds_enrich: dict[str, dict[str, Any]] = {}
        if mints_for_ds:
            for item in await self._fetch_dexscreener_tokens(chain_name, mints_for_ds):
                ds_enrich[item["mint"]] = item["attr"]

        if not pools:
            return []

        signals: list[Signal] = []
        for pool in pools[:max(limit, 80)]:
            try:
                mint = pool["mint"]
                if mint in ds_enrich:
                    # Merge fresh m5/h1/h6/pool fields from DexScreener into attr
                    merged = dict(pool["attr"])
                    for k, v in ds_enrich[mint].items():
                        if v is not None:
                            merged[k] = v
                    pool = {**pool, "attr": merged}
                sig = self._evaluate(chain_name, pool, inflow_map.get(mint))
            except Exception as exc:
                log.debug("skoring %s gagal: %s", chain_name, exc)
                continue
            if sig:
                signals.append(sig)

        signals.sort(key=lambda x: x.score, reverse=True)
        return signals[:40]

    async def _fetch_pools(self, network: str, page: int = 1) -> list[dict]:
        pools: list[dict] = []
        queries = [("trending_pools", page), ("trending_pools", page + 1), ("new_pools", page)]
        for endpoint, pg in queries:
            try:
                r = await self._http.get(
                    f"{GT}/networks/{network}/{endpoint}", params={"page": pg}
                )
                if r.status_code != 200:
                    continue
                for item in (r.json() or {}).get("data") or []:
                    base = ((item.get("relationships") or {})
                            .get("base_token", {}).get("data", {}).get("id") or "")
                    mint = base.split("_", 1)[1] if "_" in base else ""
                    if not mint:
                        continue
                    pools.append({
                        "mint": mint,
                        "attr": item.get("attributes") or {},
                        "source": endpoint,
                    })
            except Exception as exc:
                log.debug("geckoterminal %s p%d gagal: %s", endpoint, pg, exc)

        dedup: dict[str, dict] = {}
        for p in pools:
            key = p["mint"]
            liq = _f(p["attr"].get("reserve_in_usd")) or 0.0
            prev = dedup.get(key)
            if prev is None or liq > (_f(prev["attr"].get("reserve_in_usd")) or 0.0):
                dedup[key] = p
        return list(dedup.values())

    async def _fetch_smart_money_inflow(self, chain_name: str) -> dict[str, float]:
        bw_id = BW3_CHAINS.get(chain_name)
        if not bw_id:
            return {}
        try:
            r = await self._http.post(
                BW3_INFLOW,
                headers={
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "binance-web3/3.0 (Skill)",
                },
                json={"chainId": bw_id, "period": "1h", "tagType": 2},
            )
            if r.status_code != 200:
                return {}
            out: dict[str, float] = {}
            for row in (r.json() or {}).get("data") or []:
                ca = row.get("ca")
                inf = _f(row.get("inflow"))
                if ca and inf is not None:
                    out[ca] = inf
            return out
        except Exception as exc:
            log.debug("bw3 smart money inflow gagal: %s", exc)
            return {}

    async def _fetch_extra_mints(self, chain_name: str) -> list[str]:
        mints: list[str] = []
        seen: set[str] = set()

        def add_mint(m: str | None) -> None:
            if m and m not in seen:
                seen.add(m)
                mints.append(m)

        # 1. Binance Web3 trending & hyped token ranks (sizes up to 30)
        bw_id = BW3_CHAINS.get(chain_name)
        if bw_id:
            for rtype in (10, 20):
                try:
                    r = await self._http.post(
                        BW3_RANK,
                        headers={
                            "Content-Type": "application/json",
                            "Accept-Encoding": "identity",
                            "User-Agent": "binance-web3/3.0 (Skill)",
                        },
                        json={
                            "rankType": rtype, "chainId": bw_id, "period": 40,
                            "sortBy": 70, "orderAsc": False, "page": 1, "size": 25,
                            "liquidityMin": 40000, "volumeMin": 30000,
                        },
                    )
                    if r.status_code == 200:
                        for t in ((r.json() or {}).get("data") or {}).get("tokens") or []:
                            add_mint(t.get("contractAddress"))
                except Exception as exc:
                    log.debug("bw3 token rank %d gagal: %s", rtype, exc)

        # 2. DexScreener top/latest boosted tokens & profiles
        for ds_ep in ("token-boosts/top/v1", "token-boosts/latest/v1", "token-profiles/latest/v1"):
            try:
                r = await self._http.get(f"{DEXS}/{ds_ep}")
                if r.status_code == 200:
                    for item in r.json() or []:
                        if item.get("chainId") == chain_name:
                            add_mint(item.get("tokenAddress"))
            except Exception as exc:
                log.debug("dexscreener %s gagal: %s", ds_ep, exc)

        return mints

    async def _fetch_dexscreener_tokens(self, chain_name: str, mints: list[str]) -> list[dict]:
        out: list[dict] = []
        # DexScreener allows up to 30 comma-separated addresses per call
        for i in range(0, len(mints), 25):
            chunk = ",".join(mints[i:i + 25])
            try:
                r = await self._http.get(f"{DEXS}/latest/dex/tokens/{chunk}")
                if r.status_code != 200:
                    continue
                pairs = (r.json() or {}).get("pairs") or []
                by_mint: dict[str, dict] = {}
                for p in pairs:
                    if p.get("chainId") != chain_name:
                        continue
                    base = p.get("baseToken") or {}
                    mint = base.get("address") or ""
                    liq = _f((p.get("liquidity") or {}).get("usd")) or 0.0
                    prev = by_mint.get(mint)
                    if prev is None or liq > (_f((prev.get("liquidity") or {}).get("usd")) or 0.0):
                        by_mint[mint] = p
                for mint, p in by_mint.items():
                    base = p.get("baseToken") or {}
                    tx = p.get("txns") or {}
                    ch = p.get("priceChange") or {}
                    vol = p.get("volume") or {}
                    created_ms = _f(p.get("pairCreatedAt"))
                    created_iso = None
                    if created_ms and created_ms > 0:
                        created_iso = datetime.fromtimestamp(created_ms / 1000.0, tz=timezone.utc).isoformat()
                    out.append({
                        "mint": mint,
                        "source": "dexscreener",
                        "attr": {
                            "name": f"{base.get('symbol') or '?'}/SOL",
                            "address": p.get("pairAddress"),
                            "reserve_in_usd": (p.get("liquidity") or {}).get("usd"),
                            "base_token_price_usd": p.get("priceUsd"),
                            "fdv_usd": p.get("fdv") or p.get("marketCap"),
                            "pool_created_at": created_iso,
                            "volume_usd": {
                                "h24": vol.get("h24"),
                                "h6": vol.get("h6"),
                                "h1": vol.get("h1"),
                                "m5": vol.get("m5"),
                            },
                            "price_change_percentage": {
                                "m5": ch.get("m5"),
                                "h1": ch.get("h1"),
                                "h6": ch.get("h6"),
                                "h24": ch.get("h24"),
                            },
                            "transactions": {
                                "m5": tx.get("m5") or {},
                                "h1": tx.get("h1") or {},
                            },
                        },
                    })
            except Exception as exc:
                log.debug("dexscreener batch gagal: %s", exc)
        return out

    # ------------------------------------------------------------ scoring

    def _evaluate(self, chain: str, pool: dict, smart_inflow: float | None = None) -> Signal | None:
        L = self.s.meme
        a = pool["attr"]
        mint = pool["mint"]
        now = time.time()

        # Keep scanner memory cooldown to 45s so 2-minute scalping scans re-evaluate active coins.
        if self._seen.get(mint, 0) + 45.0 > now:
            return None

        liq = _f(a.get("reserve_in_usd")) or 0.0
        vol_d = a.get("volume_usd") or {}
        vol24 = _f(vol_d.get("h24")) or 0.0
        vol1 = _f(vol_d.get("h1")) or 0.0
        vol5 = _f(vol_d.get("m5")) or 0.0
        ch = a.get("price_change_percentage") or {}
        ch5 = _f(ch.get("m5")) or 0.0
        ch1 = _f(ch.get("h1")) or 0.0
        ch6 = _f(ch.get("h6")) or 0.0
        ch24 = _f(ch.get("h24")) or 0.0
        price = _f(a.get("base_token_price_usd")) or 0.0
        fdv = _f(a.get("fdv_usd"))
        tx = a.get("transactions") or {}
        tx1 = tx.get("h1") or {}
        tx5 = tx.get("m5") or {}
        buys = _f(tx1.get("buys")) or 0.0
        sells = _f(tx1.get("sells")) or 0.0
        buys5 = _f(tx5.get("buys")) or 0.0
        sells5 = _f(tx5.get("sells")) or 0.0
        symbol = (a.get("name") or "?").split("/")[0].strip()

        created = a.get("pool_created_at")
        age_min = 10_000.0
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
            except ValueError:
                pass

        if price <= 0 or liq < self.s.risk.min_liquidity_usd:
            return None
        if pool.get("source") == "new_pools" and age_min > L.max_token_age_minutes:
            return None

        features: dict[str, Any] = {
            "liquidity_usd": round(liq, 2),
            "volume_24h": round(vol24, 2),
            "volume_1h": round(vol1, 2),
            "volume_5m": round(vol5, 2),
            "chg_5m": round(ch5, 2),
            "chg_1h": round(ch1, 2),
            "chg_6h": round(ch6, 2),
            "chg_24h": round(ch24, 2),
            "age_minutes": round(age_min, 1),
            "buys_1h": int(buys),
            "sells_1h": int(sells),
            "buys_5m": int(buys5),
            "sells_5m": int(sells5),
            "fdv": fdv,
            "pool": a.get("address"),
            "smart_money_inflow": round(smart_inflow, 2) if smart_inflow is not None else None,
        }

        score, reasons = self._score(features)
        if score < 0.40:
            return None

        features["score"] = round(score, 4)
        features["reasons"] = reasons
        self._seen[mint] = now

        return Signal(
            strategy="meme_momentum",
            symbol=f"{symbol}/USD",
            score=round(score, 4),
            venue=Venue.DEX,
            chain=chain,
            address=mint,
            price=price,
            features=features,
        )

    @staticmethod
    def _score(f: dict[str, Any]) -> tuple[float, list[str]]:
        reasons: list[str] = []
        s = 0.0

        liq = f["liquidity_usd"]
        if liq >= 300_000:
            s += 0.20
            reasons.append("likuiditas tebal")
        elif liq >= 120_000:
            s += 0.16
            reasons.append("likuiditas kuat")
        elif liq >= 60_000:
            s += 0.10
        elif liq >= 40_000:
            s += 0.06
        else:
            return 0.0, ["likuiditas tipis"]

        turn = f["volume_24h"] / liq if liq else 0
        if turn >= 4:
            s += 0.18
            reasons.append("perputaran tinggi")
        elif turn >= 1.5:
            s += 0.13
            reasons.append("volume sehat")
        elif turn < 0.5:
            s -= 0.12
            reasons.append("volume sepi")

        ch1 = f["chg_1h"]
        if 0 <= ch1 <= 20:
            s += 0.18
            reasons.append("momentum 1h sehat")
        elif 20 < ch1 <= 35:
            s += 0.08
            reasons.append("momentum lanjutan 1h")
        elif ch1 > 35:
            s -= 0.18
            reasons.append("overextended 1h")
        elif ch1 < 0:
            s -= 0.20
            reasons.append("momentum 1h negatif")

        if f["chg_6h"] > 0:
            s += 0.08
            reasons.append("tren 6h positif")
        elif f["chg_6h"] < -15:
            s -= 0.15
            reasons.append("tren 6h jatuh")

        buys, sells = f["buys_1h"], f["sells_1h"]
        if buys + sells >= 20:
            ratio = buys / max(sells, 1)
            if ratio >= 1.3:
                s += 0.15
                reasons.append("tekanan beli 1h dominan")
            elif ratio >= 1.1:
                s += 0.08
            elif ratio < 0.85:
                s -= 0.15
                reasons.append("tekanan jual 1h dominan")

        b5, s5 = f.get("buys_5m", 0), f.get("sells_5m", 0)
        if b5 + s5 >= 5:
            if b5 >= s5 * 1.1 and f.get("chg_5m", 0) >= 0:
                s += 0.10
                reasons.append("beli 5m aktif")
            elif b5 < s5 * 0.8:
                s -= 0.10
                reasons.append("jual 5m aktif")

        age = f["age_minutes"]
        if 30 <= age <= 360:
            s += 0.10
            reasons.append("early alpha")

        inflow = f.get("smart_money_inflow")
        if inflow is not None:
            if inflow > 500:
                s += 0.20
                reasons.append("smart money akumulasi")
            elif inflow > 0:
                s += 0.10
                reasons.append("smart money inflow")
            elif inflow < -300:
                s -= 0.22
                reasons.append("smart money distribusi")

        fdv = f.get("fdv")
        if fdv and liq:
            if fdv / liq > 45:
                s -= 0.15
                reasons.append("fdv jauh di atas likuiditas")

        return max(0.0, min(1.0, s)), reasons


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
