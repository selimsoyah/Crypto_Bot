"""Tests for session CSV export on shutdown."""

from datetime import datetime, timedelta, timezone

import pytest

import config
from bot_loop import TradingBot, build_session_report, generate_session_summary_report
from session_export import export_session_csv, session_csv_filename
from trade_store import StatusRow, TradeRecord, TradeStore


@pytest.fixture()
def bot(tmp_path):
    store = TradeStore(db_path=str(tmp_path / "test.db"))
    tb = TradingBot(store=store)
    tb.session_id = "sesscsv"
    tb._session_started_at = datetime(2026, 7, 5, 18, 3, 0, tzinfo=timezone.utc)
    tb.risk.begin_session(5_000.0)
    return tb


def _status(session_id: str, ts: str, balance: float, action: str = "HOLD") -> StatusRow:
    return StatusRow(
        ts=ts,
        session_id=session_id,
        price=60_000.0,
        prob_long=0.4,
        prob_short=0.3,
        prob_cash=0.3,
        direction="CASH",
        balance=balance,
        open_position="FLAT",
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        entry_price=None,
        tp_price=None,
        sl_price=None,
        action=action,
        event="WAIT",
        reason="scan",
    )


def test_session_csv_filename_encodes_start_and_duration():
    start = datetime(2026, 7, 5, 18, 3, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2, minutes=15, seconds=30)
    name = session_csv_filename(start, end)
    assert name == "session_2026-07-05_18h03m_2h15m30s.csv"


def test_export_session_csv_writes_metrics_logs_and_trades(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_EXPORT_DIR", str(tmp_path / "exports"))
    bot.store.log_status(_status("sesscsv", "2026-07-05 18:05:00", 5_000.0))
    bot.store.log_status(
        _status("sesscsv", "2026-07-05 18:10:00", 5_010.0, action="OPEN_LONG")
    )
    bot.store.record_trade(
        TradeRecord(
            session_id="sesscsv",
            side="LONG",
            entry_ts="2026-07-05 18:05:00",
            exit_ts="2026-07-05 18:10:00",
            entry_price=60_000.0,
            exit_price=60_200.0,
            quantity=0.05,
            tp_price=60_360.0,
            sl_price=59_820.0,
            peak_unrealized=12.0,
            realized_pnl=10.0,
            outcome="TP",
        )
    )

    shutdown = bot._session_started_at + timedelta(hours=1, minutes=7)
    report = build_session_report(bot, shutdown_ts=shutdown)
    assert report is not None

    path = export_session_csv(bot, report)
    assert path.endswith("session_2026-07-05_18h03m_1h7m0s.csv")
    assert (tmp_path / "exports").exists()

    text = open(path, encoding="utf-8").read()
    assert "# SESSION METRICS" in text
    assert "sesscsv" in text
    assert "# STATUS LOG" in text
    assert "OPEN_LONG" in text
    assert "# COMPLETED TRADES" in text
    assert "TP" in text
    assert "1h 7m 0s" in text


def test_generate_session_summary_report_includes_csv_path(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_EXPORT_DIR", str(tmp_path / "exports"))
    bot.store.log_status(_status("sesscsv", "2026-07-05 18:05:00", 5_000.0))

    report = generate_session_summary_report(bot)
    assert report is not None
    assert report.csv_path
    assert report.csv_path.endswith(".csv")
    assert (tmp_path / "exports").exists()
