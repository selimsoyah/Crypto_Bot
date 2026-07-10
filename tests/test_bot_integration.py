"""Extended mocked integration tests for TradingBot._iteration (Phase 4)."""

import pandas as pd
import pytest

import bot_loop
import config
from conftest import drive_iteration


def test_connection_degraded_blocks_new_entry(bot, monkeypatch):
    drive_iteration(bot, monkeypatch, 60_000.0, 0.90, 0.05)
    pos = bot.state.position
    assert pos is not None
    # Close to flat so the next iteration attempts a new entry.
    drive_iteration(bot, monkeypatch, pos.take_profit_price * 1.001, 0.05, 0.05)
    assert bot.state.position is None

    bot.state.connection_degraded = True
    bot.state.connection_error = "simulated outage"
    drive_iteration(bot, monkeypatch, 60_000.0, 0.90, 0.05)

    df = bot.store.read_status_df()
    actions = df["Action"].astype(str).tolist()
    assert any("BLOCKED" in a for a in actions)
    assert bot.state.position is None


def test_bracket_tp_still_closes_when_degraded(bot, monkeypatch):
    drive_iteration(bot, monkeypatch, 60_000.0, 0.90, 0.05)
    pos = bot.state.position
    bot.state.connection_degraded = True
    tp_touch = pos.take_profit_price * 1.001
    drive_iteration(bot, monkeypatch, tp_touch, 0.05, 0.05)
    assert bot.state.position is None
    trades = bot.store.read_trades_df(session_id="testsession")
    assert len(trades) == 1
    assert trades.iloc[0]["outcome"] == "TP"


def test_consecutive_loss_pause_blocks_entry(bot, monkeypatch):
    for i in range(config.RISK_MAX_CONSECUTIVE_LOSSES):
        drive_iteration(bot, monkeypatch, 60_000.0, 0.90, 0.05)
        pos = bot.state.position
        crash = pos.stop_loss_price * 0.999
        drive_iteration(bot, monkeypatch, crash, 0.05, 0.05)

    assert bot.risk.snapshot().manual_resume_required is True
    drive_iteration(bot, monkeypatch, 60_000.0, 0.90, 0.05)
    df = bot.store.read_status_df()
    actions = df["Action"].astype(str).tolist()
    assert any("BLOCKED" in a for a in actions)
    assert bot.state.position is None


def test_empty_candles_sets_degraded(bot, monkeypatch):
    monkeypatch.setattr(
        bot_loop.data_pipeline, "fetch_latest_candles", lambda: pd.DataFrame()
    )
    bot._iteration()
    assert bot.state.connection_degraded is True
    df = bot.store.read_status_df()
    assert len(df) >= 1
    assert df.iloc[-1]["Event"] == "SCAN"


def test_iteration_writes_status_without_deadlock(bot, monkeypatch):
    """Regression: balance fetch must not run under state._lock (nested lock hang)."""

    def balance_fetch():
        bot._mark_api_success()
        return 5_000.0

    monkeypatch.setattr(bot, "_get_usdt_balance", balance_fetch)
    drive_iteration(bot, monkeypatch, 60_000.0, 0.05, 0.05)
    df = bot.store.read_status_df()
    assert len(df) >= 1
    assert df.iloc[-1]["Action"] == "SCAN"


def test_insufficient_balance_blocks_order(bot, monkeypatch):
    monkeypatch.setattr(bot, "_get_usdt_balance", lambda: 10.0)
    monkeypatch.setattr(bot, "_get_total_wallet_balance", lambda: 10.0)
    drive_iteration(bot, monkeypatch, 60_000.0, 0.90, 0.05)
    assert bot.state.position is None
    df = bot.store.read_status_df()
    actions = df["Action"].astype(str).tolist()
    assert any("BLOCKED" in a for a in actions)


def test_full_lifecycle_open_hold_close(bot, monkeypatch):
    drive_iteration(bot, monkeypatch, 60_000.0, 0.90, 0.05)
    assert bot.state.position.side == "LONG"

    drive_iteration(bot, monkeypatch, 60_050.0, 0.05, 0.05)
    assert bot.state.position is not None

    pos = bot.state.position
    drive_iteration(bot, monkeypatch, pos.take_profit_price * 1.001, 0.05, 0.05)
    assert bot.state.position is None

    trades = bot.store.read_trades_df(session_id="testsession")
    assert len(trades) == 1
    assert trades.iloc[0]["realized_pnl"] > 0
