"""Tests for session CSV export on shutdown."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import config
from bot_loop import (
    TradingBot,
    build_session_report,
    generate_session_summary_report,
    recover_orphan_session_export,
)
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


def test_finalize_session_shutdown_is_idempotent(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_EXPORT_DIR", str(tmp_path / "exports"))
    bot.store.log_status(_status("sesscsv", "2026-07-05 18:05:00", 5_000.0))

    first = bot._finalize_session_shutdown()
    second = bot._finalize_session_shutdown()
    assert first is not None
    assert second is not None
    assert first.csv_path == second.csv_path
    assert len(list((tmp_path / "exports").glob("*.csv"))) == 1


def test_recovers_orphan_session_export_after_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_EXPORT_DIR", str(tmp_path / "exports"))
    marker_path = tmp_path / ".bot_active_session.json"
    monkeypatch.setattr("bot_loop.ACTIVE_SESSION_MARKER_PATH", str(marker_path))

    store = TradeStore(db_path=str(tmp_path / "orphan.db"))
    started = datetime(2026, 7, 6, 18, 0, 0, tzinfo=timezone.utc)
    store.log_status(_status("orphan01", "2026-07-06 18:05:00", 5_000.0))
    store.log_status(
        _status("orphan01", "2026-07-06 22:14:11", 4_980.0, action="HOLD")
    )
    store.record_trade(
        TradeRecord(
            session_id="orphan01",
            side="LONG",
            entry_ts="2026-07-06 18:05:00",
            exit_ts="2026-07-06 19:00:00",
            entry_price=60_000.0,
            exit_price=59_900.0,
            quantity=0.05,
            tp_price=60_240.0,
            sl_price=59_850.0,
            peak_unrealized=5.0,
            realized_pnl=-5.0,
            outcome="SL",
        )
    )
    marker_path.write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "orphan01",
                "session_started_at": started.isoformat(),
                "pid": 99999,
            }
        ),
        encoding="utf-8",
    )

    report = recover_orphan_session_export(store)
    assert report is not None
    assert report.csv_path
    assert report.csv_path.endswith(".csv")
    assert not marker_path.exists()

    text = open(report.csv_path, encoding="utf-8").read()
    assert "orphan01" in text
    assert "SL" in text


def test_trading_bot_init_recovers_orphan_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_EXPORT_DIR", str(tmp_path / "exports"))
    marker_path = tmp_path / ".bot_active_session.json"
    monkeypatch.setattr("bot_loop.ACTIVE_SESSION_MARKER_PATH", str(marker_path))

    store = TradeStore(db_path=str(tmp_path / "init.db"))
    started = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
    store.log_status(_status("initrec", "2026-07-06 12:10:00", 5_000.0))
    marker_path.write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "initrec",
                "session_started_at": started.isoformat(),
                "pid": 4242,
            }
        ),
        encoding="utf-8",
    )

    TradingBot(store=store)
    assert not marker_path.exists()
    assert list((tmp_path / "exports").glob("*.csv"))


def test_recovers_from_stale_instance_lock_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_EXPORT_DIR", str(tmp_path / "exports"))
    lock_path = tmp_path / ".bot_instance.lock"
    monkeypatch.setattr("bot_loop.INSTANCE_LOCK_PATH", str(lock_path))
    monkeypatch.setattr("bot_loop.ACTIVE_SESSION_MARKER_PATH", str(tmp_path / "missing_marker.json"))

    store = TradeStore(db_path=str(tmp_path / "stale.db"))
    store.log_status(_status("stale01", "2026-07-07 06:10:00", 5_000.0))
    store.log_status(_status("stale01", "2026-07-07 06:12:48", 4_990.0))
    lock_path.write_text("999999\n", encoding="utf-8")

    report = recover_orphan_session_export(store)
    assert report is not None
    assert report.csv_path
    assert "stale01" in open(report.csv_path, encoding="utf-8").read()
