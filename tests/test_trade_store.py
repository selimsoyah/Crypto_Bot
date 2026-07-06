"""Tests for the SQLite trade store (Phase 0 fixes #5 and #6)."""

import os
import threading

import pytest

from trade_store import StatusRow, TradeRecord, TradeStore


@pytest.fixture()
def store(tmp_path):
    return TradeStore(db_path=str(tmp_path / "test.db"))


def _status_row(**overrides) -> StatusRow:
    base = dict(
        ts="2026-07-05 12:00:00",
        session_id="sess1",
        price=60_000.0,
        prob_long=0.5,
        prob_short=0.3,
        prob_cash=0.2,
        direction="LONG",
        balance=5_000.0,
        open_position="FLAT",
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        entry_price=None,
        tp_price=None,
        sl_price=None,
        action="HOLD",
        event="WAIT",
        reason="scanning",
    )
    base.update(overrides)
    return StatusRow(**base)


def _trade(**overrides) -> TradeRecord:
    base = dict(
        session_id="sess1",
        side="LONG",
        entry_ts="2026-07-05 12:00:00",
        exit_ts="2026-07-05 13:00:00",
        entry_price=60_000.0,
        exit_price=60_720.0,
        quantity=0.05,
        tp_price=60_720.0,
        sl_price=59_640.0,
        peak_unrealized=40.0,
        realized_pnl=36.0,
        outcome="TP",
    )
    base.update(overrides)
    return TradeRecord(**base)


def test_status_round_trip(store):
    store.log_status(_status_row(action="OPEN_LONG", event="BUY_LONG"))
    store.log_status(_status_row(ts="2026-07-05 12:00:07", action="HOLD"))

    df = store.read_status_df()
    assert len(df) == 2
    assert list(df["Action"]) == ["OPEN_LONG", "HOLD"]
    assert df.iloc[0]["Event"] == "BUY_LONG"
    assert df.iloc[0]["Current_Balance"] == 5_000.0


def test_status_limit_keeps_latest_in_chronological_order(store):
    for i in range(10):
        store.log_status(_status_row(ts=f"2026-07-05 12:00:{i:02d}", action=f"A{i}"))
    df = store.read_status_df(limit=3)
    assert list(df["Action"]) == ["A7", "A8", "A9"]


def test_trade_round_trip_and_session_filter(store):
    store.record_trade(_trade())
    store.record_trade(_trade(session_id="other", outcome="SL", realized_pnl=-12.0))

    all_trades = store.read_trades_df()
    assert len(all_trades) == 2

    sess1 = store.read_trades_df(session_id="sess1")
    assert len(sess1) == 1
    assert sess1.iloc[0]["outcome"] == "TP"
    assert sess1.iloc[0]["realized_pnl"] == pytest.approx(36.0)


def test_session_balance_bounds(store):
    store.log_status(_status_row(balance=1_000.0))
    store.log_status(_status_row(ts="2026-07-05 12:10:00", balance=1_055.5))
    first, last = store.session_balance_bounds("sess1")
    assert first == pytest.approx(1_000.0)
    assert last == pytest.approx(1_055.5)


def test_csv_export(store, tmp_path):
    store.log_status(_status_row())
    out = store.export_status_csv(path=str(tmp_path / "export.csv"))
    assert os.path.exists(out)
    with open(out) as fh:
        header = fh.readline()
    assert "Timestamp" in header and "Reason" in header


def test_concurrent_writer_and_reader(store):
    """A writer thread and reader thread must not corrupt or crash (WAL)."""
    errors: list[Exception] = []

    def writer():
        try:
            for i in range(50):
                store.log_status(_status_row(ts=f"2026-07-05 13:00:{i % 60:02d}"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            for _ in range(50):
                store.read_status_df()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(store.read_status_df()) == 50
