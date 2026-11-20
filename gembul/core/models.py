"""Shared value types. Small, immutable, no logic."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Venue(str, Enum):
    CEX = "cex"
    DEX = "dex"
    FREQTRADE = "freqtrade"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(slots=True)
class Signal:
    strategy: str
    symbol: str
    score: float
    venue: Venue
    chain: str | None = None
    address: str | None = None
    price: float | None = None
    features: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def is_onchain(self) -> bool:
        return self.venue is Venue.DEX


@dataclass(slots=True)
class OrderIntent:
    venue: Venue
    side: Side
    symbol: str
    usd_amount: float
    chain: str | None = None
    address: str | None = None
    limit_price: float | None = None
    slippage_bps: int = 150
    signal_id: int = 0
    reason: str = ""


@dataclass(slots=True)
class Fill:
    qty: float
    price: float
    fee_usd: float = 0.0
    tx_hash: str | None = None
    venue: Venue = Venue.CEX


@dataclass(slots=True)
class Position:
    id: int
    venue: Venue
    symbol: str
    side: Side
    qty: float
    entry_price: float
    entry_usd: float
    chain: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_stop: float | None = None
    peak_price: float | None = None
    status: str = "open"
    pnl_usd: float | None = None
    pnl_pct: float | None = None
    scheme: str = "scalp"
    opened_at: str | None = None

    @property
    def notional(self) -> float:
        return self.qty * self.entry_price

    def unrealized_pct(self, price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (price - self.entry_price) / self.entry_price * 100.0

    def unrealized_usd(self, price: float) -> float:
        return (price - self.entry_price) * self.qty


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    usd_amount: float = 0.0
    reason: str = ""
    checks: dict[str, Any] = field(default_factory=dict)
