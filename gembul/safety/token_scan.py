"""Token safety gate for meme coins.

Sources, all free and keyless:
  goplus      - honeypot, taxes, mint authority, holder concentration, LP locks
  rugcheck    - solana risk score
  honeypot.is - EVM buy/sell simulation

A hard failure blocks the buy no matter how strong the signal was.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from gembul.db import Database
from gembul.settings import MemeLimits
from gembul.venues.dex.chains import Chain

log = logging.getLogger(__name__)

GOPLUS = "https://api.gopluslabs.io/api/v1"


@dataclass(slots=True)
class SafetyReport:
    ok: bool
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def block(self, reason: str) -> None:
        self.ok = False
        self.reasons.append(reason)


class TokenScanner:
    def __init__(self, db: Database, limits: MemeLimits) -> None:
        self.db = db
        self.limits = limits
        self._http = httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": "gembul/1.0"}
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def scan(self, chain: Chain, address: str) -> SafetyReport:
        rep = SafetyReport(ok=True)

        gp, rc, hp = await asyncio.gather(
            self._goplus(chain, address),
            self._rugcheck(address) if chain.kind == "svm" else _none(),
            self._honeypot(chain, address) if chain.kind == "evm" else _none(),
            return_exceptions=True,
        )

        penalty = 0.0

        if isinstance(gp, dict) and gp:
            rep.data["goplus"] = {k: gp.get(k) for k in _GP_KEYS if k in gp}
            judge = self._judge_solana if chain.kind == "svm" else self._judge_evm
            reasons, p = judge(gp, address=address) if chain.kind == "svm" else judge(gp)
            penalty += p
            for r in reasons:
                rep.block(r)

        if isinstance(rc, dict) and rc:
            norm = rc.get("score_normalised")
            rep.data["rugcheck"] = {"score": norm,
                                    "risks": [r.get("name") for r in (rc.get("risks") or [])]}
            if norm is not None:
                if norm >= 85:
                    rep.block(f"rugcheck risk {norm}")
                else:
                    penalty += norm / 400

        if isinstance(hp, dict) and hp:
            res = hp.get("honeypotResult") or {}
            if res.get("isHoneypot"):
                rep.block("honeypot")
            sim = hp.get("simulationResult") or {}
            bt, st = float(sim.get("buyTax") or 0), float(sim.get("sellTax") or 0)
            rep.data["honeypot"] = {"buy_tax": bt, "sell_tax": st}
            if bt > self.limits.max_buy_tax_pct:
                rep.block(f"buy tax {bt:.1f}%")
            if st > self.limits.max_sell_tax_pct:
                rep.block(f"sell tax {st:.1f}%")

        rep.score = round(max(0.0, 1.0 - penalty), 3)
        rep.ok = rep.ok and rep.score > 0.25

        await self.db.execute(
            "INSERT INTO tokens_seen(chain,address,score,verdict,payload) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chain,address) DO UPDATE SET last_seen=datetime('now'), "
            "score=excluded.score, verdict=excluded.verdict, payload=excluded.payload",
            (chain.name, address, rep.score, "ok" if rep.ok else "blocked",
             str(rep.reasons)[:2000]),
        )
        return rep

    # ---------------------------------------------------------------- goplus

    async def _goplus(self, chain: Chain, address: str) -> dict:
        if chain.kind == "svm":
            url = f"{GOPLUS}/solana/token_security"
        else:
            url = f"{GOPLUS}/token_security/{chain.goplus_id}"
        r = await self._http.get(url, params={"contract_addresses": address})
        if r.status_code != 200:
            return {}
        data = (r.json() or {}).get("result") or {}
        if not data:
            return {}
        return data.get(address) or next(iter(data.values()))

    def _judge_evm(self, d: dict) -> tuple[list[str], float]:
        L = self.limits
        reasons: list[str] = []
        p = 0.0

        def flag(key: str, label: str, pen: float) -> None:
            nonlocal p
            if str(d.get(key, "0")) == "1":
                reasons.append(label)
                p += pen

        if L.require_honeypot_pass:
            flag("is_honeypot", "honeypot", 1.0)
        if L.require_no_mint_authority:
            flag("is_mintable", "mint authority aktif", 0.6)
        flag("hidden_owner", "hidden owner", 0.5)
        flag("can_take_back_ownership", "ownership bisa diambil alih", 0.5)
        flag("selfdestruct", "selfdestruct", 0.7)
        flag("transfer_pausable", "transfer bisa dipause", 0.4)
        flag("is_blacklisted", "blacklist", 0.4)
        flag("slippage_modifiable", "slippage bisa diubah", 0.4)

        bt, st = _pct(d.get("buy_tax")), _pct(d.get("sell_tax"))
        if bt > L.max_buy_tax_pct:
            reasons.append(f"buy tax {bt:.1f}%")
            p += 0.5
        if st > L.max_sell_tax_pct:
            reasons.append(f"sell tax {st:.1f}%")
            p += 0.5

        holders = d.get("holders") or []
        if holders and len(holders) < 30:
            reasons.append(f"holder sedikit ({len(holders)})")
            p += 0.25

        top10 = sum(_pct(h.get("percent")) for h in holders[:10] if h.get("is_locked") != "1")
        if top10 > 65.0:
            reasons.append(f"top10 {top10:.1f}%")
            p += 0.3

        if L.require_lp_locked:
            lp = d.get("lp_holders") or []
            total = sum(_pct(h.get("percent")) for h in lp)
            locked = sum(
                _pct(h.get("percent")) for h in lp
                if str(h.get("is_locked")) == "1"
                or str(h.get("tag", "")).lower() in ("burn", "locked", "dead")
            )
            if total > 0 and (locked / total) < 0.25:
                reasons.append(f"LP terkunci {locked:.0f}%")
                p += 0.35

        liq = _f(d.get("liquidity"))
        if liq is not None and 0 < liq < 25_000:
            reasons.append(f"likuiditas ${liq:,.0f}")
            p += 0.3

        return reasons, min(p, 1.2)

    def _judge_solana(self, d: dict, address: str = "") -> tuple[list[str], float]:
        L = self.limits
        reasons: list[str] = []
        p = 0.0
        is_pump = address.lower().endswith("pump")

        def status(key: str) -> str:
            v = d.get(key)
            if isinstance(v, dict):
                return str(v.get("status", "0"))
            return str(v or "0")

        # Anti-Rugpull Guards:
        if L.require_no_mint_authority and status("mintable") == "1":
            reasons.append("mint authority aktif")
            p += 0.8

        if status("freezable") == "1":
            reasons.append("freeze authority aktif")
            p += 0.8

        if status("balance_mutable_authority") == "1":
            reasons.append("balance bisa diubah dev")
            p += 0.8

        if status("non_transferable") == "1":
            reasons.append("token tidak bisa ditransfer")
            p += 0.9

        if status("transfer_hook") == "1":
            reasons.append("transfer hook berbahaya")
            p += 0.6

        fee = d.get("transfer_fee")
        if isinstance(fee, dict) and str(fee.get("status", "0")) == "1":
            reasons.append("transfer fee aktif")
            p += 0.4

        count = int(d.get("holder_count") or 0)
        # Relaxed holder count (min 30, pump.fun tokens exempt)
        if count and count < 30 and not is_pump:
            reasons.append(f"holder sedikit ({count})")
            p += 0.25

        holders = d.get("holders") or []
        top10 = sum(_pct(h.get("percent")) for h in holders[:10]
                    if str(h.get("is_locked")) != "1")
        if top10 > 75.0 and not is_pump:
            reasons.append(f"top10 {top10:.1f}%")
            p += 0.3

        if L.require_lp_locked and not is_pump:
            lp = d.get("lp_holders") or []
            total = sum(_pct(h.get("percent")) for h in lp)
            locked = sum(
                _pct(h.get("percent")) for h in lp
                if str(h.get("is_locked")) == "1"
                or str(h.get("tag", "")).lower() in ("burn", "locked", "dead")
            )
            if total > 0 and (locked / total) < 0.25:
                reasons.append(f"LP terkunci {locked:.0f}%")
                p += 0.35

        return reasons, min(p, 1.2)

    # -------------------------------------------------------------- others

    async def _rugcheck(self, address: str) -> dict:
        r = await self._http.get(f"https://api.rugcheck.xyz/v1/tokens/{address}/report/summary")
        return r.json() if r.status_code == 200 else {}

    async def _honeypot(self, chain: Chain, address: str) -> dict:
        r = await self._http.get(
            "https://api.honeypot.is/v2/IsHoneypot",
            params={"address": address, "chainID": chain.chain_id},
        )
        return r.json() if r.status_code == 200 else {}


async def _none() -> dict:
    return {}


_GP_KEYS = (
    "is_honeypot", "buy_tax", "sell_tax", "is_mintable", "hidden_owner",
    "can_take_back_ownership", "selfdestruct", "transfer_pausable",
    "is_blacklisted", "slippage_modifiable", "holder_count", "liquidity",
    "mintable", "freezable", "closable", "balance_mutable_authority",
    "transfer_hook", "non_transferable", "transfer_fee", "lp_holders",
)


def _pct(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f * 100 if f <= 1.0 else f


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
