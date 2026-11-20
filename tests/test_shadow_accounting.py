import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gembul.core.models import Signal, Venue
from gembul.core.shadow import ShadowBook, analyze_ohlcv
from gembul.db import Database
from gembul.memory.journal import JournalManager
from gembul.memory.store import MemoryStore


class _FakeReport:
    ok = True
    score = 0.9
    reasons: list[str] = []


class _FakeScanner:
    async def scan(self, chain, address):
        return _FakeReport()


class _FakeVenue:
    def __init__(self) -> None:
        self.buy_out = 1_000_000_000  # 1000 tokens at 6 decimals
        self.sell_out = 9_920_000     # $9.92 USDC back -> 0.8% roundtrip

    async def decimals(self, mint: str) -> int:
        return 6

    async def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100):
        # Buy USDC -> token
        if output_mint != "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v":
            return {"inAmount": str(amount), "outAmount": str(self.buy_out), "priceImpactPct": "0.002"}
        # Sell token -> USDC proportional to amount / 1e9
        ratio = amount / 1_000_000_000
        return {"inAmount": str(amount), "outAmount": str(int(self.sell_out * ratio)), "priceImpactPct": "0.002"}


class _FakeDex:
    def __init__(self) -> None:
        self.v = _FakeVenue()

    def venue(self, chain: str):
        return self.v


class _FakeLlm:
    ready = False


def _healthy_candles(count: int = 30, base: float = 0.01, step: float = 0.0001) -> list[list[float]]:
    out = []
    px = base
    for i in range(count):
        px = px + step
        vol = 1000.0 if i < count - 4 else 1600.0
        out.append([1700000000 + i * 300, px * 0.99, px * 1.01, px * 0.985, px, vol])
    return out


def test_analyze_ohlcv_healthy_uptrend():
    metrics = analyze_ohlcv(_healthy_candles())
    assert metrics is not None
    assert 42.0 <= metrics["rsi14"] <= 73.0 or metrics["rsi14"] > 70
    assert metrics["close"] > metrics["ema20"]
    assert metrics["vol_ratio"] > 1.0


