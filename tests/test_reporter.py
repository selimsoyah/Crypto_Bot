"""Session reporter tests (Phase 0 fix #6).

The reporter must read the ground-truth ``trades`` table — never reconstruct
trades from log rows. These tests insert known trades and assert the report
aggregates match exactly.
"""

from datetime import datetime, timedelta, timezone

import pytest

from bot_loop import TradingBot, build_session_report
from trade_store import StatusRow, TradeRecord, TradeStore


@pytest.fixture()
def bot(tmp_path):
    store = TradeStore(db_path=str(tmp_path / "test.db"))
    tb = TradingBot(store=store)
    tb.session_id = "sessX"
    tb._session_started_at = datetime.now(timezone.utc) - timedelta(hours=2)
    return tb


def _status(session_id: str, ts: str, balance: float) -> StatusRow:
    return StatusRow(
        ts=ts, session_id=session_id, price=60_000.0, prob_long=0.4,
        prob_short=0.3, prob_cash=0.3, direction="CASH", balance=balance,
        open_position="FLAT", realized_pnl=0.0, unrealized_pnl=0.0,
        entry_price=None, tp_price=None, sl_price=None,
        action="HOLD", event="WAIT", reason="scan",
    )


def _trade(session_id: str, side: str, pnl: float, outcome: str) -> TradeRecord:
    return TradeRecord(
        session_id=session_id, side=side,
        entry_ts="2026-07-05 10:00:00", exit_ts="2026-07-05 11:00:00",
        entry_price=60_000.0, exit_price=60_000.0 + pnl * 100,
        quantity=0.05, tp_price=60_720.0, sl_price=59_640.0,
        peak_unrealized=max(pnl, 5.0), realized_pnl=pnl, outcome=outcome,
    )


def test_report_aggregates_match_ground_truth(bot):
    bot.store.log_status(_status("sessX", "2026-07-05 10:00:00", 1_000.0))
    bot.store.log_status(_status("sessX", "2026-07-05 12:00:00", 1_042.0))

    bot.store.record_trade(_trade("sessX", "LONG", +36.0, "TP"))
    bot.store.record_trade(_trade("sessX", "SHORT", -12.0, "SL"))
    bot.store.record_trade(_trade("sessX", "LONG", +18.0, "TP"))
    # A trade from a DIFFERENT session must be excluded.
    bot.store.record_trade(_trade("other", "LONG", +999.0, "TP"))

    report = build_session_report(bot)
    assert report is not None
    summary = report.summary

    assert summary["total_closed"] == 3
    assert summary["wins"] == 2
    assert summary["win_rate"] == pytest.approx(100.0 * 2 / 3)
    assert summary["net_realized"] == pytest.approx(36.0 - 12.0 + 18.0)
    assert summary["long_count"] == 2
    assert summary["short_count"] == 1
    assert summary["initial_balance"] == pytest.approx(1_000.0)
    assert summary["final_balance"] == pytest.approx(1_042.0)
    assert summary["balance_delta"] == pytest.approx(42.0)

    # Ledger rows come straight from the trades table, in insert order.
    assert [t["outcome"] for t in report.trades] == ["TP", "SL", "TP"]
    assert "ground-truth `trades` table" in report.markdown


def test_report_handles_empty_session(bot):
    report = build_session_report(bot)
    assert report is not None
    assert report.summary["total_closed"] == 0
    assert report.summary["win_rate"] == 0.0
    assert "No completed trade cycles" in report.markdown


def test_report_requires_started_session(tmp_path):
    store = TradeStore(db_path=str(tmp_path / "t.db"))
    tb = TradingBot(store=store)
    # Never started: no session id / start time.
    assert build_session_report(tb) is None
