"""Dashboard statistics honesty tests (Phase 4)."""

import pandas as pd
import pytest

import config
import dashboard_stats
from trade_store import StatusRow, TradeStore


def _log_row(**kwargs) -> dict:
    base = {
        "Current_Balance": 5000.0,
        "Open_Position": "LONG",
        "Prob_Long": 0.5,
        "Prob_Short": 0.3,
        "Prob_Cash": 0.2,
        "Direction": "LONG",
        "Unrealized_PNL": 25.0,
        "Current_Price": 60100.0,
        "Entry_Price": 60000.0,
    }
    base.update(kwargs)
    return base


def test_empty_log_sets_data_warning():
    stats = dashboard_stats.compute_stats(pd.DataFrame(), pd.DataFrame())
    assert stats["data_warnings"]
    assert stats["balance"] == 0.0


def test_win_rate_counts_profitable_flip():
    trades = pd.DataFrame(
        [
            {"outcome": "TP", "realized_pnl": 10.0, "side": "LONG"},
            {"outcome": "FLIP", "realized_pnl": 5.0, "side": "SHORT"},
            {"outcome": "SL", "realized_pnl": -3.0, "side": "LONG"},
        ]
    )
    closed = dashboard_stats.compute_closed_trade_stats(trades)
    assert closed["wins"] == 2
    assert closed["win_rate"] == pytest.approx(100.0 * 2 / 3)


def test_fetch_live_position_error_not_flat(monkeypatch):
    class BadClient:
        def futures_position_information(self, symbol):
            raise ConnectionError("timeout")

    monkeypatch.setattr(config, "API_KEY", "real_key_abc123")
    monkeypatch.setattr(config, "SECRET_KEY", "real_sec_def456")
    result = dashboard_stats.fetch_live_position(client=BadClient())
    assert result["status"] == "error"


def test_fetch_live_position_parses_open_long(monkeypatch):
    class MockClient:
        def futures_position_information(self, symbol):
            return [
                {
                    "positionAmt": "0.010",
                    "entryPrice": "60000",
                    "markPrice": "60100",
                    "unRealizedProfit": "10",
                    "leverage": "3",
                }
            ]

    monkeypatch.setattr(config, "API_KEY", "real_key_abc123")
    monkeypatch.setattr(config, "SECRET_KEY", "real_sec_def456")
    pos = dashboard_stats.fetch_live_position(client=MockClient())
    assert pos["status"] == "ok"
    assert pos["side"] == "LONG"
    assert pos["unrealized_pnl"] == pytest.approx(10.0)


def test_reconcile_prefers_exchange_over_log():
    log = pd.DataFrame([_log_row(Unrealized_PNL=25.0)])
    exchange = {
        "status": "ok",
        "side": "LONG",
        "unrealized_pnl": 30.0,
        "pct_change": 0.5,
        "mark_price": 60300.0,
    }
    live = dashboard_stats.reconcile_floating_pnl(log, exchange)
    assert live["source"] == "exchange"
    assert live["unrealized_pnl"] == pytest.approx(30.0)


def test_reconcile_marks_stale_log_on_exchange_error():
    log = pd.DataFrame([_log_row()])
    exchange = {"status": "error", "message": "API down"}
    live = dashboard_stats.reconcile_floating_pnl(log, exchange)
    assert live["source"] == "log_stale"
    assert live["open"] is True
    assert "API down" in live["warning"]


def test_session_risk_pnl_includes_unrealized(bot):
    from bot_loop import Position

    bot.state.position = Position(
        side="LONG",
        entry_price=60_000.0,
        quantity=0.01,
        entry_time="2026-07-05 12:00:00",
        entry_candle_ts="2026-07-05 12:00:00+00:00",
        take_profit_price=61_000.0,
        stop_loss_price=59_000.0,
    )
    bot.state.last_price = 60_100.0
    bot.state.realized_pnl = -10.0
    pnl_pct, realized, unrealized = dashboard_stats.compute_session_risk_pnl(bot)
    assert unrealized == pytest.approx(1.0)
    assert pnl_pct == pytest.approx((realized + unrealized) / 5_000.0)


def test_trades_to_log_rows_won_lost_colors():
    trades = pd.DataFrame(
        [
            {
                "side": "LONG",
                "entry_ts": "2026-07-05 08:00:00",
                "exit_ts": "2026-07-05 08:05:32",
                "entry_price": 54_000.0,
                "exit_price": 54_500.0,
                "realized_pnl": 9.84,
            },
            {
                "side": "SHORT",
                "entry_ts": "2026-07-05 08:10:00",
                "exit_ts": "2026-07-05 08:15:00",
                "entry_price": 60_000.0,
                "exit_price": 60_200.0,
                "realized_pnl": -13.65,
            },
        ]
    )
    rows = dashboard_stats.trades_to_log_rows(trades)
    assert len(rows) == 2
    assert rows[0]["status"] == "WON"
    assert rows[0]["side"] == "Up"
    assert rows[0]["entry"] == pytest.approx(54_000.0)
    assert rows[0]["exit"] == pytest.approx(54_500.0)
    assert rows[0]["won"] is True
    assert rows[1]["status"] == "LOST"
    assert rows[1]["side"] == "Down"
    assert rows[1]["won"] is False


