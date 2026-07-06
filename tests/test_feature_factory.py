"""Feature factory determinism tests (Phase 4)."""

import numpy as np
import pandas as pd
import pytest

from feature_factory import add_technical_indicators, compute_live_features


@pytest.fixture()
def ohlcv_frame():
    n = 300
    rng = np.random.default_rng(42)
    close = 60_000 + np.cumsum(rng.normal(0, 50, n))
    high = close + rng.uniform(10, 200, n)
    low = close - rng.uniform(10, 200, n)
    open_ = close + rng.normal(0, 30, n)
    volume = rng.uniform(50, 500, n)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}
    )


def test_add_technical_indicators_deterministic(ohlcv_frame):
    a = add_technical_indicators(ohlcv_frame)
    b = add_technical_indicators(ohlcv_frame)
    pd.testing.assert_frame_equal(a, b)


def test_compute_live_features_has_atr_pct(ohlcv_frame):
    enriched = compute_live_features(ohlcv_frame)
    assert "atr_pct" in enriched.columns
    valid = enriched["atr_pct"].dropna()
    assert len(valid) > 0
    assert (valid > 0).all()


def test_compute_live_features_does_not_mutate_input(ohlcv_frame):
    before = ohlcv_frame.copy()
    compute_live_features(ohlcv_frame)
    pd.testing.assert_frame_equal(before, ohlcv_frame)


def test_missing_columns_raises():
    bad = pd.DataFrame({"Close": [1.0, 2.0]})
    with pytest.raises(ValueError, match="missing required columns"):
        add_technical_indicators(bad)
