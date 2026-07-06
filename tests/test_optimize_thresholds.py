"""Tests for threshold optimizer edge-selection rules."""

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import config
import model_brain
from feature_factory import TARGET_COLUMN


@pytest.fixture()
def minimal_valid_df():
    n = 200
    return pd.DataFrame(
        {
            TARGET_COLUMN: np.zeros(n, dtype=int),
            "Close": np.linspace(60_000, 61_000, n),
            "High": np.linspace(60_100, 61_100, n),
            "Low": np.linspace(59_900, 60_900, n),
            "price_vs_ema200": np.zeros(n),
        }
    )


def test_optimize_thresholds_picks_positive_validation_pnl(minimal_valid_df):
    proba = np.full((len(minimal_valid_df), 3), 1.0 / 3.0)

    def fake_bt(_valid, _proba, long_threshold, short_threshold, **kwargs):
        isolate = config.THRESHOLD_DISABLED
        if short_threshold >= isolate:
            profit, n_dir = (5.0, 50) if long_threshold <= 0.5 else (-10.0, 50)
            return profit, 0.0, n_dir, 0, np.array([1.0])
        profit, n_dir = (3.0, 50) if short_threshold <= 0.5 else (-8.0, 50)
        return profit, 0.0, 0, n_dir, np.array([1.0])

    with patch.object(model_brain, "backtest_directional", side_effect=fake_bt):
        long_thr, short_thr, long_pnl, short_pnl = model_brain.optimize_thresholds(
            minimal_valid_df, proba
        )

    assert long_thr <= 0.5
    assert short_thr <= 0.5
    assert long_pnl == pytest.approx(5.0)
    assert short_pnl == pytest.approx(3.0)


def test_optimize_thresholds_falls_back_when_all_validation_pnl_negative(minimal_valid_df):
    proba = np.full((len(minimal_valid_df), 3), 1.0 / 3.0)

    def fake_bt(_valid, _proba, long_threshold, short_threshold, **kwargs):
        return -5.0, 0.0, 60, 60, np.array([0.99])

    with patch.object(model_brain, "backtest_directional", side_effect=fake_bt):
        long_thr, short_thr, long_pnl, short_pnl = model_brain.optimize_thresholds(
            minimal_valid_df, proba
        )

    assert long_thr == pytest.approx(config.LONG_FALLBACK_THRESHOLD)
    assert short_thr == pytest.approx(config.SHORT_FALLBACK_THRESHOLD)
    assert long_pnl == 0.0
    assert short_pnl == 0.0


def test_optimize_thresholds_falls_back_on_insufficient_trade_count(minimal_valid_df):
    proba = np.full((len(minimal_valid_df), 3), 1.0 / 3.0)

    def fake_bt(_valid, _proba, long_threshold, short_threshold, **kwargs):
        isolate = config.THRESHOLD_DISABLED
        if short_threshold >= isolate:
            return 100.0, 0.0, 5, 0, np.array([1.0])
        return 100.0, 0.0, 0, 5, np.array([1.0])

    with patch.object(model_brain, "backtest_directional", side_effect=fake_bt):
        long_thr, short_thr, _, _ = model_brain.optimize_thresholds(
            minimal_valid_df, proba
        )

    assert long_thr == pytest.approx(config.LONG_FALLBACK_THRESHOLD)
    assert short_thr == pytest.approx(config.SHORT_FALLBACK_THRESHOLD)
