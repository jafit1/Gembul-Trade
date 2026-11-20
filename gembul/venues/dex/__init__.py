"""DEX factory: one object per chain, lazily created."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from gembul.core.models import Fill, OrderIntent
from gembul.settings import Settings
from gembul.venues.dex.chains import CHAINS, Chain, get_chain
from gembul.venues.dex.evm import EvmDex
from gembul.venues.dex.solana import SolanaDex
from gembul.venues.dex.wallet import EvmSigner, SolanaSigner, SigningError

log = logging.getLogger(__name__)


class DexRouter:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._venues: dict[str, Any] = {}
        self._signers: dict[str, Any] = {}
        self._http = httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "gembul/1.0"})

    def signer(self, chain: Chain) -> Any:
        if chain.name in self._signers:
            return self._signers[chain.name]

        if not self.s.live:
            self._signers[chain.name] = None
            return None

        try:
            if chain.kind == "svm":
                sig: Any = SolanaSigner(self.s.solana_private_key, chain.rpc)
            else:
                sig = EvmSigner(self.s.evm_private_key, chain.rpc)
        except SigningError as exc:
            log.warning("signer %s tidak aktif: %s", chain.name, exc)
            sig = None
        self._signers[chain.name] = sig
        return sig

    def venue(self, chain_name: str) -> Any:
        chain = get_chain(chain_name)
        if chain.name in self._venues:
            return self._venues[chain.name]
        sig = self.signer(chain)
        if chain.kind == "svm":
            v: Any = SolanaDex(chain, self.s, sig)
        else:
            v = EvmDex(chain, self.s, sig)
        self._venues[chain.name] = v
        return v

    async def price(self, chain_name: str, address: str, amount_usd: float | None = None) -> float:
        """Get token USD price from the most liquid pool, optionally matching trade size.

        When amount_usd is given, use DexScreener only as a pool selector. The returned
        price is an executable Jupiter quote at that size, not a chart mid-price.
        """
        try:
            r = await self._http.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
            if r.status_code == 200:
                pairs = (r.json() or {}).get("pairs") or []
                if pairs:
                    if chain_name == "solana":
                        pairs = [p for p in pairs if p.get("chainId") == "solana"]
                    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0), reverse=True)
                    px = float(pairs[0].get("priceUsd") or 0.0)
                    if px > 0:
                        if amount_usd and chain_name == "solana":
                            try:
                                venue = self.venue(chain_name)
                                decimals = await venue.decimals(address)
                                raw_amount = int(amount_usd * 10**6)
                                quote = await venue.quote(
                                    get_chain(chain_name).usdc, address, raw_amount, slippage_bps=50
                                )
                                got = float(quote.get("outAmount") or 0) / 10**decimals
                                if got > 0:
                                    # This is the position-size-weighted executable average price.
                                    return float(amount_usd) / got
                            except Exception as exc:
                                log.debug("size-aware quote failed for %s: %s", address[:8], exc)
                        return px
        except Exception as exc:
            log.debug("dexscreener price fetch error for %s: %s", address[:8], exc)

        # Never fall back to a fake/stale mark when the caller asks for an executable-size quote.
        if amount_usd:
            raise RuntimeError(f"tidak ada quote eksekusi yang valid untuk {address[:8]}")
        return await self.venue(chain_name).price(address)

    async def sell_price(self, chain_name: str, address: str, token_qty: float) -> float:
        """Executable average sell price for the whole token quantity, after route fees/impact."""
        venue = self.venue(chain_name)
        decimals = await venue.decimals(address)
        raw_qty = int(token_qty * (10 ** decimals))
        if raw_qty <= 0:
            raise RuntimeError("jumlah token tidak valid untuk quote jual")
        quote = await venue.quote(address, get_chain(chain_name).usdc, raw_qty, slippage_bps=50)
        usdc_out = float(quote.get("outAmount") or 0) / 1e6
        if usdc_out <= 0:
            raise RuntimeError("quote jual tidak valid")
        return usdc_out / token_qty

    async def execute(self, intent: OrderIntent) -> Fill:
        return await self.venue(intent.chain or "solana").execute(intent)

    async def decimals(self, chain_name: str, address: str) -> int:
        return await self.venue(chain_name).decimals(address)

    async def balance(self, chain_name: str, address: str) -> float:
        v = self.venue(chain_name)
        raw = await v.balance(address)
        dec = await v.decimals(address)
        return raw / 10 ** dec

    async def native_balance(self, chain_name: str) -> float:
        sig = self.signer(get_chain(chain_name))
        if sig is None:
            return 0.0
        try:
            return await sig.balance_native()
        except Exception:
            return 0.0

    async def close(self) -> None:
        await self._http.aclose()
        for v in self._venues.values():
            await v.close()
        for sig in self._signers.values():
            close = getattr(sig, "close", None)
            if close:
                await close()

    @property
    def chains(self) -> list[str]:
        return list(CHAINS)
