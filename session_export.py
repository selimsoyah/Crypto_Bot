"""
session_export.py
=================
Write a per-session CSV dossier on bot shutdown.

The file name encodes the session start (date + time) and total runtime, e.g.::

    session_2026-07-05_18h03m_2h15m30s.csv

Each export contains:
    1. Session metrics (key/value block)
    2. Full status log for the session (every scan / transition row)
    3. Completed trades ledger for the session
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

import config

if TYPE_CHECKING:
    from bot_loop import SessionReport, TradingBot

logger = config.configure_logging(__name__)


def _duration_filename_token(start: datetime, end: datetime) -> str:
    """Filesystem-safe runtime token, e.g. ``2h15m30s`` or ``45s``."""
    total_seconds = max(0, int((end - start).total_seconds()))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes}m{seconds}s"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


def session_csv_filename(session_start: datetime, shutdown_ts: datetime) -> str:
    """Return the CSV basename for a session export."""
    if session_start.tzinfo is None:
        session_start = session_start.replace(tzinfo=timezone.utc)
    start_token = session_start.strftime("%Y-%m-%d_%Hh%Mm")
    duration_token = _duration_filename_token(session_start, shutdown_ts)
    return f"session_{start_token}_{duration_token}.csv"


def session_csv_path(session_start: datetime, shutdown_ts: datetime) -> str:
    """Absolute path for the session CSV export."""
    export_dir = config.SESSION_EXPORT_DIR
    os.makedirs(export_dir, exist_ok=True)
    return os.path.join(export_dir, session_csv_filename(session_start, shutdown_ts))


def _metric_rows(summary: dict[str, Any], bot: "TradingBot") -> list[tuple[str, str]]:
    """Flatten session summary + config context into CSV metric rows."""
    snap = bot.risk.snapshot()
    rows: list[tuple[str, str]] = [
        ("Session ID", str(bot.session_id or "")),
        (
            "Session Start (UTC)",
            summary["session_start"].strftime("%Y-%m-%d %H:%M:%S"),
        ),
        (
            "Session End (UTC)",
            summary["shutdown_ts"].strftime("%Y-%m-%d %H:%M:%S"),
        ),
        ("Duration", str(summary["duration"])),
        ("Trading Profile", config.TRADING_PROFILE),
        ("Symbol", config.SYMBOL),
        ("Interval", config.INTERVAL),
        ("Leverage", f"{config.LEVERAGE}x"),
        ("Execution Venue", config.EXECUTION_VENUE),
        ("Initial Balance (USDT)", f"{summary['initial_balance']:.2f}"),
        ("Final Balance (USDT)", f"{summary['final_balance']:.2f}"),
        ("Balance Change (USDT)", f"{summary['balance_delta']:+.2f}"),
        ("Net Realized PnL (USDT)", f"{summary['net_realized']:+.2f}"),
        ("Win Rate (%)", f"{summary['win_rate']:.2f}"),
        ("Wins (TP)", str(summary["wins"])),
        ("Total Closed Trades", str(summary["total_closed"])),
        ("Long Trades", str(summary["long_count"])),
        ("Short Trades", str(summary["short_count"])),
        ("Status Log Rows", str(summary.get("status_log_rows", 0))),
        ("Session Start Equity (Risk)", f"{snap.session_start_equity:.2f}"),
        ("Consecutive Wins (at shutdown)", str(snap.consecutive_wins)),
        ("Consecutive Losses (at shutdown)", str(snap.consecutive_losses)),
    ]
    long_thr = getattr(bot, "_long_threshold", None)
    short_thr = getattr(bot, "_short_threshold", None)
    if long_thr is not None and short_thr is not None:
        rows.append(("Long Threshold", f"{long_thr:.3f}"))
        rows.append(("Short Threshold", f"{short_thr:.3f}"))
    return rows


def export_session_csv(
    bot: "TradingBot",
    report: "SessionReport",
    path: Optional[str] = None,
) -> str:
    """Write the session dossier CSV and return the absolute file path."""
    summary = report.summary
    session_start: datetime = summary["session_start"]
    shutdown_ts: datetime = summary["shutdown_ts"]
    out_path = path or session_csv_path(session_start, shutdown_ts)

    status_df = bot.store.read_status_df(session_id=bot.session_id)
    trades_df = bot.store.read_trades_df(session_id=bot.session_id)
    summary["status_log_rows"] = len(status_df)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp_path = f"{out_path}.tmp"

    with open(tmp_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)

        writer.writerow(["# SESSION METRICS"])
        writer.writerow(["Metric", "Value"])
        for label, value in _metric_rows(summary, bot):
            writer.writerow([label, value])

        writer.writerow([])
        writer.writerow(["# STATUS LOG"])
        if status_df.empty:
            writer.writerow(["(no status rows for this session)"])
        else:
            export_status = status_df.copy()
            if "Timestamp" in export_status.columns:
                export_status["Timestamp"] = export_status["Timestamp"].astype(str)
            writer.writerow(list(export_status.columns))
            for row in export_status.itertuples(index=False, name=None):
                writer.writerow(list(row))

        writer.writerow([])
        writer.writerow(["# COMPLETED TRADES"])
        if trades_df.empty:
            writer.writerow(["(no completed trades for this session)"])
        else:
            writer.writerow(list(trades_df.columns))
            for row in trades_df.itertuples(index=False, name=None):
                writer.writerow(list(row))

    os.replace(tmp_path, out_path)
    logger.info(
        "Session CSV export written to %s (%d log rows, %d trades)",
        out_path,
        len(status_df),
        len(trades_df),
    )
    return out_path
