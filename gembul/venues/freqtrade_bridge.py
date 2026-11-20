"""Bridge to a running freqtrade instance.

Freqtrade does the heavy backtesting and the CEX strategy execution; this class
lets our engine see and steer it over the REST API, so the same risk engine and
the same memory apply to both sides.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from gembul.settings import Settings

log = logging.getLogger(__name__)


class FreqtradeBridge:
    def __init__(self, settings: Settings) -> None:
        v = settings.venues
        self.base = v.freqtrade_url.rstrip("/")
        self.auth = (v.freqtrade_user, v.freqtrade_password)
        self._http = httpx.AsyncClient(timeout=20.0, auth=self.auth)
        self.enabled = settings.cex.use_freqtrade
        self.online = False

    async def close(self) -> None:
        await self._http.aclose()

    async def ping(self) -> bool:
        if not self.enabled:
            return False
        try:
            r = await self._http.get(f"{self.base}/api/v1/ping")
            self.online = r.status_code == 200
        except Exception:
            self.online = False
        return self.online

    async def _get(self, path: str) -> Any:
        r = await self._http.get(f"{self.base}{path}")
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, payload: dict) -> Any:
        r = await self._http.post(f"{self.base}{path}", json=payload)
        r.raise_for_status()
        return r.json()

    async def profit(self) -> dict:
        return await self._get("/api/v1/profit")

    async def open_trades(self) -> list[dict]:
        data = await self._get("/api/v1/status")
        if isinstance(data, list):
            return data
        return data.get("trades", [])

    async def closed_trades(self, limit: int = 10) -> list[dict]:
        data = await self._get(f"/api/v1/trades?limit={limit}")
        if isinstance(data, list):
            return data
        return data.get("trades", [])

    async def balance(self) -> dict:
        return await self._get("/api/v1/balance")

    async def performance(self) -> dict:
        return await self._get("/api/v1/performance")

    async def force_enter(self, pair: str, side: str = "long") -> dict:
        return await self._post("/api/v1/forceenter", {
            "pair": pair, "side": side, "entry_tag": "gembul",
            "entry_type": "market",
        })

    async def force_exit(self, trade_id: int) -> dict:
        return await self._post("/api/v1/forceexit", {
            "tradeid": str(trade_id), "exit_type": "market",
        })

    async def pause(self) -> dict:
        return await self._post("/api/v1/pause", {})

    async def start(self) -> dict:
        return await self._post("/api/v1/start", {})

    async def whitelist(self) -> list[str]:
        data = await self._get("/api/v1/whitelist")
        return data.get("whitelist", [])

    async def summary(self) -> dict:
        if not await self.ping():
            return {"online": False}
        try:
            p = await self.profit()
            t = await self.open_trades()
            return {
                "online": True,
                "profit_closed_pct": p.get("profit_closed_percent", 0),
                "profit_all_pct": p.get("profit_all_percent", 0),
                "open_trades": len(t),
                "closed_trades": p.get("closed_trade_count", 0),
            }
        except Exception as exc:
            return {"online": False, "error": str(exc)}
