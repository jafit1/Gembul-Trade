"""Freqtrade strategy: buy the dip inside an established uptrend, hyperopt-ready.

Version history, kept honest:
- v1 (15m breakout): 491 trades, -9.0%, 40% win. Overtraded chop.
- v2 (HTF filter + tight pullback): 24 trades, -0.8%, 8% win. Exits fired
  before the trade could breathe, entries landed on exhaustion.
- v3 (MACD cross / RSI recovery): 608 trades, -14.9%, 20% win. Signal fired
  into mean reversion instead of after it.
- v4: every threshold below is a searchable parameter. The defaults are only
  starting points; the numbers that matter come out of hyperopt, then get
  validated on a window the optimiser never saw.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import (
    CategoricalParameter,
    DecimalParameter,
    IntParameter,
    IStrategy,
)


class GembulTrend(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"
    can_short = False
    process_only_new_candles = True
    startup_candle_count = 400

    minimal_roi = {"0": 0.07, "150": 0.035, "480": 0.015, "960": 0.0}

    stoploss = -0.05
    trailing_stop = True
    trailing_stop_positive = 0.02
    trailing_stop_positive_offset = 0.05
    trailing_only_offset_is_reached = True

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    vendored_protections = [
        {
            "method": "StoplossGuard",
            "lookback_period_candles": 96,
            "trade_limit": 3,
            "stop_duration_candles": 48,
            "only_per_pair": False,
        },
        {
            "method": "MaxDrawdown",
            "lookback_period_candles": 400,
            "trade_limit": 8,
            "stop_duration_candles": 96,
            "max_allowed_drawdown": 0.08,
        },
    ]

    @property
    def protections(self):
        return self.vendored_protections

    # buy space
    rsi_floor = IntParameter(18, 45, default=44, space="buy")
    adx_min = IntParameter(10, 32, default=21, space="buy")
    ext_max = DecimalParameter(1.0, 6.0, default=3.5, decimals=1, space="buy")
    atr_ceiling = DecimalParameter(2.5, 7.0, default=2.8, decimals=1, space="buy")
    require_uptrend = CategoricalParameter([True, False], default=False, space="buy")
    entry_mode = CategoricalParameter(
        ["rsi_recovery", "macd_cross", "both"], default="rsi_recovery", space="buy"
    )

    # sell space
    rsi_ceiling = IntParameter(62, 88, default=88, space="sell")
    exit_on_structure = CategoricalParameter([True, False], default=False, space="sell")
    exit_on_trend_loss = CategoricalParameter([True, False], default=False, space="sell")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["rsi_prev"] = dataframe["rsi"].shift(1)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macd_signal"] = macd["macdsignal"]
        dataframe["macd_prev"] = dataframe["macd"].shift(1)
        dataframe["macd_signal_prev"] = dataframe["macd_signal"].shift(1)

        dataframe["uptrend"] = (dataframe["close"] > dataframe["ema200"]) & (
            dataframe["ema50"] > dataframe["ema200"]
        )
        dataframe["macd_cross_up"] = (
            (dataframe["macd"] > dataframe["macd_signal"])
            & (dataframe["macd_prev"] <= dataframe["macd_signal_prev"])
        )
        dataframe["rsi_recovery"] = (dataframe["rsi_prev"] < self.rsi_floor.value) & (
            dataframe["rsi"] >= self.rsi_floor.value
        )
        dataframe["ext_atr"] = (dataframe["close"] - dataframe["ema50"]) / dataframe["atr"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if self.entry_mode.value == "rsi_recovery":
            setup = dataframe["rsi_recovery"]
        elif self.entry_mode.value == "macd_cross":
            setup = dataframe["macd_cross_up"]
        else:
            setup = dataframe["macd_cross_up"] | dataframe["rsi_recovery"]

        if self.require_uptrend.value:
            regime = dataframe["uptrend"]
        else:
            regime = dataframe["close"] > dataframe["ema200"] * 0.985

        filter_ok = (
            regime
            & (dataframe["adx"] > self.adx_min.value)
            & (dataframe["atr_pct"] < self.atr_ceiling.value)
            & (dataframe["ext_atr"] < self.ext_max.value)
        )

        dataframe.loc[setup & filter_ok, ["enter_long", "enter_tag"]] = (
            1,
            "dip_buy",
        )
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        exits = dataframe["rsi"] > self.rsi_ceiling.value

        if self.exit_on_structure.value:
            exits = exits | (
                (dataframe["close"] < dataframe["ema50"])
                & (dataframe["macd"] < dataframe["macd_signal"])
            )
        if self.exit_on_trend_loss.value:
            exits = exits | (dataframe["close"] < dataframe["ema200"])

        dataframe.loc[exits, ["exit_long", "exit_tag"]] = (1, "trend_or_ob")
        return dataframe

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float | None,
                            max_stake: float, leverage: float,
                            entry_tag: str | None, side: str,
                            **kwargs: Any) -> float:
        """Volatility-scaled sizing: wider ATR means a smaller position."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) == 0:
            return proposed_stake
        atr_pct = float(dataframe["atr_pct"].iloc[-1])
        if atr_pct <= 0:
            return proposed_stake
        scale = min(1.0, 2.2 / atr_pct)
        return max(min_stake or 0.0, proposed_stake * max(0.3, scale))
