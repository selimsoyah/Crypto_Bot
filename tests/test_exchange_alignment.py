"""Tests for exchange alignment safety net and ReduceOnly hardening."""

from unittest.mock import MagicMock

import pytest

import config
import order_execution
from bot_loop import Position, TradingBot
from trade_store import TradeStore


def test_is_reduce_only_reject_detects_code():
    class FakeApiError(Exception):
        code = -2022

    assert order_execution.is_reduce_only_reject(FakeApiError("ReduceOnly Order is rejected."))
    assert order_execution.is_reduce_only_reject(Exception("APIError(code=-2022): ReduceOnly"))
    assert not order_execution.is_reduce_only_reject(Exception("timeout"))


def test_round_quantity_floors_to_step():
    assert order_execution.round_quantity(0.1239, 0.001, 3) == pytest.approx(0.123)
    assert order_execution.round_quantity(0.0, 0.001, 3) == 0.0


def test_verify_exchange_alignment_panic_on_orphan(tmp_path, monkeypatch):
    store = TradeStore(db_path=str(tmp_path / "align.db"))
    bot = TradingBot(store=store, _skip_session_recovery=True)
    bot._client = MagicMock()
    bot.state.last_price = 64_000.0
    # Local FLAT, exchange SHORT — catastrophic ghost.
    monkeypatch.setattr(
        order_execution,
        "fetch_open_position",
        lambda *a, **k: {"side": "SHORT", "quantity": 0.08, "entry_price": 62_000.0},
    )
    flattened = {"n": 0}

    def fake_flatten(*a, **k):
        flattened["n"] += 1
        monkeypatch.setattr(
            order_execution,
            "fetch_open_position",
            lambda *a, **k: {"side": "FLAT", "quantity": 0.0, "entry_price": 0.0},
        )
        return True

    monkeypatch.setattr(bot, "_flatten_exchange_orphans", fake_flatten)
    monkeypatch.setattr(config, "EXCHANGE_ALIGNMENT_CHECK", True)

    ok = bot.verify_exchange_alignment()
    assert flattened["n"] == 1
    assert bot.state.position is None
    assert bot.risk.state.manual_resume_required is True
    assert ok is True


def test_verify_exchange_alignment_no_reentry(tmp_path, monkeypatch):
    store = TradeStore(db_path=str(tmp_path / "align2.db"))
    bot = TradingBot(store=store, _skip_session_recovery=True)
    bot._client = MagicMock()
    bot._alignment_panic_active = True
    monkeypatch.setattr(config, "EXCHANGE_ALIGNMENT_CHECK", True)
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        return {"side": "SHORT", "quantity": 1.0, "entry_price": 1.0}

    monkeypatch.setattr(order_execution, "fetch_open_position", boom)
    assert bot.verify_exchange_alignment() is False
    assert called["n"] == 0  # re-entrancy guard blocked fetch


def test_close_timeout_does_not_clear_if_exchange_still_open(tmp_path, monkeypatch):
    store = TradeStore(db_path=str(tmp_path / "close.db"))
    bot = TradingBot(store=store, _skip_session_recovery=True)
    bot.session_id = "s1"
    bot._client = MagicMock()
    bot.state.position = Position(
        side="SHORT",
        entry_price=62_000.0,
        quantity=0.08,
        entry_time="2026-07-10 12:00:00",
        entry_candle_ts="2026-07-10 12:00:00",
        take_profit_price=61_000.0,
        stop_loss_price=63_000.0,
        quantity_open=0.08,
    )
    monkeypatch.setattr(config, "USE_POST_ONLY_MAKER", True)
    monkeypatch.setattr(
        bot,
        "_execute_maker_limit",
        lambda *a, **k: order_execution.MakerOrderResult(success=True, fill_price=62_100.0),
    )
    # Pre-close sync sees SHORT; post-close confirm stays SHORT; flatten fails.
    monkeypatch.setattr(
        order_execution,
        "fetch_open_position",
        lambda *a, **k: {"side": "SHORT", "quantity": 0.08, "entry_price": 62_000.0},
    )
    monkeypatch.setattr(bot, "_flatten_exchange_orphans", lambda *a, **k: False)
    monkeypatch.setattr(bot, "_confirm_exchange_flat", lambda: False)

    bot._close_position(62_100.0, reason_code="TIMEOUT")

    assert bot.state.position is not None  # must NOT ghost-clear
    assert store.read_trades_df().empty


def test_flatten_uses_exchange_side_on_mismatch(monkeypatch):
    client = MagicMock()
    calls = []

    def fake_fetch(c, symbol):
        return {"side": "SHORT", "quantity": 0.05, "entry_price": 100.0}

    def fake_retry(fn, *a, **kw):
        calls.append(kw)
        return {"avgPrice": "100", "status": "FILLED"}

    monkeypatch.setattr(order_execution, "fetch_open_position", fake_fetch)
    monkeypatch.setattr(order_execution.exchange_client, "call_with_retry", fake_retry)

    order_execution.flatten_position_market(
        client,
        symbol="BTCUSDT",
        quantity=0.05,
        position_side="LONG",  # wrong caller side
        step_size=0.001,
        qty_precision=3,
    )
    assert calls
    assert calls[0]["side"] == "BUY"  # close SHORT → BUY
    assert calls[0]["reduceOnly"] is True
