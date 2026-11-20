"""Solana DEX execution through Jupiter."""
from __future__ import annotations

import logging

import httpx

from gembul.core.models import Fill, OrderIntent, Side, Venue
from gembul.settings import Settings
from gembul.venues.dex.chains import Chain
from gembul.venues.dex.wallet import SolanaSigner, SigningError

log = logging.getLogger(__name__)


class SolanaDex:
    def __init__(self, chain: Chain, settings: Settings, signer: SolanaSigner | None) -> None:
        self.chain = chain
        self.s = settings
        self.signer = signer
        self.api = settings.venues.jupiter_api.rstrip("/")
        self._http = httpx.AsyncClient(timeout=45.0)
        self._dec_cache: dict[str, int] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- price

    async def price(self, mint: str, amount_usd: float | None = None) -> float:
        """Price by most-liquid Solana pool; optionally return a size-aware executable average quote."""
        if mint == self.chain.usdc:
            return 1.0
        try:
            r = await self._http.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
            if r.status_code == 200:
                pairs = (r.json() or {}).get("pairs") or []
                pairs = [p for p in pairs if p.get("chainId") == "solana"]
                pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0), reverse=True)
                if pairs:
                    px = float(pairs[0].get("priceUsd") or 0.0)
                    if px > 0:
                        if amount_usd:
                            quote = await self.quote(self.chain.usdc, mint, int(amount_usd * 1e6), slippage_bps=50)
                            out = float(quote.get("outAmount") or 0) / (10 ** await self.decimals(mint))
                            if out > 0:
                                return float(amount_usd) / out
                        return px
        except Exception as exc:
            if amount_usd:
                raise RuntimeError(f"quote sized gagal untuk {mint[:8]}: {exc}") from exc

        if amount_usd:
            quote = await self.quote(self.chain.usdc, mint, int(amount_usd * 1e6), slippage_bps=50)
            out = float(quote.get("outAmount") or 0) / (10 ** await self.decimals(mint))
            if out > 0:
                return float(amount_usd) / out
            raise RuntimeError("quote sized Jupiter tidak valid")

        decimals = await self.decimals(mint)
        q = await self.quote(self.chain.usdc, mint, 1_000_000, slippage_bps=50)
        out = float(q.get("outAmount") or 0)
        if out <= 0:
            raise RuntimeError("jupiter tidak memberi harga")
        return (1.0 / out) * 10 ** decimals

    async def decimals(self, mint: str) -> int:
        if mint in self._dec_cache:
            return self._dec_cache[mint]
        try:
            resp = await self._http.post(self.chain.rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTokenSupply",
                "params": [mint],
            })
            data = resp.json().get("result", {}).get("value") or {}
            dec = data.get("decimals")
            if dec is not None:
                dec = int(dec)
                self._dec_cache[mint] = dec
                return dec
        except Exception:
            pass
        return 6

    async def balance(self, mint: str) -> int:
        if not self.signer:
            return 0
        resp = await self._http.post(self.chain.rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [
                self.signer.address,
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        })
        total = 0
        for acc in resp.json().get("result", {}).get("value", []):
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += int(float(amt["amount"]))
        return total

    # ---------------------------------------------------------------- quote

    async def quote(self, input_mint: str, output_mint: str, amount: int,
                    slippage_bps: int = 150) -> dict:
        r = await self._http.get(f"{self.api}/quote", params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": slippage_bps,
            "restrictIntermediateTokens": "true",
        })
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"jupiter quote: {data['error']}")
        return data

    # ---------------------------------------------------------------- swap

    async def execute(self, intent: OrderIntent) -> Fill:
        assert intent.address, "alamat mint wajib"

        mint = intent.address
        slippage = min(intent.slippage_bps, self.s.risk.max_slippage_bps)

        if self.s.live:
            if intent.side is Side.BUY:
                amount = int(intent.usd_amount * 1e6)  # USDC 6 desimal
                quote = await self.quote(self.chain.usdc, mint, amount, slippage)
            else:
                if not self.signer:
                    raise SigningError("jual butuh signer solana")
                bal = await self.balance(mint)
                if bal <= 0:
                    raise RuntimeError("saldo token kosong")
                quote = await self.quote(mint, self.chain.usdc, bal, slippage)

            if not self.signer:
                raise SigningError("signer solana tidak dikonfigurasi")
            swap = await self._http.post(f"{self.api}/swap", json={
                "quoteResponse": quote,
                "userPublicKey": self.signer.address,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            })
            swap.raise_for_status()
            sig = await self.signer.sign_and_send(swap.json()["swapTransaction"])
            ok = await self.signer.confirm(sig)
            if not ok:
                raise RuntimeError(f"transaksi solana gagal konfirmasi: {sig}")

            in_amt = float(quote["inAmount"])
            out_amt = float(quote["outAmount"])
            in_dec = 6 if intent.side is Side.BUY else await self.decimals(mint)
            out_dec = await self.decimals(mint) if intent.side is Side.BUY else 6
            in_human = in_amt / 10 ** in_dec
            out_human = out_amt / 10 ** out_dec
            price = in_human / out_human if out_human > 0 else 0.0
        else:
            # Paper execution: always resolve exact live price from DexScreener/RPC
            sig = "paper-" + str(abs(hash(mint + str(intent.usd_amount))) % 10**12)
            price = await self.price(mint)
            if price <= 0:
                price = 0.00001
            out_human = (intent.usd_amount / price) if price > 0 else 0.0
            log.info("[PAPER] solana swap %s px=$%.8f qty=%.2f", mint[:8], price, out_human)

        return Fill(qty=out_human, price=price, tx_hash=sig, venue=Venue.DEX)
