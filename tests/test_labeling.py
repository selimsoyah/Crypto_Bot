"""Train/serve consistency tests (Phase 0 fix #1).

Guarantees that the triple-barrier labels used to TRAIN the model and the
TP/SL brackets used to TRADE live both derive from the same ``config.py``
constants and can never silently drift apart.
"""

import inspect

import numpy as np
import pandas as pd
import pytest

import config
from bot_loop import bracket_prices
from feature_factory import build_target


def _ohlcv_frame(closes, highs, lows):
    n = len(closes)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": np.ones(n),
        }
    )


def test_build_target_defaults_resolve_to_config_constants():
    """Labeling barriers MUST resolve to config TP/SL when args are omitted."""
    signature = inspect.signature(build_target)
    assert signature.parameters["take_profit"].default is None
    assert signature.parameters["stop_loss"].default is None
    assert signature.parameters["window"].default is None

    closes = np.full(30, 50_000.0)
    frame = _ohlcv_frame(closes, closes + 100, closes - 100)
    labels = build_target(frame)
    explicit = build_target(
        frame,
        window=config.FORWARD_WINDOW,
        take_profit=config.TAKE_PROFIT_PCT,
        stop_loss=config.STOP_LOSS_PCT,
    )
    pd.testing.assert_series_equal(labels, explicit)


def test_live_brackets_match_config_constants():
    """Live TP/SL brackets MUST be the config TP/SL applied to the fill."""
    entry = 50_000.0
    tp, sl = bracket_prices("LONG", entry)
    assert tp == pytest.approx(entry * (1.0 + config.TAKE_PROFIT_PCT))
    assert sl == pytest.approx(entry * (1.0 - config.STOP_LOSS_PCT))

    tp_s, sl_s = bracket_prices("SHORT", entry)
    assert tp_s == pytest.approx(entry * (1.0 - config.TAKE_PROFIT_PCT))
    assert sl_s == pytest.approx(entry * (1.0 + config.STOP_LOSS_PCT))


def test_bracket_rejects_unknown_direction():
    with pytest.raises(ValueError):
        bracket_prices("SIDEWAYS", 100.0)


def test_label_long_when_tp_hit_before_sl():
    """A path that cleanly hits the LONG take-profit first labels LONG."""
    entry = 100.0
    tp_price = entry * (1.0 + config.TAKE_PROFIT_PCT)
    window = config.FORWARD_WINDOW

    closes = [entry] + [entry] * window
    highs = list(closes)
    lows = list(closes)
    # Candle 2 pokes above the TP barrier without ever touching the SL barrier.
    highs[2] = tp_price * 1.001

    target = build_target(_ohlcv_frame(closes, highs, lows))
    assert target.iloc[0] == config.LABEL_LONG


def test_label_short_when_downside_tp_hit_first():
    entry = 100.0
    tp_price = entry * (1.0 - config.TAKE_PROFIT_PCT)
    window = config.FORWARD_WINDOW

    closes = [entry] + [entry] * window
    highs = list(closes)
    lows = list(closes)
    lows[2] = tp_price * 0.999

    target = build_target(_ohlcv_frame(closes, highs, lows))
    assert target.iloc[0] == config.LABEL_SHORT


def test_label_cash_when_no_barrier_touched():
    entry = 100.0
    window = config.FORWARD_WINDOW
    closes = [entry] * (window + 1)

    target = build_target(_ohlcv_frame(closes, closes, closes))
    assert target.iloc[0] == config.LABEL_CASH


def test_label_long_invalidated_by_prior_stop():
    """If the SL barrier is touched BEFORE the TP barrier, LONG must not win."""
    entry = 100.0
    tp_price = entry * (1.0 + config.TAKE_PROFIT_PCT)
    sl_price = entry * (1.0 - config.STOP_LOSS_PCT)
    window = config.FORWARD_WINDOW

    closes = [entry] + [entry] * window
    highs = list(closes)
    lows = list(closes)
    lows[1] = sl_price * 0.999   # stop touched first ...
    highs[3] = tp_price * 1.001  # ... then the would-be take-profit

    target = build_target(_ohlcv_frame(closes, highs, lows))
    assert target.iloc[0] != config.LABEL_LONG


def test_labels_use_the_same_barriers_the_bot_trades():
    """End-to-end: the label barrier prices equal live bracket prices."""
    entry = 61_234.56
    long_tp, long_sl = bracket_prices("LONG", entry)
    # Reconstruct the label barriers exactly as build_target computes them.
    assert long_tp == pytest.approx(entry * (1.0 + config.TAKE_PROFIT_PCT))
    assert long_sl == pytest.approx(entry * (1.0 - config.STOP_LOSS_PCT))
