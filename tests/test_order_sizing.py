"""Order sizing and fill-price helper tests (Phase 4)."""

import pytest

import config
from bot_loop import TradingBot


@pytest.fixture()
def tb():
    return TradingBot(store=None)


def test_round_step_respects_lot_size(tb):
    tb._step_size = 0.001
    tb._qty_precision = 3
    assert tb._round_step(0.123456) == pytest.approx(0.123)


def test_round_step_zero_step_uses_precision(tb):
    tb._step_size = 0.0
    tb._qty_precision = 2
    assert tb._round_step(1.239) == pytest.approx(1.24)


def test_extract_fill_price_from_avg(tb):
    order = {"avgPrice": "60123.45"}
    assert tb._extract_fill_price(order, fallback=60_000.0) == pytest.approx(60123.45)


def test_extract_fill_price_from_fills(tb):
    order = {
        "fills": [
            {"price": "60000", "qty": "0.001"},
            {"price": "60200", "qty": "0.001"},
        ]
    }
    assert tb._extract_fill_price(order, fallback=0.0) == pytest.approx(60100.0)


def test_extract_fill_price_fallback(tb):
    assert tb._extract_fill_price({}, fallback=59_999.0) == pytest.approx(59_999.0)


def test_compute_order_margin_delegates_to_risk(tb):
    tb.risk.begin_session(10_000.0)
    tb._min_notional = 50.0
    sizing = tb._compute_order_margin(10_000.0, 0.008)
    expected = 10_000.0 * config.CASH_ALLOCATION_PCT
    assert sizing.margin_usdt == pytest.approx(expected)
    assert not sizing.exchange_floor_applied
    assert sizing.notional_usdt == pytest.approx(sizing.margin_usdt * config.LEVERAGE)


def test_compute_order_margin_uses_percentage_not_legacy_floor(tb):
    """Small account where % sizing exceeds exchange min — no floor bump."""
    tb.risk.begin_session(1_000.0)
    tb._min_notional = 50.0
    sizing = tb._compute_order_margin(1_000.0, config.RISK_ATR_BASELINE_PCT)
    expected = 1_000.0 * config.CASH_ALLOCATION_PCT
    assert sizing.margin_usdt == pytest.approx(expected)
    assert sizing.margin_usdt > 50.0 / config.LEVERAGE
    assert not sizing.exchange_floor_applied
