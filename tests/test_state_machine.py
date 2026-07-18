"""Logging state-machine tests (Phase 0 fix #4).

The historical bug: when a position was closed and a new one opened in the
SAME iteration (signal flip), only the final ``OPEN_*`` row was written — the
``CLOSE_*`` event was silently dropped, corrupting the ledger and the
shutdown report. These tests drive the real ``TradingBot._iteration`` with
mocked exchange/model calls and assert every discrete transition gets its own
row and every completed trade lands in the ground-truth ``trades`` table.
"""

import pandas as pd
import pytest

import bot_loop
import config
import order_execution
from bot_loop import TradingBot
from trade_store import TradeStore

import feature_factory  # noqa: F401 — patched via bot_loop.feature_factory in tests


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    """A TradingBot wired to a temp store with all network calls mocked out."""
    store = TradeStore(db_path=str(tmp_path / "test.db"))
    tb = TradingBot(store=store)
    tb.session_id = "testsession"

    monkeypatch.setattr(config, "USE_POST_ONLY_MAKER", True)
    # No sleeping in tests (the open path pauses 1.5s for balance settling).
    monkeypatch.setattr(bot_loop.time, "sleep", lambda *_: None)

    # Exchange mocks: orders always fill at the requested reference price.
    tb._last_test_price = 60_000.0

    def fake_maker_limit(side, quantity, reduce_only, book=None):
        return order_execution.MakerOrderResult(
            success=True,
            fill_price=tb._last_test_price,
            order={"avgPrice": str(tb._last_test_price)},
        )

    monkeypatch.setattr(tb, "_execute_maker_limit", fake_maker_limit)
    monkeypatch.setattr(
        tb,
        "_fetch_book",
        lambda: order_execution.BookTicker(
            bid=tb._last_test_price - 0.5,
            ask=tb._last_test_price + 0.5,
        ),
    )
    monkeypatch.setattr(tb, "_get_usdt_balance", lambda: 5_000.0)
    monkeypatch.setattr(tb, "_get_total_wallet_balance", lambda: 5_000.0)
    monkeypatch.setattr(tb, "_flatten_exchange_orphans", lambda *a, **k: False)
    monkeypatch.setattr(tb, "_confirm_exchange_flat", lambda: True)
    monkeypatch.setattr(tb, "verify_exchange_alignment", lambda: True)
    tb.risk.begin_session(5_000.0)
    return tb


def _drive_iteration(bot_obj, monkeypatch, price, prob_long, prob_short):
    """Run one real _iteration with the market and model stubbed."""
    bot_obj._last_test_price = price
    candles = pd.DataFrame(
        {
            "Close": [price] * 5,
            "Timestamp": [pd.Timestamp("2025-06-01 12:00:00+00:00")] * 5,
        }
    )
    monkeypatch.setattr(
        bot_loop.data_pipeline, "fetch_latest_candles", lambda: candles
    )
    monkeypatch.setattr(
        bot_loop.model_brain,
        "predict_latest",
        lambda model, c: {
            "prob_long": prob_long,
            "prob_short": prob_short,
            "prob_cash": max(0.0, 1.0 - prob_long - prob_short),
            "trend": 0.0,
        },
    )

    def _fake_live_features(df):
        n = len(df)
        return pd.DataFrame({"atr_pct": [config.RISK_ATR_BASELINE_PCT] * n})

    monkeypatch.setattr(
        feature_factory, "compute_live_features", _fake_live_features
    )
    bot_obj._iteration()


def test_open_writes_its_own_transition_row(bot, monkeypatch):
    _drive_iteration(bot, monkeypatch, price=60_000.0, prob_long=0.90, prob_short=0.05)

    df = bot.store.read_status_df()
    actions = list(df["Action"])
    assert "OPEN_LONG" in actions
    assert bot.state.position is not None
    assert bot.state.position.side == "LONG"


