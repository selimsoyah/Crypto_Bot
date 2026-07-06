"""Tests for model_brain.decide_direction edge cases (Phase 4)."""

import math

import pytest

import config
import model_brain


@pytest.fixture(autouse=True)
def trend_filter_off(monkeypatch):
    monkeypatch.setattr(config, "USE_TREND_FILTER", False)


def test_at_threshold_is_cash_not_trade():
    assert model_brain.decide_direction(0.50, 0.10, 1.0, 0.50, 0.50) == "CASH"


def test_long_above_threshold():
    assert model_brain.decide_direction(0.60, 0.10, 1.0, 0.50, 0.50) == "LONG"


def test_short_above_threshold():
    assert model_brain.decide_direction(0.10, 0.70, -1.0, 0.50, 0.50) == "SHORT"


def test_both_above_picks_higher_probability():
    assert model_brain.decide_direction(0.80, 0.75, 0.0, 0.50, 0.50) == "LONG"
    assert model_brain.decide_direction(0.76, 0.90, 0.0, 0.50, 0.50) == "SHORT"


def test_trend_filter_blocks_long_in_downtrend(monkeypatch):
    monkeypatch.setattr(config, "USE_TREND_FILTER", True)
    assert model_brain.decide_direction(0.90, 0.05, -0.05, 0.40, 0.40) == "CASH"


def test_trend_filter_blocks_short_in_uptrend(monkeypatch):
    monkeypatch.setattr(config, "USE_TREND_FILTER", True)
    assert model_brain.decide_direction(0.05, 0.90, 0.05, 0.40, 0.40) == "CASH"


def test_trend_filter_override_param(monkeypatch):
    monkeypatch.setattr(config, "USE_TREND_FILTER", True)
    assert (
        model_brain.decide_direction(
            0.90, 0.05, -0.05, 0.40, 0.40, use_trend_filter=False
        )
        == "LONG"
    )


def test_nan_probabilities_treated_as_not_above_threshold():
    assert model_brain.decide_direction(float("nan"), 0.90, 0.0, 0.40, 0.40) == "SHORT"
    assert model_brain.decide_direction(0.90, float("nan"), 0.0, 0.40, 0.40) == "LONG"
    assert (
        model_brain.decide_direction(float("nan"), float("nan"), 0.0, 0.40, 0.40)
        == "CASH"
    )


def test_load_thresholds_fallback(monkeypatch, tmp_path):
    path = tmp_path / "missing.json"
    monkeypatch.setattr(config, "LONG_PROBABILITY_THRESHOLD", 0.34)
    monkeypatch.setattr(config, "SHORT_PROBABILITY_THRESHOLD", 0.35)
    long_thr, short_thr = model_brain.load_thresholds(path=str(path))
    assert long_thr == pytest.approx(0.34)
    assert short_thr == pytest.approx(0.35)


def test_load_thresholds_from_sidecar(monkeypatch, tmp_path):
    path = tmp_path / "decision_threshold.json"
    path.write_text('{"long_threshold": 0.475, "short_threshold": 0.600}')
    long_thr, short_thr = model_brain.load_thresholds(path=str(path))
    assert long_thr == pytest.approx(0.475)
    assert short_thr == pytest.approx(0.600)
