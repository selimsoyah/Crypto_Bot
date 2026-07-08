from __future__ import annotations

import pandas as pd

from box_strategy import BoxStrategyEngine


def _base_box_frame(n: int = 30) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "Timestamp": ts,
            "Open": [100.0] * n,
            "High": [110.0] * n,
            "Low": [90.0] * n,
            "Close": [100.0] * n,
            "Volume": [100.0] * n,
        }
    )


def test_box_state_inside_range_returns_cash():
    engine = BoxStrategyEngine(
        lookback_candles=20,
        confirmation_candles=3,
        volume_filter_multiplier=1.1,
    )
    candles = _base_box_frame()
    candles.loc[candles.index[-1], "Close"] = 100.0
    state = engine.evaluate(candles)
    assert state.valid is True
    assert state.top == 110.0
    assert state.bottom == 90.0
    assert state.breakout == "CASH"


def test_box_breakout_long_with_volume_gate():
    engine = BoxStrategyEngine(
        lookback_candles=20,
        confirmation_candles=3,
        volume_filter_multiplier=1.2,
    )
    candles = _base_box_frame()
    candles.loc[candles.index[-1], "Close"] = 111.0
    candles.loc[candles.index[-1], "Volume"] = 160.0  # > 1.2 * SMA(100)
    state = engine.evaluate(candles)
    assert state.valid is True
    assert state.volume_ok is True
    assert state.breakout == "LONG"


def test_box_breakout_short_blocked_by_volume():
    engine = BoxStrategyEngine(
        lookback_candles=20,
        confirmation_candles=3,
        volume_filter_multiplier=1.2,
    )
    candles = _base_box_frame()
    candles.loc[candles.index[-1], "Close"] = 89.0
    candles.loc[candles.index[-1], "Volume"] = 80.0
    state = engine.evaluate(candles)
    assert state.valid is True
    assert state.volume_ok is False
    assert state.breakout == "CASH"

