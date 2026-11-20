"""Whale tracking & follow system for Solana and DEX meme tokens.

Features:
1. Helius Dedicated RPC: Fast, reliable on-chain transaction monitoring without public 429s.
2. Candidate discovery: Finds high-conviction buyers from trending pools (top 5, with Anti-MEV filter).
3. Quality Standards & Auto-Pruning:
   - Filters out MEV bots (>8 tx/pool).
   - Tracks win/loss & PnL per whale.
   - Automatically removes/prunes whales that underperform (2 consecutive losses or negative PnL).
   - Automatically prunes idle whales (>24h without activity).
4. Hourly Scanner: Runs auto-discovery once every 1 hour, picking top 5 quality candidates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from gembul.db import Database
from gembul.settings import Settings

log = logging.getLogger(__name__)

GT_API = "https://api.geckoterminal.com/api/v2"
DEXS_API = "https://api.dexscreener.com/latest/dex/tokens"

IGNORED_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


class WhaleTracker:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.s = settings
        self._http = httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": "gembul/1.0", "Accept": "application/json"},
        )
        self._pool_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._last_auto_scan_ts: float = 0.0

    async def init_db(self) -> None:
        # Columns wins, losses, pnl_usd, last_active_at are ensured via Database.connect()
        pass

    async def close(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- discovery

    async def scan_candidates(
        self,
        chain: str = "solana",
        min_usd: float = 1000.0,
        top_pools: int = 3,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Scans recent trades in trending pools to detect active large buyers with anti-MEV filter."""
        # 60-second cache cushion to prevent GeckoTerminal 429
        cache_key = f"{chain}:{page}:{top_pools}"
        now = time.time()
        if cache_key in self._pool_cache:
            ts, cached = self._pool_cache[cache_key]
            if now - ts < 60.0:
                return cached

        try:
            r = await self._http.get(
                f"{GT_API}/networks/{chain}/trending_pools",
                params={"page": page},
            )
            if r.status_code == 429:
                log.warning("GeckoTerminal 429 saat scan trending pools, memakai cache/skip")
                return self._pool_cache.get(cache_key, (0, []))[1]
            if r.status_code != 200:
                return []
            pools = (r.json() or {}).get("data") or []
        except Exception as exc:
            log.warning("gagal fetch trending pools hal %d: %s", page, exc)
            return []

        candidates: dict[str, dict[str, Any]] = {}

        for p in pools[:top_pools]:
            attr = p.get("attributes") or {}
            pool_addr = attr.get("address")
            pool_name = attr.get("name") or "?"
            if not pool_addr:
                continue

            await asyncio.sleep(0.5)  # rate limit cushion
            try:
                tr_resp = await self._http.get(
                    f"{GT_API}/networks/{chain}/pools/{pool_addr}/trades"
                )
                if tr_resp.status_code == 429:
                    log.debug("trades endpoint 429 for pool %s", pool_addr[:8])
                    continue
                if tr_resp.status_code != 200:
                    continue
                trades = (tr_resp.json() or {}).get("data") or []
            except Exception:
                continue

            for t in trades:
                t_attr = t.get("attributes") or {}
                if t_attr.get("kind") != "buy":
                    continue
                vol = float(t_attr.get("volume_in_usd") or 0)
                if vol < min_usd:
                    continue
                buyer = t_attr.get("tx_from_address")
                if not buyer or len(buyer) < 30:
                    continue

                entry = candidates.setdefault(
                    buyer,
                    {
                        "wallet": buyer,
                        "chain": chain,
                        "buys": 0,
                        "total_usd": 0.0,
                        "pools": set(),
                        "last_tx": t_attr.get("tx_hash"),
                        "last_time": t_attr.get("block_timestamp"),
                    },
                )
                entry["buys"] += 1
                entry["total_usd"] += vol
                entry["pools"].add(pool_name)

        # Anti-MEV Filter: Real smart money trades 1-6 times in a single pool window.
        # Wallets spamming > 8 buys are high-frequency MEV/arbitrage bots.
        filtered = [c for c in candidates.values() if 1 <= c["buys"] <= 8]
        ranked = sorted(filtered, key=lambda x: x["total_usd"], reverse=True)
        for c in ranked:
            c["pools"] = list(c["pools"])
            c["total_usd"] = round(c["total_usd"], 2)

        top_five = ranked[:5]
        self._pool_cache[cache_key] = (now, top_five)
        return top_five

    # ---------------------------------------------------------- tracking CRUD

    async def add_whale(
        self,
        address: str,
        chain: str = "solana",
        label: str = "",
        auto_copy: bool = True,
        copy_usd: float = 100.0,
    ) -> dict[str, Any]:
        await self.init_db()
        addr = address.strip()
        tag = (label or f"Whale-{addr[:4]}").strip()

        last_sig = await self._get_latest_sig(chain, addr)

        await self.db.execute(
            "INSERT INTO tracked_whales(chain, address, label, auto_copy, copy_usd, last_sig, last_active_at) "
            "VALUES(?,?,?,?,?,?, datetime('now')) "
            "ON CONFLICT(chain, address) DO UPDATE SET "
            "label=excluded.label, auto_copy=excluded.auto_copy, copy_usd=excluded.copy_usd, last_active_at=datetime('now')",
            (chain, addr, tag, 1 if auto_copy else 0, copy_usd, last_sig),
        )
        row = await self.db.fetchone(
            "SELECT * FROM tracked_whales WHERE chain=? AND address=?", (chain, addr)
        )
        return dict(row) if row else {"address": addr, "label": tag}

    async def get_whale(self, whale_id: int) -> dict[str, Any] | None:
        await self.init_db()
        row = await self.db.fetchone("SELECT * FROM tracked_whales WHERE id=?", (whale_id,))
        return dict(row) if row else None

    async def set_copy_usd(self, whale_id: int, usd: float) -> dict[str, Any] | None:
        await self.init_db()
        await self.db.execute(
            "UPDATE tracked_whales SET copy_usd=? WHERE id=?", (usd, whale_id)
        )
        return await self.get_whale(whale_id)

    async def remove_whale(self, id_or_address: int | str) -> bool:
        await self.init_db()
        if isinstance(id_or_address, int) or str(id_or_address).isdigit():
            res = await self.db.execute(
                "DELETE FROM tracked_whales WHERE id=?", (int(id_or_address),)
            )
        else:
            res = await self.db.execute(
                "DELETE FROM tracked_whales WHERE address=?", (str(id_or_address).strip(),)
            )
        return res > 0

    async def list_whales(self, chain: str | None = None) -> list[dict[str, Any]]:
        await self.init_db()
        if chain:
            rows = await self.db.fetchall(
                "SELECT * FROM tracked_whales WHERE chain=? ORDER BY id DESC", (chain,)
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM tracked_whales ORDER BY id DESC")
        return [dict(r) for r in rows]

    async def toggle_whale(self, whale_id: int) -> dict[str, Any] | None:
        await self.init_db()
        row = await self.db.fetchone("SELECT * FROM tracked_whales WHERE id=?", (whale_id,))
        if not row:
            return None
        new_val = 0 if row["auto_copy"] else 1
        await self.db.execute(
            "UPDATE tracked_whales SET auto_copy=? WHERE id=?", (new_val, whale_id)
        )
        updated = await self.db.fetchone("SELECT * FROM tracked_whales WHERE id=?", (whale_id,))
        return dict(updated) if updated else None

    # ---------------------------------------------------- quality standard & prune

    async def record_whale_trade_result(
        self, address: str, pnl_usd: float
    ) -> dict[str, Any] | None:
        """Updates whale stats after a trade closes. Automatically prunes underperforming whales."""
        await self.init_db()
        row = await self.db.fetchone(
            "SELECT * FROM tracked_whales WHERE address=?", (address,)
        )
        if not row:
            return None

        w_id = row["id"]
        won = pnl_usd > 0
        new_wins = int(row["wins"] or 0) + (1 if won else 0)
        new_losses = int(row["losses"] or 0) + (0 if won else 1)
        new_pnl = round(float(row["pnl_usd"] or 0.0) + pnl_usd, 2)

        await self.db.execute(
            "UPDATE tracked_whales SET wins=?, losses=?, pnl_usd=?, last_active_at=datetime('now') WHERE id=?",
            (new_wins, new_losses, new_pnl, w_id),
        )

        total = new_wins + new_losses
        label = row["label"] or f"Whale-{address[:4]}"

        # Standard Evaluasi Kualitas Whale:
        # 1. Jika 2 trade berturut-turut rugi (0 menang, 2 kalah)
        # 2. Atau total pnl kumulatif minus <= -$15
        # 3. Atau sudah >= 3 trade tapi winrate < 40%
        should_prune = False
        reason = ""

        if new_losses >= 2 and new_wins == 0:
            should_prune = True
            reason = f"2 trade berturut-turut rugi (${new_pnl:+.2f})"
        elif new_pnl <= -15.0:
            should_prune = True
            reason = f"akumulasi PnL rugi melebihi batas (${new_pnl:+.2f})"
        elif total >= 3 and (new_wins / total) < 0.40:
            should_prune = True
            reason = f"win rate buruk ({new_wins}/{total} · {new_wins/total*100:.0f}%)"

        if should_prune:
            await self.remove_whale(w_id)
            log.info("WHALE PRUNED: %s (%s) dihapus otomatis: %s", label, address[:8], reason)
            return {
                "pruned": True,
                "label": label,
                "address": address,
                "pnl_usd": new_pnl,
                "reason": reason,
                "wins": new_wins,
                "losses": new_losses,
            }

        return {
            "pruned": False,
            "label": label,
            "address": address,
            "pnl_usd": new_pnl,
            "wins": new_wins,
            "losses": new_losses,
        }

    async def prune_idle_whales(self, max_idle_hours: int = 24) -> list[str]:
        """Removes whales that have had zero trade activity for over 24 hours to conserve credits."""
        await self.init_db()
        sql = f"SELECT id, label, address FROM tracked_whales WHERE last_active_at <= datetime('now', '-{max_idle_hours} hours')"
        rows = await self.db.fetchall(sql)
        pruned_labels = []
        for r in rows:
            await self.remove_whale(r["id"])
            pruned_labels.append(r["label"] or r["address"][:8])
            log.info("IDLE WHALE PRUNED: %s (> %d jam tidak aktif)", r["label"], max_idle_hours)
        return pruned_labels

    async def auto_refresh_whales_1h(self) -> list[dict[str, Any]]:
        """Periodic 1-hour scan: prunes idle whales and adopts top 5 fresh candidates if slots open."""
        await self.init_db()
        # 1. Prune idle
        await self.prune_idle_whales(max_idle_hours=24)

        current = await self.list_whales(chain="solana")
        active_count = sum(1 for w in current if w.get("auto_copy"))

        # If we already have 5 active high-quality whales, don't spam scan
        if active_count >= 5:
            return []

        needed = max(1, 5 - active_count)
        candidates = await self.scan_candidates(chain="solana", min_usd=1000.0, top_pools=3, page=1)
        if not candidates:
            return []

        existing_addrs = {w["address"] for w in current}
        added: list[dict[str, Any]] = []

        for c in candidates:
            addr = c["wallet"]
            if addr in existing_addrs:
                continue
            new_w = await self.add_whale(
                address=addr,
                chain="solana",
                label=f"SmartWhale-{addr[:4]}",
                auto_copy=True,
                copy_usd=100.0,
            )
            added.append(new_w)
            if len(added) >= needed:
                break

        return added

    # ---------------------------------------------------- token inspection

    async def get_whale_recent_tokens(
        self, address: str, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Scans on-chain transactions of a whale to find which tokens they bought recently."""
        try:
            sigs_data = await self._rpc("getSignaturesForAddress", [address, {"limit": 8}])
            sigs = (sigs_data or {}).get("result") or []
        except Exception as exc:
            log.debug("getSignaturesForAddress error: %s", exc)
            return []

        seen_mints: set[str] = set()
        tokens: list[dict[str, Any]] = []

        for s in sigs:
            sig = s.get("signature")
            if not sig:
                continue
            bought_mint, _ = await self._parse_solana_tx_buy(sig, address)
            if bought_mint and bought_mint not in seen_mints:
                seen_mints.add(bought_mint)
                info = await self._get_token_info(bought_mint)
                tokens.append({
                    "mint": bought_mint,
                    "sig": sig,
                    **info,
                })
                if len(tokens) >= limit:
                    break

        return tokens

    async def scan_all_whales_tokens(self, limit_per_whale: int = 2) -> list[dict[str, Any]]:
        """Collects recent tokens bought across all tracked whales in the portfolio."""
        whales = await self.list_whales()
        if not whales:
            return []

        seen_mints: set[str] = set()
        all_tokens: list[dict[str, Any]] = []

        for w in whales[:5]:  # only check top 5 to save credits
            if w.get("chain") != "solana":
                continue
            tokens = await self.get_whale_recent_tokens(w["address"], limit=limit_per_whale)
            for t in tokens:
                mint = t["mint"]
                if mint not in seen_mints:
                    seen_mints.add(mint)
                    t["whale_label"] = w.get("label") or f"Whale-{w['id']}"
                    t["whale_address"] = w["address"]
                    all_tokens.append(t)

        return all_tokens

    # ------------------------------------------------------------- onchain poll (Helius)

    async def poll_new_buys(self) -> list[dict[str, Any]]:
        """Polls top 5 tracked Solana wallets for new token purchase transactions economically."""
        await self.init_db()
        whales = await self.list_whales(chain="solana")
        if not whales:
            return []

        events: list[dict[str, Any]] = []

        # Strictly poll only up to 5 active whales to respect the 100k daily credits
        for w in whales[:5]:
            if not w.get("auto_copy"):
                continue

            addr = w["address"]
            last_sig = w.get("last_sig") or ""

            try:
                # 1 credit: fetch single latest signature
                sigs = await self._rpc("getSignaturesForAddress", [addr, {"limit": 1}])
            except Exception as exc:
                log.debug("getSignaturesForAddress %s error: %s", addr[:8], exc)
                continue

            res = (sigs or {}).get("result") or []
            if not res:
                continue

            new_sig = res[0].get("signature")
            if not new_sig or new_sig == last_sig:
                continue

            # First time seeing this wallet? Set baseline
            if not last_sig:
                await self.db.execute(
                    "UPDATE tracked_whales SET last_sig=?, last_active_at=datetime('now') WHERE id=?",
                    (new_sig, w["id"]),
                )
                continue

            # Signature changed! Now inspect transaction (Helius Enhanced / getTransaction)
            bought_mint, delta = await self._parse_solana_tx_buy(new_sig, addr)
            await self.db.execute(
                "UPDATE tracked_whales SET last_sig=?, total_trades=total_trades+1, last_active_at=datetime('now') WHERE id=?",
                (new_sig, w["id"]),
            )

            if not bought_mint:
                continue

            token_info = await self._get_token_info(bought_mint)
            events.append({
                "whale_id": w["id"],
                "whale_address": addr,
                "label": w.get("label") or f"Whale-{addr[:4]}",
                "auto_copy": bool(w.get("auto_copy")),
                "copy_usd": float(w.get("copy_usd") or 100.0),
                "chain": "solana",
                "mint": bought_mint,
                "symbol": token_info.get("symbol") or f"{bought_mint[:6].upper()}/USD",
                "name": token_info.get("name") or "Unknown Token",
                "price": token_info.get("price") or 0.0,
                "market_cap": token_info.get("market_cap") or 0.0,
                "liquidity": token_info.get("liquidity") or 0.0,
                "chg_1h": token_info.get("chg_1h") or 0.0,
                "tx_hash": new_sig,
                "token_qty": delta,
            })

        return events

    # ------------------------------------------------------------- internal helpers

    async def _rpc(self, method: str, params: list[Any]) -> dict[str, Any]:
        rpc_url = self.s.venues.solana_rpc or "https://api.mainnet-beta.solana.com"
        resp = await self._http.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        return resp.json()

    async def _get_latest_sig(self, chain: str, address: str) -> str:
        if chain != "solana":
            return ""
        try:
            data = await self._rpc("getSignaturesForAddress", [address, {"limit": 1}])
            res = (data or {}).get("result") or []
            return res[0]["signature"] if res else ""
        except Exception:
            return ""

    async def _parse_solana_tx_buy(self, sig: str, wallet: str) -> tuple[str | None, float]:
        """Detects whether a transaction resulted in positive token balance for a non-native mint."""
        try:
            tx_data = await self._rpc(
                "getTransaction",
                [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
            )
            r = (tx_data or {}).get("result") or {}
            meta = r.get("meta") or {}
            if meta.get("err") is not None:
                return None, 0.0

            pre_map: dict[tuple[int, str], float] = {}
            for b in meta.get("preTokenBalances") or []:
                idx = b.get("accountIndex")
                mint = b.get("mint")
                amt = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
                if idx is not None and mint:
                    pre_map[(idx, mint)] = amt

            for b in meta.get("postTokenBalances") or []:
                idx = b.get("accountIndex")
                mint = b.get("mint")
                if not idx or not mint or mint in IGNORED_MINTS:
                    continue
                post_amt = float(b.get("uiTokenAmount", {}).get("uiAmount") or 0)
                pre_amt = pre_map.get((idx, mint), 0.0)
                delta = post_amt - pre_amt
                if delta > 0:
                    return mint, delta

            return None, 0.0
        except Exception as exc:
            log.debug("parse_solana_tx_buy gagal: %s", exc)
            return None, 0.0

    async def _get_token_info(self, mint: str) -> dict[str, Any]:
        try:
            r = await self._http.get(f"{DEXS_API}/{mint}")
            if r.status_code == 200:
                pairs = (r.json() or {}).get("pairs") or []
                if pairs:
                    p = pairs[0]
                    base = p.get("baseToken") or {}
                    sym = base.get("symbol") or "TOKEN"
                    px = float(p.get("priceUsd") or 0.0)
                    mc = float(p.get("marketCap") or p.get("fdv") or 0.0)
                    liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
                    chg_1h = float((p.get("priceChange") or {}).get("h1") or 0.0)
                    chg_24h = float((p.get("priceChange") or {}).get("h24") or 0.0)
                    return {
                        "symbol": f"{sym.upper()}/USD",
                        "name": base.get("name") or sym,
                        "price": px,
                        "market_cap": mc,
                        "liquidity": liq,
                        "chg_1h": chg_1h,
                        "chg_24h": chg_24h,
                        "dex_url": p.get("url") or f"https://dexscreener.com/solana/{mint}",
                        "gmgn_url": f"https://gmgn.ai/sol/token/{mint}",
                    }
        except Exception:
            pass
        return {
            "symbol": f"{mint[:6].upper()}/USD",
            "name": "Token",
            "price": 0.0,
            "market_cap": 0.0,
            "liquidity": 0.0,
            "chg_1h": 0.0,
            "chg_24h": 0.0,
            "dex_url": f"https://dexscreener.com/solana/{mint}",
            "gmgn_url": f"https://gmgn.ai/sol/token/{mint}",
        }
