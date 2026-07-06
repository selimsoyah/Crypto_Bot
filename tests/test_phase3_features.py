"""Phase 3 feature variant tests."""

import numpy as np
import pandas as pd
import pytest

from feature_factory import (
    BASELINE_FEATURE_COLUMNS,
    FLOW_FEATURE_COLUMNS,
    MTF_FEATURE_COLUMNS,
    REGIME_FEATURE_COLUMNS,
    TARGET_COLUMN,
    TIME_FEATURE_COLUMNS,
    add_technical_indicators,
    build_feature_matrix,
    feature_columns_for,
    use_feature_variant,
)


@pytest.fixture()
def ohlcv_with_ts():
    n = 400
    rng = np.random.default_rng(7)
    close = 60_000 + np.cumsum(rng.normal(0, 40, n))
    ts = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "Timestamp": ts,
            "Open": close + rng.normal(0, 20, n),
            "High": close + rng.uniform(20, 120, n),
            "Low": close - rng.uniform(20, 120, n),
            "Close": close,
            "Volume": rng.uniform(80, 800, n),
        }
    )


def test_feature_columns_for_counts():
    assert len(feature_columns_for("F0")) == len(BASELINE_FEATURE_COLUMNS)
    assert len(feature_columns_for("F1")) == len(BASELINE_FEATURE_COLUMNS) + len(
        REGIME_FEATURE_COLUMNS
    )
    assert len(feature_columns_for("F5")) == len(BASELINE_FEATURE_COLUMNS) + len(
        REGIME_FEATURE_COLUMNS
    ) + len(MTF_FEATURE_COLUMNS) + len(TIME_FEATURE_COLUMNS) + len(FLOW_FEATURE_COLUMNS)


def test_phase3_regime_columns_present(ohlcv_with_ts):
    out = add_technical_indicators(ohlcv_with_ts, feature_variant="F1")
    for col in REGIME_FEATURE_COLUMNS:
        assert col in out.columns
        assert out[col].dropna().size > 0


def test_phase3_mtf_columns_present(ohlcv_with_ts):
    out = add_technical_indicators(ohlcv_with_ts, feature_variant="F2")
    for col in MTF_FEATURE_COLUMNS:
        assert col in out.columns
        assert out[col].notna().sum() > 100


def test_phase3_time_columns_bounded(ohlcv_with_ts):
    out = add_technical_indicators(ohlcv_with_ts, feature_variant="F3")
    for col in TIME_FEATURE_COLUMNS:
        assert col in out.columns
        assert out[col].between(-1.0, 1.0).all()


def test_phase3_flow_close_in_bar_range(ohlcv_with_ts):
    out = add_technical_indicators(ohlcv_with_ts, feature_variant="F4")
    valid = out["close_in_bar"].dropna()
    assert (valid >= 0).all() and (valid <= 1).all()


def test_use_feature_variant_restores_columns():
    before = list(feature_columns_for("F0"))
    with use_feature_variant("F5"):
        from feature_factory import FEATURE_COLUMNS

        assert len(FEATURE_COLUMNS) > len(before)
    from feature_factory import FEATURE_COLUMNS

    assert FEATURE_COLUMNS == before


def test_build_feature_matrix_f5_includes_all_groups(ohlcv_with_ts):
    matrix = build_feature_matrix(ohlcv_with_ts, feature_variant="F5")
    cols = feature_columns_for("F5")
    for col in cols:
        assert col in matrix.columns
    assert len(matrix) > 200


def test_unknown_variant_raises():
    with pytest.raises(ValueError, match="Unknown feature variant"):
        feature_columns_for("F9")


def test_train_matrix_uses_passed_feature_columns(ohlcv_with_ts):
    """Regression: training must fit on the variant column list, not a stale import."""
    import model_brain

    matrix = build_feature_matrix(ohlcv_with_ts, feature_variant="F2")
    cols = feature_columns_for("F2")
    model = model_brain._build_model()
    model_brain._fit_balanced(model, matrix[cols], matrix[TARGET_COLUMN])
    assert model_brain._model_feature_names(model) == cols
