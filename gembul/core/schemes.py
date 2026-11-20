"""Strategy profiles: Plan 1 (Trend Ladder), Plan 2 (Breakout), Plan 3 (Early Sniper)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SchemeConfig:
    name: str
    label: str
    badge: str
    stop_loss_pct: float
    take_profit_pct: float
    trailing_activation_pct: float
    trailing_distance_pct: float
    min_liquidity_usd: float
    default_size_usd: float
    max_hold_minutes: float
    description: str


SCHEMES: dict[str, SchemeConfig] = {
    "plan1": SchemeConfig(
        name="plan1",
        label="Skema Scalping (1m/5m Fast)",
        badge="⚡️ SCALPING (1m/5m)",
        stop_loss_pct=6.5,
        take_profit_pct=10.0,  # TP1 trigger
        trailing_activation_pct=20.0,
        trailing_distance_pct=5.0,
        min_liquidity_usd=60_000.0,
        default_size_usd=100.0,
        max_hold_minutes=45.0,
        description="Scalping 1m/5m: SL ATR 5.5-7.5%. TP1 (+10%) jual 50% & SL ke BE (+1.5%). TP2 (+20%) jual 25% & SL ke +10%. Sisa 25% trailing 5%. Time-stop 45m jika stagnan.",
    ),
    "plan2": SchemeConfig(
        name="plan2",
        label="Rencana 2: Breakout (Nonaktif)",
        badge="🚀 RENCANA 2 (NONAKTIF)",
        stop_loss_pct=8.0,
        take_profit_pct=40.0,
        trailing_activation_pct=12.0,
        trailing_distance_pct=4.5,
        min_liquidity_usd=120_000.0,
        default_size_usd=100.0,
        max_hold_minutes=90.0,
        description="Dinonaktifkan (fokus skema Scalping).",
    ),
    "plan3": SchemeConfig(
        name="plan3",
        label="Rencana 3: Sniper (Nonaktif)",
        badge="🎯 RENCANA 3 (NONAKTIF)",
        stop_loss_pct=9.0,
        take_profit_pct=25.0,
        trailing_activation_pct=25.0,
        trailing_distance_pct=7.0,
        min_liquidity_usd=60_000.0,
        default_size_usd=100.0,
        max_hold_minutes=90.0,
        description="Dinonaktifkan (fokus skema Scalping).",
    ),
}

# Backward compatibility aliases
SCHEMES["scalp"] = SCHEMES["plan1"]
SCHEMES["safe"] = SCHEMES["plan3"]


def get_scheme(name: str) -> SchemeConfig:
    key = (name or "").strip().lower()
    return SCHEMES.get(key) or SCHEMES["plan1"]
