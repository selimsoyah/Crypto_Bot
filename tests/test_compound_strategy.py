"""Tests for Path B compound_strategy module."""

import pytest

import config
import compound_strategy
from bot_loop import Position, bracket_prices


@pytest.fixture(autouse=True)
def compound_profile(monkeypatch):
    monkeypatch.setattr(config, "TRADING_PROFILE", "COMPOUND")
    monkeypatch.setattr(config, "TAKE_PROFIT_PCT", 0.006)
    monkeypatch.setattr(config, "STOP_LOSS_PCT", 0.003)
    monkeypatch.setattr(config, "USE_ATR_BRACKETS", True)
    monkeypatch.setattr(config, "TRAILING_STOP_ENABLED", True)
    monkeypatch.setattr(config, "EXECUTION_AUDIT_PARITY", False)
    monkeypatch.setattr(config, "TRAILING_STOP_ACTIVATION_PCT", 0.004)
    monkeypatch.setattr(config, "TRAILING_STOP_DISTANCE_PCT", 0.002)


def test_effective_brackets_long_fixed(monkeypatch):
    monkeypatch.setattr(config, "USE_ATR_BRACKETS", False)
    levels = compound_strategy.effective_brackets("LONG", 100_000.0)
    assert levels.take_profit == pytest.approx(100_600.0)
    assert levels.stop_loss == pytest.approx(99_700.0)


def test_effective_brackets_scales_with_atr():
    levels = compound_strategy.effective_brackets("LONG", 100_000.0, atr_pct=0.01)
    assert levels.tp_pct > config.TAKE_PROFIT_PCT * 0.5
    assert levels.take_profit > 100_000.0


def test_trailing_stop_ratchet_long():
    pos = Position(
        side="LONG",
        entry_price=100.0,
        quantity=1.0,
        entry_time="t",
        entry_candle_ts="2025-01-01 00:00:00+00:00",
        take_profit_price=101.0,
        stop_loss_price=99.5,
        best_price=100.0,
    )
    compound_strategy.update_trailing_stop(pos, 100.3)
    assert pos.trail_active is False
    compound_strategy.update_trailing_stop(pos, 100.5)
    assert pos.trail_active is True
    assert pos.stop_loss_price > 99.5


def test_compound_size_multiplier_win_streak():
    mult = compound_strategy.compound_size_multiplier(2, 0)
    assert mult == pytest.approx(1.1025, rel=1e-3)


def test_compound_size_multiplier_loss_streak():
    mult = compound_strategy.compound_size_multiplier(0, 2)
    assert mult == pytest.approx(0.81, rel=1e-3)


def test_threshold_distance():
    d = compound_strategy.threshold_distance(0.35, 0.40, 0.38, 0.38)
    assert d["long_gap"] == pytest.approx(0.03)
    assert d["short_gap"] == pytest.approx(0.0)
    assert d["short_ready"] is True


def test_bracket_prices_delegates():
    tp, sl = bracket_prices("SHORT", 50_000.0)
    assert tp < 50_000.0
    assert sl > 50_000.0