def test_essential_metrics_includes_wallet_from_log():
    trades = pd.DataFrame()
    log = pd.DataFrame([{"Current_Balance": 4321.5, "Direction": "LONG", "Open_Position": "FLAT"}])
    m = dashboard_stats.essential_metrics(trades, log)
    assert m["wallet_balance"] == pytest.approx(4321.5)


def test_status_to_activity_rows_includes_scan_heartbeats():
    log = pd.DataFrame(
        [
            {
                "Timestamp": "2026-07-06 12:00:00",
                "Action": "HOLD",
                "Event": "WAIT",
                "Open_Position": "FLAT",
                "Reason": "legacy heartbeat (hidden)",
            },
            {
                "Timestamp": "2026-07-06 12:00:05",
                "Action": "SCAN",
                "Event": "SCAN",
                "Open_Position": "FLAT",
                "Reason": "Scan Complete: Model checked, staying FLAT",
            },
            {
                "Timestamp": "2026-07-06 12:01:00",
                "Action": "SKIPPED_LONG_POST_ONLY",
                "Event": "WARNING",
                "Open_Position": "FLAT",
                "Reason": "Post-only order rejected",
            },
        ]
    )
    rows = dashboard_stats.status_to_activity_rows(log)
    assert len(rows) == 2
    assert rows[0]["action"] == "SCAN"
    assert rows[0]["tone"] == "scan"
    assert rows[1]["action"] == "SKIPPED_LONG_POST_ONLY"
    assert rows[1]["tone"] == "warn"


def test_open_position_row_from_exchange():
    row = dashboard_stats.open_position_row(
        {
            "status": "ok",
            "side": "LONG",
            "entry_price": 60_000.0,
            "mark_price": 59_900.0,
            "unrealized_pnl": -0.76,
        }
    )
    assert row is not None
    assert row["status"] == "OPEN"
    assert row["pnl"] == pytest.approx(-0.76)


def test_position_mismatch_warning_when_exchange_open_log_flat():
    log = pd.DataFrame([{"Open_Position": "FLAT"}])
    exchange = {"status": "ok", "side": "LONG"}
    msg = dashboard_stats.position_mismatch_warning(log, exchange)
    assert "open LONG" in msg


def test_bot_health_live_when_recent_log(bot):
    log = pd.DataFrame(
        [
            {
                "Timestamp": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S"),
                "Action": "HOLD",
                "Event": "WAIT",
                "Open_Position": "FLAT",
            }
        ]
    )
    bot.state.running = True
    h = dashboard_stats.bot_health(bot, log)
    assert h["status"] == "LIVE"
    assert h["running"] is True


def test_bot_health_uses_exchange_position_when_open(bot):
    log = pd.DataFrame(
        [
            {
                "Timestamp": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S"),
                "Action": "SKIPPED_LONG_POST_ONLY",
                "Event": "WARNING",
                "Open_Position": "FLAT",
            }
        ]
    )
    exchange = {"status": "ok", "side": "LONG", "unrealized_pnl": -0.76}
    h = dashboard_stats.bot_health(bot, log, exchange_pos=exchange)
    assert h["open_position"] == "LONG"
    assert h["position_source"] == "exchange"


def test_bot_health_booting_when_running_without_log(bot):
    from datetime import datetime, timezone

    bot.state.running = True
    bot._session_started_at = datetime.now(timezone.utc)
    h = dashboard_stats.bot_health(bot, pd.DataFrame())
    assert h["status"] == "BOOTING"
    assert h["stale"] is False


def test_reconcile_manual_exchange_close_backfills_trade(tmp_path):
    store = TradeStore(db_path=str(tmp_path / "dashboard.db"))
    store.log_status(
        StatusRow(
            ts="2026-07-08 10:00:00",
            session_id="sess-manual",
            price=63_700.0,
            prob_long=0.62,
            prob_short=0.22,
            prob_cash=0.16,
            direction="LONG",
            balance=5_000.0,
            open_position="LONG",
            realized_pnl=-9.48,
            unrealized_pnl=9.64,
            entry_price=63_761.89,
            tp_price=64_272.00,
            sl_price=63_443.00,
            action="HOLD",
            event="WAIT",
            reason="stale orphan",
        )
    )

    log = store.read_status_df()
    trades = store.read_trades_df()

    class MockClient:
        def futures_account_trades(self, symbol, limit=200):
            return [
                {
                    "symbol": symbol,
                    "orderId": 777,
                    "side": "SELL",
                    "price": "63988.51",
                    "qty": "0.0533",
                    "realizedPnl": "12.12",
                    "time": 1890000000000,
                }
            ]

    result = dashboard_stats.reconcile_manual_exchange_close(
        store=store,
        log=log,
        trades=trades,
        exchange_pos={"status": "flat"},
        client=MockClient(),
    )
    assert result["inserted"] is True

    trades2 = store.read_trades_df()
    assert len(trades2) == 1
    assert trades2.iloc[0]["session_id"] == "sess-manual"
    assert trades2.iloc[0]["side"] == "LONG"
    assert trades2.iloc[0]["outcome"] == "MANUAL"
    assert trades2.iloc[0]["realized_pnl"] == pytest.approx(12.12)

    # Second call should be idempotent (no duplicates).
    result2 = dashboard_stats.reconcile_manual_exchange_close(
        store=store,
        log=store.read_status_df(),
        trades=trades2,
        exchange_pos={"status": "flat"},
        client=MockClient(),
    )
    assert result2["inserted"] is False
