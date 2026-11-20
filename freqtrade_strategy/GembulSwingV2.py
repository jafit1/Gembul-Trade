"""Freqtrade strategy: 1h regime-adaptive pullback & oversold snapback.

Fixes two bugs in the original GembulSwing:
1. config.json overrode timeframe to 15m (chopping fees).
2. minimal_roi used 24/72/168 thinking they were hours, when Freqtrade uses minutes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy


class GembulSwingV2(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 250

    # Values are in MINUTES (12h, 24h, 48h, 72h)
    minimal_roi = {
        "0": 0.14,
        "720": 0.08,
        "1440": 0.045,
        "2880": 0.025,
        "4320": 0.012,
    }
    stoploss = -0.065
    trailing_stop = True
    trailing_stop_positive = 0.018
    trailing_stop_positive_offset = 0.042
    trailing_only_offset_is_reached = True
    use_custom_stoploss = True

    use_exit_signal = True
    exit_profit_only = True
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
            "stop_duration_candles": 3,
        },
    ]

    @property
    def protections(self):
        return self.vendored_protections

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["rsi_prev"] = dataframe["rsi"].shift(1)
        dataframe["rsi_min3"] = dataframe["rsi"].rolling(3).min()
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd_hist"] = macd["macdhist"]
        dataframe["macd_hist_prev"] = dataframe["macd_hist"].shift(1)

        bb = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0)
        dataframe["bb_lower"] = bb["lowerband"]
        dataframe["bb_mid"] = bb["middleband"]
        dataframe["bb_upper"] = bb["upperband"]

        dataframe["vol_mean"] = dataframe["volume"].rolling(24).mean()
        dataframe["vol_ok"] = dataframe["volume"] > dataframe["vol_mean"] * 0.75
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 1. Uptrend pullback: intact macro trend, dipped near/below EMA21 with RSI < 42, now turning green
        uptrend = (dataframe["close"] > dataframe["ema200"]) & (
            dataframe["ema50"] > dataframe["ema200"]
        )
        pullback = (
            (dataframe["low"].rolling(3).min() <= dataframe["ema21"] * 1.002)
            & (dataframe["rsi_min3"] < 42)
            & (dataframe["rsi"] > dataframe["rsi_prev"])
            & (dataframe["close"] > dataframe["open"])
            & (dataframe["close"] > dataframe["ema9"])
        )
        cond_pullback = (
            uptrend
            & pullback
            & (dataframe["adx"] > 16)
            & (dataframe["atr_pct"] < 5.0)
            & dataframe["vol_ok"]
        )

        # 2. Oversold snapback (works in chop / bear bounces without fighting knife blindly)
        snapback = (
            (dataframe["rsi_min3"] < 28)
            & (dataframe["rsi"] >= 30)
            & (dataframe["rsi"] > dataframe["rsi_prev"])
            & (dataframe["low"].rolling(3).min() <= dataframe["bb_lower"] * 1.003)
            & (dataframe["close"] > dataframe["bb_lower"])
            & (dataframe["macd_hist"] > dataframe["macd_hist_prev"])
            & (dataframe["close"] > dataframe["open"])
            & (dataframe["atr_pct"] < 5.5)
            & (dataframe["volume"] > dataframe["vol_mean"] * 1.05)
        )

        dataframe.loc[cond_pullback, ["enter_long", "enter_tag"]] = (1, "trend_pullback")
        dataframe.loc[snapback & ~cond_pullback, ["enter_long", "enter_tag"]] = (1, "oversold_snapback")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Only exit on strong overbought exhaustion (exit_profit_only=True protects winners)
        ob = (dataframe["rsi"] > 78) & (dataframe["close"] < dataframe["open"])
        dataframe.loc[ob, ["exit_long", "exit_tag"]] = (1, "rsi_exhaustion")
        return dataframe

    def custom_stoploss(
        self, pair: str, trade: Any, current_time: datetime, current_rate: float,
        current_profit: float, after_fill: bool, **kwargs: Any
    ) -> float:
        # Lock breakeven (+0.6% covering 0.2% roundtrip fees) once trade hits +2.8%
        if current_profit >= 0.028:
            return -0.022  # relative to current_rate -> locks > +0.5% net
        # Time-based risk tightening: if stuck flat/negative after 36h, tighten stop to -3.5%
        hours = (current_time - trade.open_date_utc).total_seconds() / 3600.0
        if hours >= 36 and current_profit < 0.01:
            return -0.035
        return self.stoploss

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
        scale = min(1.0, 2.0 / atr_pct)
        return max(min_stake or 0.0, proposed_stake * max(0.4, scale))
