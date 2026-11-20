"""Chain registry. RPC endpoints are public defaults and can be overridden from config."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Chain:
    name: str
    kind: str            # "evm" | "svm"
    chain_id: int        # 0 for solana
    rpc: str
    explorer: str
    wrapped_native: str
    native_symbol: str
    usdc: str
    goplus_id: str
    kyber: str = ""      # kyberswap chain slug; empty for non-evm


CHAINS: dict[str, Chain] = {
    "solana": Chain(
        name="solana", kind="svm", chain_id=0,
        rpc=os.environ.get("GEMBUL_SOLANA_RPC") or "https://api.mainnet-beta.solana.com",
        explorer="https://solscan.io/tx/",
        wrapped_native="So11111111111111111111111111111111111111112",
        native_symbol="SOL",
        usdc="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        goplus_id="solana",
    ),
    "ethereum": Chain(
        name="ethereum", kind="evm", chain_id=1,
        rpc="https://eth.llamarpc.com",
        explorer="https://etherscan.io/tx/",
        wrapped_native="0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        native_symbol="ETH",
        usdc="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        goplus_id="1", kyber="ethereum",
    ),
    "base": Chain(
        name="base", kind="evm", chain_id=8453,
        rpc="https://mainnet.base.org",
        explorer="https://basescan.org/tx/",
        wrapped_native="0x4200000000000000000000000000000000000006",
        native_symbol="ETH",
        usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        goplus_id="8453", kyber="base",
    ),
    "bsc": Chain(
        name="bsc", kind="evm", chain_id=56,
        rpc="https://bsc-dataseed.binance.org",
        explorer="https://bscscan.com/tx/",
        wrapped_native="0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        native_symbol="BNB",
        usdc="0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        goplus_id="56", kyber="bsc",
    ),
    "arbitrum": Chain(
        name="arbitrum", kind="evm", chain_id=42161,
        rpc="https://arb1.arbitrum.io/rpc",
        explorer="https://arbiscan.io/tx/",
        wrapped_native="0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        native_symbol="ETH",
        usdc="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        goplus_id="42161", kyber="arbitrum",
    ),
    "polygon": Chain(
        name="polygon", kind="evm", chain_id=137,
        rpc="https://polygon-rpc.com",
        explorer="https://polygonscan.com/tx/",
        wrapped_native="0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
        native_symbol="POL",
        usdc="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        goplus_id="137", kyber="polygon",
    ),
}


def get_chain(name: str) -> Chain:
    key = name.lower()
    if key not in CHAINS:
        raise KeyError(f"chain tidak dikenal: {name}. tersedia: {', '.join(CHAINS)}")
    return CHAINS[key]
