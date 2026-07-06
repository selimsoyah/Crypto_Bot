"""Unit tests for the Phase 2 risk engine circuit breakers."""

import os

import pytest

import config
from risk_engine import RiskEngine


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    """RiskEngine with isolated kill-switch / resume flag files."""
    monkeypatch.setattr(config, "KILL_SWITCH_FILE", str(tmp_path / "kill"))
    monkeypatch.setattr(config, "RISK_MANUAL_RESUME_FILE", str(tmp_path / "resume"))
    return RiskEngine()


def test_begin_session_resets_state(engine):
    engine.begin_session(10_000.0)
    snap = engine.snapshot()
    assert snap.session_start_equity == 10_000.0
    assert snap.consecutive_losses == 0
    assert snap.halted is False
    assert snap.manual_resume_required is False


def test_session_loss_limit_trips(engine):
    engine.begin_session(10_000.0)
    # -3% exactly at default limit
    decision = engine.check_session_loss_limit(realized=-200.0, unrealized=-100.0)
    assert decision.allowed is False
    assert decision.flatten_positions is True
    assert decision.halt_new_orders is True
    assert "SESSION LOSS LIMIT" in decision.reason


def test_session_loss_under_limit_passes(engine):
    engine.begin_session(10_000.0)
    decision = engine.check_session_loss_limit(realized=-100.0, unrealized=-50.0)
    assert decision.allowed is True


def test_consecutive_losses_pause_until_manual_resume(engine, tmp_path):
    engine.begin_session(10_000.0)
    for _ in range(config.RISK_MAX_CONSECUTIVE_LOSSES - 1):
        d = engine.record_trade_close(-10.0)
        assert d.allowed is True

    d = engine.record_trade_close(-10.0)
    assert d.allowed is False
    assert d.halt_new_orders is True
    assert engine.snapshot().manual_resume_required is True
    assert os.path.exists(config.RISK_MANUAL_RESUME_FILE)

    gate = engine.check_can_open()
    assert gate.allowed is False

    resume = engine.confirm_manual_resume()
    assert resume.allowed is True
    assert engine.snapshot().manual_resume_required is False
    assert engine.check_can_open().allowed is True


def test_winning_trade_resets_consecutive_streak(engine):
    engine.begin_session(10_000.0)
    engine.record_trade_close(-5.0)
    engine.record_trade_close(-5.0)
    engine.record_trade_close(20.0)
    assert engine.snapshot().consecutive_losses == 0


def test_kill_switch_blocks_and_flattens(engine, tmp_path):
    engine.begin_session(5_000.0)
    engine.trigger_kill_switch("test halt")
    assert os.path.exists(config.KILL_SWITCH_FILE)

    decision = engine.check_kill_switch()
    assert decision.allowed is False
    assert decision.flatten_positions is True
    assert engine.check_can_open().allowed is False


def test_clear_kill_switch_removes_file(engine):
    engine.trigger_kill_switch("test")
    engine.clear_kill_switch()
    assert not engine.kill_switch_file_active()
    assert engine.snapshot().kill_switch_active is False


def test_order_sanity_rejects_bad_price(engine):
    engine.begin_session(5_000.0)
    engine.update_last_good_price(60_000.0)
    bad = engine.validate_order_sanity(63_000.0, 0.01, 630.0)
    assert bad.allowed is False
    assert "deviates" in bad.reason.lower()

    ok = engine.validate_order_sanity(60_050.0, 0.01, 600.5)
    assert ok.allowed is True


def test_volatility_sizing_scales_down_in_high_atr(engine):
    engine.begin_session(10_000.0)
    baseline = engine.compute_position_size(10_000.0, config.RISK_ATR_BASELINE_PCT)
    high_vol = engine.compute_position_size(10_000.0, config.RISK_ATR_BASELINE_PCT * 4)

    assert high_vol.margin_usdt < baseline.margin_usdt
    assert high_vol.vol_scale == pytest.approx(config.RISK_VOL_SCALE_FLOOR)
    assert baseline.vol_scale == pytest.approx(1.0)
    assert not baseline.exchange_floor_applied


def test_percentage_sizing_not_overridden_by_legacy_floor(engine):
    """$5k @ 12% should be $600 margin — not bumped to MIN_ORDER_USDT_FLOOR ($60)."""
    engine.begin_session(5_000.0)
    sizing = engine.compute_position_size(5_000.0, config.RISK_ATR_BASELINE_PCT)
    expected = 5_000.0 * config.CASH_ALLOCATION_PCT
    assert sizing.margin_usdt == pytest.approx(expected)
    assert sizing.intended_margin_usdt == pytest.approx(expected)
    assert not sizing.exchange_floor_applied


def test_exchange_min_floor_only_when_below_minimum(engine):
    """Tiny wallet: clamp to exchange min margin, not the legacy $60 floor."""
    engine.begin_session(100.0)
    sizing = engine.compute_position_size(
        100.0, config.RISK_ATR_BASELINE_PCT * 10, exchange_min_notional=50.0
    )
    min_margin = 50.0 / config.LEVERAGE
    assert sizing.intended_margin_usdt < min_margin
    assert sizing.margin_usdt == pytest.approx(min_margin)
    assert sizing.exchange_floor_applied
    assert sizing.margin_usdt < config.MIN_ORDER_USDT_FLOOR