def test_flip_writes_close_and_open_rows_in_same_iteration(bot, monkeypatch):
    """THE regression test: a flip must produce BOTH a CLOSE and an OPEN row."""
    # Iteration 1: open a SHORT.
    _drive_iteration(bot, monkeypatch, price=60_000.0, prob_long=0.05, prob_short=0.90)
    assert bot.state.position is not None and bot.state.position.side == "SHORT"

    # Iteration 2: strong LONG signal -> close the short AND open a long.
    _drive_iteration(bot, monkeypatch, price=60_100.0, prob_long=0.90, prob_short=0.05)

    df = bot.store.read_status_df()
    actions = list(df["Action"])
    assert "CLOSE_SHORT_FLIP" in actions, f"CLOSE row was dropped: {actions}"
    assert "OPEN_LONG" in actions
    # The close must be recorded BEFORE the new open (chronological ledger).
    assert actions.index("CLOSE_SHORT_FLIP") < actions.index("OPEN_LONG")

    # Ground truth: exactly one completed trade so far, outcome FLIP.
    trades = bot.store.read_trades_df(session_id="testsession")
    assert len(trades) == 1
    row = trades.iloc[0]
    assert row["side"] == "SHORT"
    assert row["outcome"] == "FLIP"
    # Short entered at 60000 and closed at 60100 -> a loss.
    assert row["realized_pnl"] < 0


def test_stop_loss_close_recorded_atomically(bot, monkeypatch):
    _drive_iteration(bot, monkeypatch, price=60_000.0, prob_long=0.90, prob_short=0.05)
    pos = bot.state.position
    assert pos is not None

    # Price crashes through the stop-loss bracket; signal goes quiet (CASH).
    crash = pos.stop_loss_price * 0.999
    _drive_iteration(bot, monkeypatch, price=crash, prob_long=0.05, prob_short=0.05)

    assert bot.state.position is None
    trades = bot.store.read_trades_df(session_id="testsession")
    assert len(trades) == 1
    row = trades.iloc[0]
    assert row["outcome"] == "SL"
    assert row["realized_pnl"] < 0
    assert row["entry_price"] == pytest.approx(60_000.0)
    assert row["tp_price"] == pytest.approx(
        60_000.0 * (1.0 + config.TAKE_PROFIT_PCT), rel=1e-6
    )

    df = bot.store.read_status_df()
    assert "CLOSE_LONG_SL" in list(df["Action"])


def test_take_profit_close_records_win(bot, monkeypatch):
    _drive_iteration(bot, monkeypatch, price=60_000.0, prob_long=0.90, prob_short=0.05)
    pos = bot.state.position
    tp_touch = pos.take_profit_price * 1.001
    _drive_iteration(bot, monkeypatch, price=tp_touch, prob_long=0.05, prob_short=0.05)

    trades = bot.store.read_trades_df(session_id="testsession")
    assert len(trades) == 1
    assert trades.iloc[0]["outcome"] == "TP"
    assert trades.iloc[0]["realized_pnl"] > 0


def test_peak_unrealized_tracked_while_holding(bot, monkeypatch):
    _drive_iteration(bot, monkeypatch, price=60_000.0, prob_long=0.90, prob_short=0.05)
    pos = bot.state.position

    # Hold through a profitable tick that does NOT touch the TP bracket.
    up_tick = min(60_300.0, pos.take_profit_price * 0.999)
    _drive_iteration(bot, monkeypatch, price=up_tick, prob_long=0.05, prob_short=0.05)
    assert bot.state.position is not None  # still open
    expected_peak = (up_tick - 60_000.0) * pos.quantity
    assert pos.peak_unrealized == pytest.approx(expected_peak, rel=1e-6)

    # Now stop out; the recorded trade must remember the peak.
    crash = pos.stop_loss_price * 0.999
    _drive_iteration(bot, monkeypatch, price=crash, prob_long=0.05, prob_short=0.05)
    trades = bot.store.read_trades_df(session_id="testsession")
    assert trades.iloc[0]["peak_unrealized"] == pytest.approx(expected_peak, rel=1e-6)


