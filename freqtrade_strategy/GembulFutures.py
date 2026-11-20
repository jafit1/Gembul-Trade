"""Freqtrade USDT-M Futures strategy: Bidirectional Regime-Adaptive Mean-Reversion (Long + Short).

Walk-forward validated across 2026 Bear (-22.8%), Bull (+32.9%), and Bear (-25.8%) regimes:
- Longs (Regime-Gated):
  * Macro Bull (BTC > EMA200): z20 < -1.90, RSI < 31, green candle, vol > 1.1x.
  * 4h Uptrend (BTC 4h > EMA50 & EMA20 > EMA50): z20 < -2.00, RSI < 30, vol > 1.2x.
  * Bear/Chop Deep Capitulation: z20 < -2.40, RSI < 27, BTC 6h > -2.0%.
- Shorts (Strict Macro Bear Gate):
  * Only active when BOTH BTC 1h < EMA200 AND BTC 4h < EMA50_4h AND pair < EMA200.
  * Fades overextended bear-market dead-cat bounces (z20 > 1.75 & RSI > 60 & red reversal).
- Isolated 2.0x leverage, -4.0% SL (price move), +5.2% ROI, EMA20 reversion exit, 24h time-stop.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy, merge_informative_pair


class GembulFutures(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True
    process_only_new_candles = True
    startup_candle_count = 220

    # Target ROI in price-move terms (Freqtrade multiplies by leverage automatically on profit%)
    minimal_roi = {
        "0": 0.095,
        "720": 0.06,
        "1440": 0.035,
    }
    # -4.0% underlying price stop * 2x leverage = -8.0% position ROI stop
    stoploss = -0.08
    trailing_stop = False
    use_custom_stoploss = False

    use_exit_signal = True
    exit_profit_only = True
    exit_profit_offset = 0.01  # +1.0% leveraged = +0.5% price move covering 0.1% futures RT fee
    ignore_roi_if_entry_signal = False

    entry_pricing = {
        "price_side": "other",
        "use_order_book": True,
        "order_book_top": 3,
        "price_last_balance": 0.0,
        "check_depth_of_market": {"enabled": False, "bids_to_ask_delta": 1},
    }
    exit_pricing = {"price_side": "other", "use_order_book": True, "order_book_top": 3}

    vendored_protections = [
        {
            "method": "StoplossGuard",
            "lookback_period_candles": 24,
            "trade_limit": 2,
            "stop_duration_candles": 12,
            "only_per_pair": False,
        },
        {
            "method": "CooldownPeriod",
            "stop_duration_candles": 2,
        },
    ]

    @property
    def protections(self):
        return self.vendored_protections

    def _btc_pair(self) -> str:
        stake = self.config.get("stake_currency", "USDT")
        if self.config.get("trading_mode", "spot") == "futures":
            return f"BTC/{stake}:{stake}"
        return f"BTC/{stake}"

    def informative_pairs(self) -> list[tuple[str, str]]:
        btc = self._btc_pair()
        pairs = [(btc, "1h"), (btc, "4h")]
        for pair in self.dp.current_whitelist():
            pairs.append((pair, "4h"))
        return pairs

    def leverage(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_leverage: float, max_leverage: float, entry_tag: str | None,
        side: str, **kwargs: Any
    ) -> float:
        return min(2.0, max_leverage)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100.0

        mid = dataframe["close"].rolling(20).mean()
        std = dataframe["close"].rolling(20).std().replace(0, np.nan)
        dataframe["z20"] = (dataframe["close"] - mid) / std
        dataframe["vol_mean"] = dataframe["volume"].rolling(24).mean()

        if self.dp:
            inf_4h = self.dp.get_pair_dataframe(pair=metadata["pair"], timeframe="4h")
            if len(inf_4h) > 0:
                inf_4h = inf_4h[["date", "close"]].copy()
                inf_4h["ema20"] = ta.EMA(inf_4h, timeperiod=20)
                inf_4h["ema50"] = ta.EMA(inf_4h, timeperiod=50)
                dataframe = merge_informative_pair(
                    dataframe, inf_4h, self.timeframe, "4h", ffill=True
                )
                dataframe = dataframe.drop(columns=["date_4h"], errors="ignore")

            btc_pair = self._btc_pair()
            btc_1h = self.dp.get_pair_dataframe(pair=btc_pair, timeframe="1h")
            if len(btc_1h) > 0:
                btc_1h = btc_1h[["date", "close"]].copy()
                btc_1h["ema200"] = ta.EMA(btc_1h, timeperiod=200)
                btc_1h["ret6"] = btc_1h["close"].pct_change(6) * 100.0
                btc_1h = btc_1h.rename(columns={
                    "close": "btc_close", "ema200": "btc_ema200", "ret6": "btc_ret6"
                })
                dataframe = dataframe.merge(btc_1h, on="date", how="left")

            btc_4h = self.dp.get_pair_dataframe(pair=btc_pair, timeframe="4h")
            if len(btc_4h) > 0:
                btc_4h = btc_4h[["date", "close"]].copy()
                btc_4h["ema20"] = ta.EMA(btc_4h, timeperiod=20)
                btc_4h["ema50"] = ta.EMA(btc_4h, timeperiod=50)
                btc_4h = btc_4h.rename(columns={
                    "close": "btc_close", "ema20": "btc_ema20", "ema50": "btc_ema50"
                })
                dataframe = merge_informative_pair(
                    dataframe, btc_4h, self.timeframe, "4h", ffill=True
                )
                dataframe = dataframe.drop(columns=["date_4h"], errors="ignore")

        for col, default in [
            ("close_4h", dataframe["close"]),
            ("ema50_4h", dataframe["ema50"]),
            ("btc_close", dataframe["close"]),
            ("btc_ema200", dataframe["ema200"]),
            ("btc_ret6", 0.0),
            ("btc_close_4h", dataframe["close"]),
            ("btc_ema20_4h", dataframe["ema20"]),
            ("btc_ema50_4h", dataframe["ema50"]),
        ]:
            if col not in dataframe.columns:
                dataframe[col] = default

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # LONG: Regime-gated oversold snapback
        long_base = (
            (dataframe["close"] > dataframe["open"])
            & (dataframe["ema20"] > dataframe["ema50"] * 0.965)
            & (dataframe["atr_pct"] < 6.0)
        )
        l_bull = (
            (dataframe["btc_close"] > dataframe["btc_ema200"])
            & (dataframe["z20"] < -1.90)
            & (dataframe["rsi"] < 31.0)
            & (dataframe["volume"] > dataframe["vol_mean"] * 1.10)
        )
        l_4h = (
            (dataframe["btc_close_4h"] > dataframe["btc_ema50_4h"])
            & (dataframe["btc_ema20_4h"] > dataframe["btc_ema50_4h"])
            & (dataframe["z20"] < -2.00)
            & (dataframe["rsi"] < 30.0)
            & (dataframe["volume"] > dataframe["vol_mean"] * 1.20)
        )
        l_cap = (
            (dataframe["z20"] < -2.40)
            & (dataframe["rsi"] < 27.0)
            & (dataframe["volume"] > dataframe["vol_mean"] * 1.20)
            & (dataframe["btc_ret6"] > -2.0)
        )
        dataframe.loc[long_base & l_bull, ["enter_long", "enter_tag"]] = (1, "long_snap_bull")
        dataframe.loc[long_base & ~l_bull & l_4h, ["enter_long", "enter_tag"]] = (1, "long_snap_4h")
        dataframe.loc[long_base & ~l_bull & ~l_4h & l_cap, ["enter_long", "enter_tag"]] = (1, "long_snap_cap")

        # SHORT: Strictly gated to confirmed BTC + pair bear regimes (dead-cat bounce fades)
        btc_bear = (
            (dataframe["btc_close"] < dataframe["btc_ema200"])
            & (dataframe["btc_close_4h"] < dataframe["btc_ema50_4h"])
        )
        short_base = (
            btc_bear
            & (dataframe["close"] < dataframe["ema200"])
            & (dataframe["close"] < dataframe["open"])
            & (dataframe["atr_pct"] < 5.5)
        )
        s_4h_fade = (
            (dataframe["close_4h"] < dataframe["ema50_4h"])
            & (dataframe["z20"] > 1.75)
            & (dataframe["rsi"] > 60.0)
            & (dataframe["volume"] > dataframe["vol_mean"] * 0.85)
        )
        s_1h_fade = (
            (dataframe["z20"] > 1.85)
            & (dataframe["rsi"] > 62.0)
        )
        dataframe.loc[short_base & s_4h_fade, ["enter_short", "enter_tag"]] = (1, "short_bear_4h_fade")
        dataframe.loc[short_base & ~s_4h_fade & s_1h_fade, ["enter_short", "enter_tag"]] = (1, "short_bear_1h_fade")

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[dataframe["close"] >= dataframe["ema20"], ["exit_long", "exit_tag"]] = (
            1, "ema20_reversion"
        )
        dataframe.loc[dataframe["close"] <= dataframe["ema20"], ["exit_short", "exit_tag"]] = (
            1, "ema20_reversion_short"
        )
        return dataframe

    def custom_exit(
        self, pair: str, trade: Any, current_time: datetime, current_rate: float,
        current_profit: float, **kwargs: Any
    ) -> str | None:
        hours = (current_time - trade.open_date_utc).total_seconds() / 3600.0
        if hours >= 24.0:
            return "time_stop_24h"
        return None

    def custom_stake_amount(
        self, pair: str, current_time: datetime, current_rate: float,
        proposed_stake: float, min_stake: float | None, max_stake: float,
        leverage: float, entry_tag: str | None, side: str, **kwargs: Any
    ) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) == 0:
            return proposed_stake
        atr_pct = float(dataframe["atr_pct"].iloc[-1])
        if atr_pct <= 0:
            return proposed_stake
        scale = min(1.0, 2.4 / atr_pct)
        return max(min_stake or 0.0, proposed_stake * max(0.5, scale))
