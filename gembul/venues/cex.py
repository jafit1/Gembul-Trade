"""CEX venue built directly on ccxt async.

Used for spot/margin entries the freqtrade strategy cannot express, and as the
price feed for everything quoted in the base currency.
"""
from __future__ import annotations

import logging

import ccxt.async_support as ccxt

from gembul.core.models import Fill, OrderIntent, Side, Venue
from gembul.settings import Settings

log = logging.getLogger(__name__)


class CexVenue:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.live = settings.mode == "live" and settings.real_trading_enabled
        self.ex: ccxt.Exchange | None = None
        self._markets_loaded = False

    async def start(self) -> None:
        klass = getattr(ccxt, self.s.cex_exchange)
        opts = {
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "adjustForTimeDifference": True},
        }
        if self.s.cex_api_key:
            opts.update({
                "apiKey": self.s.cex_api_key,
                "secret": self.s.cex_secret,
            })
            if self.s.cex_password:
                opts["password"] = self.s.cex_password
        self.ex = klass(opts)
        await self.ex.load_markets()
        self._markets_loaded = True
        log.info("cex siap: %s (%d market)", self.ex.id, len(self.ex.markets))

    async def close(self) -> None:
        if self.ex:
            await self.ex.close()

    async def price(self, symbol: str) -> float:
        assert self.ex
        t = await self.ex.fetch_ticker(symbol)
        return float(t["last"])

    async def prices(self, symbols: list[str]) -> dict[str, float]:
        assert self.ex
        out: dict[str, float] = {}
        for sym in symbols:
            try:
                out[sym] = await self.price(sym)
            except Exception as exc:
                log.debug("harga %s gagal: %s", sym, exc)
        return out

    async def balance(self, currency: str) -> float:
        if not self.live:
            return 0.0
        assert self.ex
        bal = await self.ex.fetch_balance()
        return float(bal.get("free", {}).get(currency, 0.0))

    async def execute(self, intent: OrderIntent) -> Fill:
        if not self.live:
            px = intent.limit_price or await self.price(intent.symbol)
            qty = intent.usd_amount / px
            log.info("[PAPER] cex %s %s %.6f @ %.6f", intent.side, intent.symbol, qty, px)
            return Fill(qty=qty, price=px, venue=Venue.CEX)

        assert self.ex
        px = await self.price(intent.symbol)
        book = await self.ex.fetch_order_book(intent.symbol, limit=5)
        if intent.side is Side.BUY:
            ref = float(book["asks"][0][0])
            worst = ref * (1 + intent.slippage_bps / 10_000)
            if px > worst:
                raise RuntimeError(f"slippage terlalu besar pada {intent.symbol}")
            amount = intent.usd_amount / ref
            order = await self.ex.create_order(intent.symbol, "market", "buy", amount)
        else:
            amount = intent.usd_amount / px
            order = await self.ex.create_order(intent.symbol, "market", "sell", amount)

        filled = float(order.get("filled") or 0) or amount
        avg = float(order.get("average") or order.get("price") or px)
        fee = 0.0
        if order.get("fee") and order["fee"].get("cost"):
            fee = float(order["fee"]["cost"])
        return Fill(qty=filled, price=avg, fee_usd=fee, venue=Venue.CEX,
                    tx_hash=str(order.get("id", "")))