def test_heartbeat_not_duplicated_on_transition(bot, monkeypatch):
    """A transition iteration must not also write a redundant heartbeat row."""
    _drive_iteration(bot, monkeypatch, price=60_000.0, prob_long=0.90, prob_short=0.05)
    df = bot.store.read_status_df()
    # Exactly one row for the open iteration (the OPEN transition itself).
    assert len(df) == 1

    # An idle iteration writes exactly one heartbeat row.
    _drive_iteration(bot, monkeypatch, price=60_010.0, prob_long=0.05, prob_short=0.05)
    df = bot.store.read_status_df()
    assert len(df) == 2


def test_timeout_exit_after_forward_window(bot, monkeypatch):
    """Audit parity: force-close when position ages past FORWARD_WINDOW bars."""
    monkeypatch.setattr(config, "EXECUTION_AUDIT_PARITY", True)
    monkeypatch.setattr(config, "FORWARD_WINDOW", 2)
    monkeypatch.setattr(config, "INTERVAL", "15m")

    candles = pd.DataFrame(
        {
            "Close": [60_000.0],
            "Timestamp": [pd.Timestamp("2025-01-01 00:00:00+00:00")],
        }
    )
    monkeypatch.setattr(
        bot_loop.data_pipeline, "fetch_latest_candles", lambda: candles
    )
    monkeypatch.setattr(
        bot_loop.model_brain,
        "predict_latest",
        lambda model, c: {
            "prob_long": 0.90,
            "prob_short": 0.05,
            "prob_cash": 0.05,
            "trend": 0.0,
        },
    )

    def _fake_live_features(df):
        return pd.DataFrame({"atr_pct": [config.RISK_ATR_BASELINE_PCT] * len(df)})

    monkeypatch.setattr(feature_factory, "compute_live_features", _fake_live_features)
    bot._iteration()
    assert bot.state.position is not None

    aged = pd.DataFrame(
        {
            "Close": [60_010.0],
            "Timestamp": [pd.Timestamp("2025-01-01 00:30:00+00:00")],
        }
    )
    monkeypatch.setattr(bot_loop.data_pipeline, "fetch_latest_candles", lambda: aged)
    bot._iteration()

    assert bot.state.position is None
    trades = bot.store.read_trades_df(session_id="testsession")
    assert len(trades) == 1
    assert trades.iloc[0]["outcome"] == "TIMEOUT"


def test_spread_gate_blocks_entry(bot, monkeypatch):
    _drive_iteration(bot, monkeypatch, price=60_000.0, prob_long=0.90, prob_short=0.05)
    assert bot.state.position is not None

    bot.state.position = None
    monkeypatch.setattr(
        bot,
        "_fetch_book",
        lambda: order_execution.BookTicker(bid=60_000.0, ask=60_100.0),
    )
    _drive_iteration(bot, monkeypatch, price=60_000.0, prob_long=0.90, prob_short=0.05)
    assert bot.state.position is None
    df = bot.store.read_status_df()
    actions = df["Action"].astype(str).tolist()
    reasons = df["Reason"].astype(str).tolist()
    assert any("SKIPPED_LONG_SPREAD" in a for a in actions) or any(
        "Spread too wide" in r for r in reasons
    )


def test_instance_lock_blocks_second_engine(tmp_path):
    """Two engines must never run against the same account simultaneously."""
    lock_a = bot_loop.InstanceLock(path=str(tmp_path / "lock"))
    lock_b = bot_loop.InstanceLock(path=str(tmp_path / "lock"))
    assert lock_a.acquire() is True
    assert lock_b.acquire() is False, "second engine acquired the lock!"
    lock_a.release()
    assert lock_b.acquire() is True
    lock_b.release()
