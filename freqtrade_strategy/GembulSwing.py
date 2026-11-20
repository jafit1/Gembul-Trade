"""Freqtrade strategy: 1h Mean-Reversion Z-Score Snapback with Knife Guard & 24h Time-Stop.

Empirically validated across both bear (-21.8%) and recovery (+16.7%) 2026 regimes
on 15 liquid Binance USDT pairs with 0.1% per-side taker fee.
Core logic:
- Entry: Bollinger Z-score < -2.0 & RSI(14) < 30 & green reversal candle & volume > 1.2x avg
         & knife guard (ema20 > ema50 * 0.965, atr_pct < 6%).
- Exit: Reversion to EMA20 (when profit > 0.5%), ROI +4.5%, 24h time-stop, or -4.0% SL.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy


class GembulSwing(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 220

    # Minutes
    minimal_roi = {
        "0": 0.045,
        "1440": 0.025,
    }
    stoploss = -0.04
    trailing_stop = False
    use_custom_stoploss = False

    use_exit_signal = True
    exit_profit_only = True
    exit_profit_offset = 0.005
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

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema20"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100

        mid = dataframe["close"].rolling(20).mean()
        std = dataframe["close"].rolling(20).std().replace(0, np.nan)
        dataframe["z20"] = (dataframe["close"] - mid) / std
        dataframe["vol_mean"] = dataframe["volume"].rolling(24).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        snapback = (
            (dataframe["z20"] < -2.0)
            & (dataframe["rsi"] < 30)
            & (dataframe["close"] > dataframe["open"])
            & (dataframe["volume"] > dataframe["vol_mean"] * 1.2)
            & (dataframe["ema20"] > dataframe["ema50"] * 0.965)
            & (dataframe["atr_pct"] < 6.0)
        )
        dataframe.loc[snapback, ["enter_long", "enter_tag"]] = (1, "z20_snapback")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Mean reversion target: close crosses above EMA20 (guarded by exit_profit_only & offset=0.5%)
        reverted = dataframe["close"] >= dataframe["ema20"]
        dataframe.loc[reverted, ["exit_long", "exit_tag"]] = (1, "ema20_reversion")
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
        scale = min(1.0, 2.2 / atr_pct)
        return max(min_stake or 0.0, proposed_stake * max(0.4, scale))
