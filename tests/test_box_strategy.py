from __future__ import annotations

import pandas as pd

from box_strategy import BoxStrategyEngine


def _two_day_frame(close: float, volume: float = 100.0) -> pd.DataFrame:
    """Previous UTC day (2026-06-01) plus current day bars ending on 2026-06-02."""
    prev = pd.date_range("2026-06-01 00:00", periods=96, freq="15min", tz="UTC")
    curr = pd.date_range("2026-06-02 00:00", periods=10, freq="15min", tz="UTC")
    ts = prev.append(curr)
    n = len(ts)
    frame = pd.DataFrame(
        {
            "Timestamp": ts,
            "Open": [100.0] * n,
            "High": [110.0] * n,
            "Low": [90.0] * n,
            "Close": [100.0] * n,
            "Volume": [100.0] * n,
        }
    )
    frame.loc[frame.index[-1], "Close"] = close
    frame.loc[frame.index[-1], "Volume"] = volume
    return frame


def test_previous_utc_day_bounds_extracts_high_low_middle():
    candles = _two_day_frame(close=100.0)
    top, bottom, middle, prev_day, rows = BoxStrategyEngine.previous_utc_day_bounds(candles)
    assert prev_day.isoformat() == "2026-06-01"
    assert top == 110.0
    assert bottom == 90.0
    assert middle == 100.0
    assert len(rows) == 96


def test_box_state_inside_range_returns_cash():
    engine = BoxStrategyEngine(volume_filter_multiplier=1.1)
    candles = _two_day_frame(close=100.0)
    state = engine.evaluate(candles)
    assert state.valid is True
    assert state.top == 110.0
    assert state.bottom == 90.0
    assert state.middle_line == 100.0
    assert state.active_box_number == 1
    assert state.prev_day == "2026-06-01"
    assert state.breakout == "CASH"


def test_active_box_number_increments_on_new_utc_day():
    engine = BoxStrategyEngine(volume_filter_multiplier=1.0)
    day1 = _two_day_frame(close=100.0)
    state1 = engine.evaluate(day1)
    assert state1.active_box_number == 1

    day2 = _two_day_frame(close=100.0)
    day2["Timestamp"] = pd.to_datetime(day2["Timestamp"], utc=True) + pd.Timedelta(days=1)
    state2 = engine.evaluate(day2)
    assert state2.active_box_number == 2


def test_box_breakout_long_with_volume_gate():
    engine = BoxStrategyEngine(volume_filter_multiplier=1.2)
    candles = _two_day_frame(close=111.0, volume=160.0)
    state = engine.evaluate(candles)
    assert state.valid is True
    assert state.volume_ok is True
    assert state.breakout == "LONG"


def test_box_breakout_short_blocked_by_volume():
    engine = BoxStrategyEngine(volume_filter_multiplier=1.2)
    candles = _two_day_frame(close=89.0, volume=80.0)
    state = engine.evaluate(candles)
    assert state.valid is True
    assert state.volume_ok is False
    assert state.breakout == "CASH"
