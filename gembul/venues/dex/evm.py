"""EVM DEX execution.

Router order: KyberSwap aggregator (keyless, works out of the box), then 0x if a
key is configured, then Odos. First one that returns a route wins.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from gembul.core.models import Fill, OrderIntent, Side, Venue
from gembul.settings import Settings
from gembul.venues.dex.chains import Chain
from gembul.venues.dex.wallet import EvmSigner, SigningError

log = logging.getLogger(__name__)

ERC20_DECIMALS = "0x313ce567"
ERC20_BALANCE = "0x70a08231"
KYBER = "https://aggregator-api.kyberswap.com"


class EvmDex:
    def __init__(self, chain: Chain, settings: Settings, signer: EvmSigner | None) -> None:
        self.chain = chain
        self.s = settings
        self.signer = signer
        self._http = httpx.AsyncClient(timeout=60.0)
        self._dec_cache: dict[str, int] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- helpers

    async def decimals(self, token: str) -> int:
        key = token.lower()
        if key == self.chain.usdc.lower():
            return 6
        if key in self._dec_cache:
            return self._dec_cache[key]
        try:
            resp = await self._http.post(self.chain.rpc, json={
                "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                "params": [{"to": token, "data": ERC20_DECIMALS}, "latest"],
            })
            hexv = resp.json().get("result", "0x")
            dec = int(hexv, 16) if hexv and hexv not in ("0x", "") else 18
        except Exception:
            dec = 18
        self._dec_cache[key] = dec
        return dec

    async def balance(self, token: str) -> int:
        if not self.signer:
            return 0
        data = ERC20_BALANCE + self.signer.address[2:].lower().rjust(64, "0")
        resp = await self._http.post(self.chain.rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": token, "data": data}, "latest"],
        })
        try:
            return int(resp.json().get("result", "0x0"), 16)
        except ValueError:
            return 0

    async def price(self, token: str) -> float:
        """Harga token dalam USDC dari kutipan 1 unit terkecil."""
        dec = await self.decimals(token)
        probe = 10 ** dec
        route = await self.route(self.chain.usdc, token, probe)
        out = float(route["amountOut"]) / 10 ** dec
        if out > 0:
            return 1.0 / out
        route = await self.route(token, self.chain.usdc, probe)
        return float(route["amountOut"]) / 1e6

    # ---------------------------------------------------------------- routing

    async def route(self, sell: str, buy: str, amount: int) -> dict:
        errors: list[str] = []
        for name, fn in (
            ("kyber", self._route_kyber),
            ("0x", self._route_0x),
            ("odos", self._route_odos),
        ):
            try:
                r = await fn(sell, buy, amount)
                if r and r.get("amountOut"):
                    r["router"] = name
                    return r
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError("semua router gagal | " + " | ".join(errors))

    async def _route_kyber(self, sell: str, buy: str, amount: int) -> dict:
        if not self.chain.kyber:
            raise RuntimeError("chain tidak didukung kyber")
        r = await self._http.get(
            f"{KYBER}/{self.chain.kyber}/api/v1/routes",
            params={
                "tokenIn": sell, "tokenOut": buy, "amountIn": str(amount),
                "origin": self.signer.address if self.signer else
                          "0x0000000000000000000000000000000000000000",
            },
            headers={"x-client-id": "gembul"},
        )
        if r.status_code != 200:
            raise RuntimeError(f"kyber {r.status_code}")
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(data.get("message", "kyber error"))
        summary = data["data"]["routeSummary"]
        return {
            "amountOut": summary["amountOut"],
            "summary": summary,
            "routerAddress": data["data"].get("routerAddress"),
            "gas": summary.get("gas"),
        }

    async def _route_0x(self, sell: str, buy: str, amount: int) -> dict:
        if not self.s.venues.zerox_key:
            raise RuntimeError("0x key kosong")
        if not self.signer:
            raise RuntimeError("alamat kosong")
        r = await self._http.get(
            f"{self.s.venues.zerox_api.rstrip('/')}/swap/permit2/quote",
            params={
                "chainId": self.chain.chain_id, "sellToken": sell, "buyToken": buy,
                "sellAmount": str(amount), "taker": self.signer.address,
                "slippageBps": str(self.s.risk.max_slippage_bps),
            },
            headers={"0x-api-key": self.s.venues.zerox_key},
        )
        if r.status_code != 200:
            raise RuntimeError(f"0x {r.status_code}")
        d = r.json()
        tx = d.get("transaction") or {}
        return {
            "amountOut": d.get("buyAmount"),
            "tx": {"to": tx.get("to"), "data": tx.get("data"), "value": int(tx.get("value", 0))},
            "gas": d.get("estimatedGas"),
        }

    async def _route_odos(self, sell: str, buy: str, amount: int) -> dict:
        if not self.signer:
            raise RuntimeError("alamat kosong")
        body = {
            "chainId": self.chain.chain_id,
            "inputTokens": [{"tokenAddress": sell, "amount": str(amount)}],
            "outputTokens": [{"tokenAddress": buy, "proportion": 1}],
            "userAddr": self.signer.address,
            "slippageLimitPercent": self.s.risk.max_slippage_bps / 100,
            "compact": True,
        }
        r = await self._http.post(f"{self.s.venues.odos_api.rstrip('/')}/sor/quote/v2", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"odos {r.status_code}")
        d = r.json()
        return {"amountOut": (d.get("outAmounts") or ["0"])[0], "odos": d}

    # ---------------------------------------------------------------- swap

    async def execute(self, intent: OrderIntent) -> Fill:
        assert intent.address, "alamat token wajib"

        token = intent.address
        slippage = min(intent.slippage_bps, self.s.risk.max_slippage_bps)

        if intent.side is Side.BUY:
            sell, buy = self.chain.usdc, token
            amount = int(intent.usd_amount * 1e6)
        else:
            if not self.signer:
                raise SigningError("jual butuh signer evm")
            sell, buy = token, self.chain.usdc
            amount = await self.balance(token)
            if amount <= 0:
                raise RuntimeError("saldo token kosong")

        route = await self.route(sell, buy, amount)

        tx_hash: str
        if self.s.live:
            if not self.signer:
                raise SigningError("signer evm tidak dikonfigurasi")
            tx = await self._build(route, sell, buy, amount, slippage)
            sent = self.signer.sign_evm_tx({
                "to": tx["to"],
                "value": int(tx.get("value", 0) or 0),
                "data": tx["data"],
            })
            tx_hash = sent[0] if isinstance(sent, tuple) else str(sent)
        else:
            tx_hash = "paper-evm-" + str(abs(hash(str(route))) % 10**14)
            log.info("[PAPER] evm swap %s di %s via %s", token, self.chain.name, route["router"])

        out = float(route["amountOut"])
        dec = await self.decimals(token)
        if intent.side is Side.BUY:
            qty = out / 10 ** dec
            px = (amount / 1e6) / qty if qty > 0 else 0.0
        else:
            qty = amount / 10 ** dec
            px = (out / 1e6) / qty if qty > 0 else 0.0

        return Fill(qty=qty, price=px, tx_hash=tx_hash, venue=Venue.DEX)

    async def _build(self, route: dict, sell: str, buy: str, amount: int,
                     slippage_bps: int) -> dict[str, Any]:
        if route["router"] == "kyber":
            body = {
                "routeSummary": route["summary"],
                "sender": self.signer.address,
                "recipient": self.signer.address,
                "slippageTolerance": slippage_bps // 100,
            }
            r = await self._http.post(
                f"{KYBER}/{self.chain.kyber}/api/v1/route/build",
                json=body, headers={"x-client-id": "gembul"},
            )
            r.raise_for_status()
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(d.get("message", "kyber build error"))
            data = d["data"]
            return {
                "to": data.get("routerAddress") or route.get("routerAddress"),
                "data": data["data"],
                "value": int(data.get("transactionValue", 0) or 0),
            }

        if route["router"] == "0x":
            tx = route["tx"]
            return {"to": tx["to"], "data": tx["data"], "value": tx["value"]}

        odos = route["odos"]
        r = await self._http.post(
            f"{self.s.venues.odos_api.rstrip('/')}/sor/assemble",
            json={"userAddr": self.signer.address, "pathId": odos["pathId"], "simulate": False},
        )
        r.raise_for_status()
        t = r.json()["transaction"]
        return {"to": t["to"], "data": t["data"], "value": int(t.get("value", 0) or 0)}