def test_multi_candidate_evaluation_and_be_stop_and_true_roi(tmp_path: Path):
    async def run() -> None:
        db = Database(tmp_path / "test.sqlite")
        await db.connect()
        dex = _FakeDex()
        notices: list[str] = []

        async def fake_notify(text: str, **kw):
            notices.append(text)

        sb = ShadowBook(db, dex, _FakeScanner(), notifier=fake_notify)
        await sb.init()
        sb._fetch_5m_indicators = AsyncMock(return_value={
            "ema9": 0.0102, "ema20": 0.0100, "close": 0.0103,
            "rsi14": 58.0, "atr_pct": 3.5, "ext_pct": 3.0, "vol_ratio": 1.4, "trend_up": 1.0,
        })

        # Candidate 1 fails liquidity -> must NOT throttle Candidate 2!
        bad_sig = Signal(
            strategy="meme", symbol="BAD/USD", score=0.7, venue=Venue.DEX,
            chain="solana", address="BadMint1111111111111111111111111111111111", price=0.01,
            features={"liquidity_usd": 10_000, "volume_24h": 50_000, "age_minutes": 100, "chg_1h": 5.0, "buys_1h": 40, "sells_1h": 20, "pool": "PoolBad"},
        )
        good_sig = Signal(
            strategy="meme", symbol="GOOD/USD", score=0.8, venue=Venue.DEX,
            chain="solana", address="GoodMint111111111111111111111111111111111", price=0.01,
            features={"liquidity_usd": 150_000, "volume_24h": 400_000, "volume_1h": 40_000, "age_minutes": 120,
                      "chg_5m": 1.5, "chg_1h": 8.0, "chg_6h": 10.0, "buys_1h": 120, "sells_1h": 80,
                      "buys_5m": 15, "sells_5m": 10, "pool": "PoolGood"},
        )

        assert await sb.consider("plan1", bad_sig) is False
        # Non-scalp plans stay disabled when ACTIVE_PLANS focuses on scalping only
        assert await sb.consider("plan2", good_sig) is False
        assert await sb.consider("plan1", good_sig) is True

        # Trigger Scalping TP1 (+12% > +10%) -> sells 50%, moves stop_loss to entry * 1.015
        dex.v.sell_out = 11_200_000
        await sb.mark_and_exit()
        row = await db.fetchone("SELECT * FROM shadow_positions WHERE scheme='plan1'")
        assert row["stage"] == 1
        assert abs(row["stop_loss"] - row["entry_price"] * 1.015) < 1e-9

        # Price drops back to +0.5% (below BE +1.5% stop) -> MUST trigger stop_loss_be!
        dex.v.sell_out = 10_050_000
        await sb.mark_and_exit()
        closed = await db.fetchone("SELECT * FROM shadow_positions WHERE scheme='plan1'")
        assert closed["status"] == "closed"
        # True ROI on $10 initial stake: 50% * 11.2 + 50% * 10.05 = 5.60 + 5.025 = 10.625 -> +$0.625 -> +6.25%
        assert 5.5 < closed["pnl_pct"] < 7.0
        assert closed["pnl_usd"] > 0

        # Persistent cooldown must block immediate re-entry of same mint
        sb._last_entry_ts.clear()
        assert await sb.consider("plan1", good_sig) is False

        # Notifier must have received OPEN, PARTIAL TP, and CLOSED alerts
        assert len(notices) == 3
        assert "OPEN" in notices[0]
        assert "PARTIAL TP" in notices[1]
        assert "CLOSED" in notices[2]

        # Test Scalping TP2 (+20%) + 5% runner trail on a fresh mint
        dex.v.sell_out = 9_920_000
        sig2 = Signal(
            strategy="meme", symbol="SCALP2/USD", score=0.85, venue=Venue.DEX,
            chain="solana", address="Scalp2Mint11111111111111111111111111111111", price=0.01,
            features={"liquidity_usd": 150_000, "volume_24h": 400_000, "volume_1h": 40_000, "age_minutes": 120,
                      "chg_5m": 1.5, "chg_1h": 4.0, "chg_6h": 6.0, "buys_1h": 120, "sells_1h": 80,
                      "buys_5m": 15, "sells_5m": 10, "pool": "PoolScalp2"},
        )
        assert await sb.consider("plan1", sig2) is True
        dex.v.sell_out = 11_100_000  # +11% -> TP1
        await sb.mark_and_exit()
        dex.v.sell_out = 12_200_000  # +22% -> TP2, locks SL at +10%, sets peak +22%
        await sb.mark_and_exit()
        pos2 = await db.fetchone("SELECT * FROM shadow_positions WHERE symbol='SCALP2/USD'")
        assert pos2["stage"] == 2
        assert abs(pos2["stop_loss"] - pos2["entry_price"] * 1.10) < 1e-9
        dex.v.sell_out = 11_400_000  # drops ~6.5% off +22% peak -> triggers 5% runner trail!
        await sb.mark_and_exit()
        pos2_closed = await db.fetchone("SELECT * FROM shadow_positions WHERE symbol='SCALP2/USD'")
        assert pos2_closed["status"] == "closed"
        assert pos2_closed["pnl_usd"] > 1.3  # 0.50*1.1 + 0.25*2.2 + 0.25*1.4 = +1.45

        # JournalManager.generate_ai_journal() must not KeyError on total
        jm = JournalManager(db, MemoryStore(db), _FakeLlm(), shadow=sb)
        j = await jm.generate_ai_journal("daily")
        assert j["total"]["total_trades"] == 2
        assert j["total"]["win_trades"] == 2

        # Filter checks: negative 1h, tokenized equity, low ATR, high roundtrip spread
        sb._last_entry_ts.clear()
        neg_1h = Signal(
            strategy="meme", symbol="NEG1H/USD", score=0.8, venue=Venue.DEX,
            chain="solana", address="Neg1hMint111111111111111111111111111111111", price=0.01,
            features={**good_sig.features, "chg_1h": -1.5, "pool": "PoolNeg1h"},
        )
        assert await sb.consider("plan1", neg_1h) is False
        ev_neg = await db.fetchone("SELECT reason FROM shadow_events WHERE symbol='NEG1H/USD' AND event='skip' ORDER BY id DESC LIMIT 1")
        assert ev_neg["reason"] == "scalp_conditions_not_met"

        sb._last_entry_ts.clear()
        eq_sig = Signal(
            strategy="meme", symbol="TTWO/USD", score=0.8, venue=Venue.DEX,
            chain="solana", address="TtwoMint1111111111111111111111111111111111", price=0.01,
            features={**good_sig.features, "pool": "PoolTtwo"},
        )
        assert await sb.consider("plan1", eq_sig) is False
        ev_eq = await db.fetchone("SELECT reason FROM shadow_events WHERE symbol='TTWO/USD' AND event='skip' ORDER BY id DESC LIMIT 1")
        assert ev_eq["reason"] == "tokenized_equity_excluded"

        sb._last_entry_ts.clear()
        dex.v.sell_out = 9_850_000  # 1.5% RT > 0.90% cap -> reject
        rt_sig = Signal(
            strategy="meme", symbol="WIDE/USD", score=0.8, venue=Venue.DEX,
            chain="solana", address="WideMint1111111111111111111111111111111111", price=0.01,
            features={**good_sig.features, "pool": "PoolWide"},
        )
        assert await sb.consider("plan1", rt_sig) is False
        ev_rt = await db.fetchone("SELECT reason FROM shadow_events WHERE symbol='WIDE/USD' AND event='skip' ORDER BY id DESC LIMIT 1")
        assert ev_rt["reason"] == "roundtrip_cost_too_high"

        # Early breakeven lock at +6.5% peak before TP1 (+10%)
        sb._last_entry_ts.clear()
        dex.v.sell_out = 9_920_000
        be_sig = Signal(
            strategy="meme", symbol="BE65/USD", score=0.85, venue=Venue.DEX,
            chain="solana", address="Be65Mint1111111111111111111111111111111111", price=0.01,
            features={**good_sig.features, "pool": "PoolBe65"},
        )
        assert await sb.consider("plan1", be_sig) is True
        dex.v.sell_out = 10_700_000  # +7% >= +6.5% -> lock SL at +0.3% while still stage 0
        await sb.mark_and_exit()
        pos_be = await db.fetchone("SELECT * FROM shadow_positions WHERE symbol='BE65/USD'")
        assert pos_be["stage"] == 0
        assert abs(pos_be["stop_loss"] - pos_be["entry_price"] * 1.003) < 1e-9

        dex.v.sell_out = 10_010_000  # pullback below +0.3% -> closes with stop_loss_be
        await sb.mark_and_exit()
        pos_be_closed = await db.fetchone("SELECT * FROM shadow_positions WHERE symbol='BE65/USD'")
        assert pos_be_closed["status"] == "closed"
        ev_be = await db.fetchone("SELECT reason FROM shadow_events WHERE symbol='BE65/USD' AND event='exit' ORDER BY id DESC LIMIT 1")
        assert ev_be["reason"] == "stop_loss_be"

        # Fast 25m stale exit for dead flat trade (< +1.5% peak, mark < entry)
        sb._last_entry_ts.clear()
        dex.v.sell_out = 9_920_000
        stale_sig = Signal(
            strategy="meme", symbol="STALE25/USD", score=0.82, venue=Venue.DEX,
            chain="solana", address="Stale25Mint1111111111111111111111111111111", price=0.01,
            features={**good_sig.features, "pool": "PoolStale25"},
        )
        assert await sb.consider("plan1", stale_sig) is True
        await db.execute("UPDATE shadow_positions SET opened_at=datetime('now','-26 minutes') WHERE symbol='STALE25/USD'")
        dex.v.sell_out = 9_900_000  # below entry, never hit +1.5%
        await sb.mark_and_exit()
        pos_stale = await db.fetchone("SELECT * FROM shadow_positions WHERE symbol='STALE25/USD'")
        assert pos_stale["status"] == "closed"
        ev_stale = await db.fetchone("SELECT reason FROM shadow_events WHERE symbol='STALE25/USD' AND event='exit' ORDER BY id DESC LIMIT 1")
        assert ev_stale["reason"] == "time_stop_stale_25m"

        await sb.close()
        await db.close()

    asyncio.run(run())
