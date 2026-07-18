"""
bot_loop.py
===========
Live execution thread for the BTC/USDT ML bot on the Binance **USDⓈ-M Futures
Testnet**.

A background :class:`threading.Thread` wakes every ``config.LOOP_SLEEP_SECONDS``
seconds, pulls the latest candles, recomputes features, scores them with the
trained multi-class XGBoost model and manages a single **directional** position
(LONG or SHORT) with the TP/SL bracket defined in ``config.py`` (the same
constants used to label the training data — single source of truth).

Persistence (see ``trade_store.py``):
    * every discrete state transition (scan, open, close, warning) is written
      as its own row to the SQLite ``status_log`` table — a close and an open
      in the same iteration produce TWO rows, never one;
    * every completed trade is written atomically to the ``trades`` table at
      close time. The session reporter reads that table directly and never
      reconstructs trades from log rows.

Decision thresholds are resolved through ``model_brain.load_thresholds()``:
the data-driven tuned values in ``decision_threshold.json`` take priority,
falling back to the config constants only if the sidecar is missing. The
dashboard displays the same resolved values, so what you see is what trades.

Concurrency safety: a file-based instance lock guarantees at most ONE live
trading engine per machine (protects against a CLI bot and a dashboard bot —
or two dashboard tabs — trading the same account simultaneously).

Run directly to start the bot in the foreground:

    python bot_loop.py

On shutdown (Ctrl+C, SIGTERM, or dashboard FORCE SHUTDOWN) a
``session_summary_report.md`` performance dossier and a timestamped session CSV
(``session_exports/session_{start}_{duration}.csv``) are written automatically.

Session exports also run when the engine thread exits unexpectedly (``finally``
in the trading loop), on process exit (``atexit``), and on the next dashboard
boot if a prior session left an active-session marker on disk.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import signal
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
import config
import box_strategy
import compound_strategy
import data_pipeline
import exchange_client
import model_brain
import order_execution
from alerting import send_alert
from risk_engine import RiskDecision, RiskEngine, PositionSizeResult
from trade_store import StatusRow, TradeRecord, TradeStore

logger = config.configure_logging(__name__)

SESSION_REPORT_PATH = "session_summary_report.md"
INSTANCE_LOCK_PATH = str(config.BASE_DIR / ".bot_instance.lock")
ACTIVE_SESSION_MARKER_PATH = str(config.BASE_DIR / ".bot_active_session.json")

_REPORT_LOCK = threading.Lock()
_REPORT_ALREADY_WRITTEN = False
_ATEXIT_BOT_REF: Optional[weakref.ReferenceType["TradingBot"]] = None
_ATEXIT_REGISTERED = False


# --------------------------------------------------------------------------- #
# Shared strategy math (single source of truth with training labels)          #
# --------------------------------------------------------------------------- #
def bracket_prices(
    direction: str,
    entry_price: float,
    atr_pct: Optional[float] = None,
) -> tuple[float, float]:
    """Return ``(take_profit_price, stop_loss_price)`` for a fill."""
    levels = compound_strategy.effective_brackets(direction, entry_price, atr_pct)
    return levels.take_profit, levels.stop_loss


def resolve_live_thresholds() -> tuple[float, float, str]:
    """Return ``(long_thr, short_thr, source)`` used by BOTH bot and dashboard.

    The tuned sidecar (``decision_threshold.json``) wins when present; the
    config constants are the explicit fallback. ``source`` describes which one
    was used so the operator is never shown a number the bot is not trading.
    """
    if not config.is_xgboost_ml_profile():
        return 0.0, 0.0, f"profile={config.ACTIVE_PROFILE} (ML thresholds disabled)"
    long_thr, short_thr = model_brain.load_thresholds()
    if os.path.exists(config.THRESHOLD_PATH):
        source = f"tuned sidecar ({os.path.basename(config.THRESHOLD_PATH)})"
    else:
        source = "config fallback"
    return long_thr, short_thr, source


# --------------------------------------------------------------------------- #
# Single-instance lock                                                        #
# --------------------------------------------------------------------------- #
class InstanceLock:
    """File lock ensuring at most one live trading engine per machine.

    Uses ``flock`` so the lock is released automatically by the OS if the
    process dies, and fails fast (non-blocking) if another engine holds it.
    """

    def __init__(self, path: str = INSTANCE_LOCK_PATH) -> None:
        self._path = path
        self._fh = None

    def acquire(self) -> bool:
        if self._fh is not None:
            return True
        fh = open(self._path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


# --------------------------------------------------------------------------- #
# Session shutdown reporter (ground truth: reads the trades table)            #
# --------------------------------------------------------------------------- #
def _format_duration(start: datetime, end: datetime) -> str:
    """Render a human-readable duration between two datetimes."""
    total_seconds = max(0, int((end - start).total_seconds()))
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


@dataclass
class SessionReport:
    """In-memory session dossier used for markdown, PDF, and CSV export."""

    markdown: str
    summary: dict[str, Any]
    trades: list[dict[str, Any]]
    csv_path: Optional[str] = None


def _summarize_session(
    trades: list[dict[str, Any]],
    initial_balance: float,
    final_balance: float,
    session_start: datetime,
    shutdown_ts: datetime,
) -> dict[str, Any]:
    """Aggregate headline metrics from the ground-truth trade list."""
    net_realized = sum(float(t["realized_pnl"]) for t in trades)
    wins = sum(1 for t in trades if t["outcome"] == "TP")
    total_closed = len(trades)
    win_rate = (100.0 * wins / total_closed) if total_closed else 0.0
    long_count = sum(1 for t in trades if t["type"] == "LONG")
    short_count = sum(1 for t in trades if t["type"] == "SHORT")

    return {
        "session_start": session_start,
        "shutdown_ts": shutdown_ts,
        "duration": _format_duration(session_start, shutdown_ts),
        "initial_balance": initial_balance,
        "final_balance": final_balance,
        "balance_delta": final_balance - initial_balance,
        "net_realized": net_realized,
        "win_rate": win_rate,
        "wins": wins,
        "total_closed": total_closed,
        "long_count": long_count,
        "short_count": short_count,
    }


def _render_session_report_markdown(summary: dict[str, Any], trades: list[dict[str, Any]]) -> str:
    """Build the formatted markdown document for the session summary."""
    delta = summary["balance_delta"]
    delta_sign = "+" if delta >= 0 else ""
    net = summary["net_realized"]
    net_sign = "+" if net >= 0 else ""
    net_emoji = "📈" if net >= 0 else "📉"
    bal_emoji = "🟢" if delta >= 0 else "🔴"

    lines = [
        "# 🤖 BTC/USDT ML Futures Bot — Session Summary Report",
        "",
        f"**Generated:** {summary['shutdown_ts'].strftime('%Y-%m-%d %H:%M:%S')} UTC  ",
        f"**Symbol:** `{config.SYMBOL}` · **Interval:** `{config.INTERVAL}` · "
        f"**Leverage:** `{config.LEVERAGE}x`",
        "",
        "---",
        "",
        "## 📊 Session Overview",
        "",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| **Lifespan Duration** | {summary['duration']} "
        f"({summary['session_start'].strftime('%Y-%m-%d %H:%M:%S')} → "
        f"{summary['shutdown_ts'].strftime('%Y-%m-%d %H:%M:%S')} UTC) |",
        f"| **Initial Baseline Balance** | ${summary['initial_balance']:,.2f} USDT |",
        f"| **Final Balance** | ${summary['final_balance']:,.2f} USDT |",
        f"| **Absolute Growth / Drawdown** | {bal_emoji} {delta_sign}${delta:,.2f} USDT |",
        f"| **Net Total Realized PnL** | {net_emoji} {net_sign}${net:,.2f} USDT |",
        f"| **Win Rate Efficiency** | {summary['win_rate']:.2f}% "
        f"({summary['wins']} TP / {summary['total_closed']} closed) |",
        f"| **Total Completed Trades** | {summary['total_closed']} "
        f"({summary['long_count']} LONG · {summary['short_count']} SHORT) |",
        "",
        "---",
        "",
        "## 📒 Individual Transaction Ledger",
        "",
        "_Source: ground-truth `trades` table (one row written atomically per_",
        "_completed trade) — no log reconstruction._",
        "",
    ]

    if trades:
        lines.extend(
            [
                "| Position # | Type | Entry Price | Exit Price | "
                "Max Floating Profit (Peak PnL) | Final Realized PnL ($) | "
                "Outcome (TP / SL / Manual) |",
                "| :---: | :---: | ---: | ---: | ---: | ---: | :---: |",
            ]
        )
        for idx, trade in enumerate(trades, start=1):
            realized = float(trade["realized_pnl"])
            realized_fmt = f"+${realized:,.2f}" if realized >= 0 else f"-${abs(realized):,.2f}"
            peak = float(trade["peak_pnl"])
            peak_fmt = f"+${peak:,.2f}" if peak >= 0 else f"-${abs(peak):,.2f}"
            lines.append(
                f"| {idx} | {trade['type']} | "
                f"${float(trade['entry_price']):,.2f} | "
                f"${float(trade['exit_price']):,.2f} | "
                f"{peak_fmt} | {realized_fmt} | {trade['outcome']} |"
            )
    else:
        lines.append("_No completed trade cycles were recorded during this session._")

    lines.extend(["", "---", "", "*Report auto-generated by the Session Shutdown Reporter.*", ""])
    return "\n".join(lines)


def _pdf_safe(text: str) -> str:
    """Strip characters that Helvetica cannot render in fpdf2."""
    return text.encode("ascii", "replace").decode("ascii")


def render_session_report_pdf(report: SessionReport) -> bytes:
    """Render the session dossier as a downloadable PDF byte stream."""
    from fpdf import FPDF

    summary = report.summary
    trades = report.trades
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, _pdf_safe("BTC/USDT ML Futures Bot - Session Summary Report"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(
        0,
        6,
        _pdf_safe(
            f"Generated: {summary['shutdown_ts'].strftime('%Y-%m-%d %H:%M:%S')} UTC | "
            f"Symbol: {config.SYMBOL} | Interval: {config.INTERVAL} | Leverage: {config.LEVERAGE}x"
        ),
        ln=True,
    )
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Session Overview", ln=True)
    pdf.set_font("Helvetica", "", 10)

    delta = summary["balance_delta"]
    delta_sign = "+" if delta >= 0 else ""
    net = summary["net_realized"]
    net_sign = "+" if net >= 0 else ""

    overview_rows = [
        ("Lifespan Duration", f"{summary['duration']}"),
        (
            "Session Window",
            f"{summary['session_start'].strftime('%Y-%m-%d %H:%M:%S')} -> "
            f"{summary['shutdown_ts'].strftime('%Y-%m-%d %H:%M:%S')} UTC",
        ),
        ("Initial Baseline Balance", f"${summary['initial_balance']:,.2f} USDT"),
        ("Final Balance", f"${summary['final_balance']:,.2f} USDT"),
        ("Absolute Growth / Drawdown", f"{delta_sign}${delta:,.2f} USDT"),
        ("Net Total Realized PnL", f"{net_sign}${net:,.2f} USDT"),
        (
            "Win Rate Efficiency",
            f"{summary['win_rate']:.2f}% ({summary['wins']} TP / {summary['total_closed']} closed)",
        ),
        (
            "Total Completed Trades",
            f"{summary['total_closed']} ({summary['long_count']} LONG / {summary['short_count']} SHORT)",
        ),
    ]
    col_w = (pdf.w - 24) / 2
    for label, value in overview_rows:
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(col_w, 6, _pdf_safe(label), border=0)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(col_w, 6, _pdf_safe(value), ln=True)

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Individual Transaction Ledger", ln=True)

    if not trades:
        pdf.set_font("Helvetica", "I", 10)
        pdf.cell(0, 6, "No completed trade cycles were recorded during this session.", ln=True)
    else:
        headers = ["#", "Type", "Entry", "Exit", "Peak PnL", "Realized PnL", "Outcome"]
        widths = [12, 22, 38, 38, 38, 38, 28]
        pdf.set_font("Helvetica", "B", 8)
        for header, width in zip(headers, widths):
            pdf.cell(width, 7, header, border=1, align="C")
        pdf.ln()

        pdf.set_font("Helvetica", "", 8)
        for idx, trade in enumerate(trades, start=1):
            realized = float(trade["realized_pnl"])
            peak = float(trade["peak_pnl"])
            realized_fmt = f"+${realized:,.2f}" if realized >= 0 else f"-${abs(realized):,.2f}"
            peak_fmt = f"+${peak:,.2f}" if peak >= 0 else f"-${abs(peak):,.2f}"
            row = [
                str(idx),
                str(trade["type"]),
                f"${float(trade['entry_price']):,.2f}",
                f"${float(trade['exit_price']):,.2f}",
                peak_fmt,
                realized_fmt,
                str(trade["outcome"]),
            ]
            for value, width in zip(row, widths):
                pdf.cell(width, 6, _pdf_safe(value), border=1, align="C")
            pdf.ln()

    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, 5, "Report auto-generated by the Session Shutdown Reporter.", ln=True)

    out = pdf.output()
    return out if isinstance(out, (bytes, bytearray)) else out.encode("latin-1")


def build_session_report(
    bot: "TradingBot",
    shutdown_ts: Optional[datetime] = None,
) -> Optional[SessionReport]:
    """Assemble the session dossier from the ground-truth trades table."""
    if bot.session_id is None or bot._session_started_at is None:
        return None

    end_ts = shutdown_ts or datetime.now(timezone.utc)
    trades_df = bot.store.read_trades_df(session_id=bot.session_id)
    trades = [
        {
            "type": row["side"],
            "entry_price": float(row["entry_price"]),
            "exit_price": float(row["exit_price"]),
            "peak_pnl": float(row["peak_unrealized"]),
            "realized_pnl": float(row["realized_pnl"]),
            "outcome": str(row["outcome"]),
        }
        for _, row in trades_df.iterrows()
    ]
    initial_balance, final_balance = bot.store.session_balance_bounds(bot.session_id)
    if initial_balance == 0.0 and final_balance == 0.0 and bot.state.usdt_balance > 0:
        initial_balance = final_balance = bot.state.usdt_balance

    summary = _summarize_session(
        trades, initial_balance, final_balance, bot._session_started_at, end_ts
    )
    markdown = _render_session_report_markdown(summary, trades)
    return SessionReport(markdown=markdown, summary=summary, trades=trades)


def generate_session_summary_report(
    bot: "TradingBot",
    shutdown_ts: Optional[datetime] = None,
) -> Optional[SessionReport]:
    """Write ``session_summary_report.md`` and session CSV for the current bot session."""
    global _REPORT_ALREADY_WRITTEN

    with _REPORT_LOCK:
        if _REPORT_ALREADY_WRITTEN and bot._last_session_report is not None:
            cached = bot._last_session_report
            return _ensure_csv_on_report(bot, cached)

        report = build_session_report(bot, shutdown_ts=shutdown_ts)
        if report is None:
            return None

        finalized = _write_session_artifacts(bot, report)
        if finalized is not None:
            bot._last_session_report = finalized
            _REPORT_ALREADY_WRITTEN = True
        return finalized


def _ensure_csv_on_report(bot: "TradingBot", report: SessionReport) -> SessionReport:
    """Back-fill CSV export for cached reports (e.g. after a hot reload)."""
    csv_path = getattr(report, "csv_path", None)
    if csv_path:
        return report
    try:
        from session_export import export_session_csv

        csv_path = export_session_csv(bot, report)
        logger.info("Session CSV export written to %s", csv_path)
        upgraded = SessionReport(
            markdown=report.markdown,
            summary=report.summary,
            trades=report.trades,
            csv_path=csv_path,
        )
        bot._last_session_report = upgraded
        return upgraded
    except Exception as exc:
        logger.error("Session CSV export failed: %s", config.sanitize_for_log(str(exc)))
        return SessionReport(
            markdown=report.markdown,
            summary=report.summary,
            trades=report.trades,
            csv_path=None,
        )


def _write_session_artifacts(bot: "TradingBot", report: SessionReport) -> Optional[SessionReport]:
    """Persist markdown + CSV; return a complete SessionReport."""
    report_path = os.path.join(config.BASE_DIR, SESSION_REPORT_PATH)
    csv_path: Optional[str] = None
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            fh.write(report.markdown)
        logger.info("Session summary report written to %s", report_path)
        print(
            f"💾 [SYSTEM] Bot session terminated. Performance report generated "
            f"successfully at {SESSION_REPORT_PATH}"
        )
    except OSError as exc:
        logger.error("Failed to write session summary report: %s", exc)
        return None

    try:
        from session_export import export_session_csv

        csv_path = export_session_csv(bot, report)
        logger.info("Session CSV export written to %s", csv_path)
        print(f"💾 [SYSTEM] Session CSV export written to {csv_path}")
    except Exception as exc:
        logger.error("Session CSV export failed: %s", config.sanitize_for_log(str(exc)))

    return SessionReport(
        markdown=report.markdown,
        summary=report.summary,
        trades=report.trades,
        csv_path=csv_path,
    )


# --------------------------------------------------------------------------- #
# Active-session marker + orphan export recovery                              #
# --------------------------------------------------------------------------- #
def _parse_session_ts(value: str) -> Optional[datetime]:
    """Parse a session timestamp stored in SQLite or JSON."""
    if not value:
        return None
    text = str(value).replace("+00:00", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def infer_session_shutdown_ts(
    store: TradeStore,
    session_id: str,
    *,
    fallback: Optional[datetime] = None,
) -> datetime:
    """Best-effort session end time from the last status row for ``session_id``."""
    with store._connect() as conn:
        row = conn.execute(
            "SELECT ts FROM status_log WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if row and row[0]:
        parsed = _parse_session_ts(str(row[0]))
        if parsed is not None:
            return parsed
    return fallback or datetime.now(timezone.utc)


def _load_active_session_marker() -> Optional[dict[str, Any]]:
    if not os.path.exists(ACTIVE_SESSION_MARKER_PATH):
        return None
    try:
        with open(ACTIVE_SESSION_MARKER_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read active session marker: %s", exc)
        return None


def _write_active_session_marker(bot: "TradingBot") -> None:
    if bot.session_id is None or bot._session_started_at is None:
        return
    started = bot._session_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    payload = {
        "version": 1,
        "session_id": bot.session_id,
        "session_started_at": started.isoformat(),
        "pid": os.getpid(),
    }
    tmp_path = f"{ACTIVE_SESSION_MARKER_PATH}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp_path, ACTIVE_SESSION_MARKER_PATH)
    except OSError as exc:
        logger.warning("Could not write active session marker: %s", exc)


def _clear_active_session_marker() -> None:
    try:
        if os.path.exists(ACTIVE_SESSION_MARKER_PATH):
            os.remove(ACTIVE_SESSION_MARKER_PATH)
    except OSError as exc:
        logger.warning("Could not remove active session marker: %s", exc)


def _session_export_stamp_path(session_id: str) -> str:
    export_dir = config.SESSION_EXPORT_DIR
    os.makedirs(export_dir, exist_ok=True)
    return os.path.join(export_dir, f".exported_{session_id}")


def _session_already_exported(session_id: str) -> bool:
    stamp = _session_export_stamp_path(session_id)
    if os.path.exists(stamp):
        return True
    export_dir = config.SESSION_EXPORT_DIR
    if not os.path.isdir(export_dir):
        return False
    # Heuristic: any CSV mentioning the session id in metrics is treated as exported.
    for name in os.listdir(export_dir):
        if not name.endswith(".csv"):
            continue
        path = os.path.join(export_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                head = fh.read(4096)
            if session_id in head:
                return True
        except OSError:
            continue
    return False


def _mark_session_exported(session_id: str) -> None:
    try:
        with open(_session_export_stamp_path(session_id), "w", encoding="utf-8") as fh:
            fh.write(datetime.now(timezone.utc).isoformat())
            fh.write("\n")
    except OSError as exc:
        logger.warning("Could not write session export stamp: %s", exc)


def _recover_from_stale_instance_lock(store: TradeStore) -> Optional[SessionReport]:
    """Fallback for crashes before the active-session marker existed."""
    if os.path.exists(ACTIVE_SESSION_MARKER_PATH):
        return None
    if not os.path.exists(INSTANCE_LOCK_PATH):
        return None
    try:
        lock_pid = int(
            str(open(INSTANCE_LOCK_PATH, encoding="utf-8").read()).strip() or "0"
        )
    except (OSError, ValueError):
        lock_pid = 0
    if lock_pid > 0:
        try:
            os.kill(lock_pid, 0)
            return None
        except OSError:
            pass

    with store._connect() as conn:
        row = conn.execute(
            "SELECT session_id FROM status_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        started_row = None
        if row and row[0]:
            started_row = conn.execute(
                "SELECT ts FROM status_log WHERE session_id=? ORDER BY id ASC LIMIT 1",
                (row[0],),
            ).fetchone()
    if not row or not row[0]:
        return None
    session_id = str(row[0])
    if _session_already_exported(session_id):
        return None

    started_at = (
        _parse_session_ts(str(started_row[0]))
        if started_row and started_row[0]
        else None
    )
    if started_at is None:
        return None

    shutdown_ts = infer_session_shutdown_ts(store, session_id, fallback=started_at)
    global _REPORT_ALREADY_WRITTEN
    _REPORT_ALREADY_WRITTEN = False
    bot = TradingBot(store=store, _skip_session_recovery=True)
    bot.session_id = session_id
    bot._session_started_at = started_at
    logger.warning(
        "Stale engine lock detected — recovering session %s (last scan %s UTC).",
        session_id,
        shutdown_ts.strftime("%Y-%m-%d %H:%M:%S"),
    )
    report = generate_session_summary_report(bot, shutdown_ts=shutdown_ts)
    if report is not None:
        _mark_session_exported(session_id)
        _clear_active_session_marker()
    return report


def recover_orphan_session_export(store: TradeStore) -> Optional[SessionReport]:
    """Export a crashed session using the on-disk marker + SQLite ground truth."""
    marker = _load_active_session_marker()
    if marker:
        session_id = str(marker.get("session_id") or "").strip()
        started_raw = marker.get("session_started_at")
        if not session_id or not started_raw:
            logger.warning("Active session marker is invalid — removing.")
            _clear_active_session_marker()
            return None

        started_at = _parse_session_ts(str(started_raw))
        if started_at is None:
            logger.warning("Active session marker has bad timestamp — removing.")
            _clear_active_session_marker()
            return None

        shutdown_ts = infer_session_shutdown_ts(store, session_id, fallback=started_at)
        global _REPORT_ALREADY_WRITTEN
        _REPORT_ALREADY_WRITTEN = False
        bot = TradingBot(store=store, _skip_session_recovery=True)
        bot.session_id = session_id
        bot._session_started_at = started_at
        logger.warning(
            "Recovering orphaned session %s (last scan %s UTC) — writing export.",
            session_id,
            shutdown_ts.strftime("%Y-%m-%d %H:%M:%S"),
        )
        report = generate_session_summary_report(bot, shutdown_ts=shutdown_ts)
        if report is not None:
            _mark_session_exported(session_id)
            _clear_active_session_marker()
            logger.info(
                "Recovered session export for %s -> %s",
                session_id,
                report.csv_path or SESSION_REPORT_PATH,
            )
        return report

    return _recover_from_stale_instance_lock(store)


def _register_process_shutdown_hook(bot: "TradingBot") -> None:
    """Ensure SIGTERM / normal process exit attempts a session export once."""
    global _ATEXIT_BOT_REF, _ATEXIT_REGISTERED

    _ATEXIT_BOT_REF = weakref.ref(bot)

    def _on_process_exit() -> None:
        live = _ATEXIT_BOT_REF() if _ATEXIT_BOT_REF is not None else None
        if live is None:
            return
        if live.session_id is None or live._session_started_at is None:
            return
        if not os.path.exists(ACTIVE_SESSION_MARKER_PATH):
            return
        logger.info("Process exit hook — finalizing active session export.")
        live._stop_event.set()
        live._finalize_session_shutdown()

    if not _ATEXIT_REGISTERED:
        atexit.register(_on_process_exit)
        _ATEXIT_REGISTERED = True


# --------------------------------------------------------------------------- #
# Runtime state                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    """Internal representation of an open futures position (LONG or SHORT)."""

    side: str  # "LONG" or "SHORT"
    entry_price: float
    quantity: float
    entry_time: str
    entry_candle_ts: str  # candle Timestamp when the trade opened (bar timeout)
    take_profit_price: float
    stop_loss_price: float
    peak_unrealized: float = 0.0
    best_price: float = 0.0
    trail_active: bool = False
    quantity_open: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp1_quantity: float = 0.0
    runner_quantity: float = 0.0
    tp1_hit: bool = False
    split_enabled: bool = False
    partial_realized_pnl: float = 0.0
    atr_at_entry: float = 0.0


@dataclass
class BotState:
    """Mutable, thread-safe-ish snapshot of the bot's runtime state."""

    running: bool = False
    last_price: float = 0.0
    prob_long: float = 0.0
    prob_short: float = 0.0
    prob_cash: float = 0.0
    signal_direction: str = "CASH"
    usdt_balance: float = 0.0
    position: Optional[Position] = None
    realized_pnl: float = 0.0
    completed_trades: int = 0
    long_trades: int = 0
    short_trades: int = 0
    last_action: str = "INIT"
    last_event: str = "WAIT"
    last_reason: str = "Engine initialised. Waiting for first market scan."
    last_error: str = ""
    connection_degraded: bool = False
    connection_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class TradingBot:
    """Encapsulates the live trading loop and the dual-direction engine."""

    def __init__(
        self,
        store: Optional[TradeStore] = None,
        *,
        _skip_session_recovery: bool = False,
    ) -> None:
        self.state = BotState()
        self.store = store or TradeStore()
        self.session_id: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._client = None
        self._model = None
        self._step_size: float = 0.001
        self._qty_precision: int = 3
        self._tick_size: float = 0.1
        self._price_precision: int = 2
        self._min_notional: float = 50.0
        self._long_threshold, self._short_threshold, thr_source = resolve_live_thresholds()
        logger.info(
            "Live thresholds -> long: %.3f | short: %.3f (source: %s)",
            self._long_threshold,
            self._short_threshold,
            thr_source,
        )
        self._session_started_at: Optional[datetime] = None
        self._last_session_report: Optional[SessionReport] = None
        self._instance_lock = InstanceLock()
        self._transition_logged = False
        self.risk = RiskEngine()
        self._api_failures: int = 0
        self._api_state_lock = threading.Lock()
        self._last_known_balance: float = 0.0
        self._last_known_total_wallet: float = 0.0
        self._last_close_at: Optional[datetime] = None
        self._bracket_closed_this_iteration: bool = False
        self._post_sl_cooldown_until: Optional[datetime] = None
        self._session_armed: bool = False
        self._box_engine = box_strategy.BoxStrategyEngine(
            lookback_candles=config.BOX_LOOKBACK_CANDLES,
            confirmation_candles=config.BOX_CONFIRMATION_CANDLES,
            risk_to_reward_ratio=config.BOX_RISK_REWARD_RATIO,
            volume_filter_multiplier=config.BOX_VOLUME_FILTER_MULTIPLIER,
        )
        self._active_box: Optional[box_strategy.BoxState] = None
        self._darvas_bounds_cache_day: Optional[str] = None
        self._darvas_daily_high: Optional[float] = None
        self._darvas_daily_low: Optional[float] = None
        self._darvas_consumed_signal_ts: Optional[str] = None
        self._radio_tower = None
        # Alignment panic: True while emergency flatten is in-flight (re-entrancy guard).
        self._alignment_panic_active: bool = False
        # Rolling probability samples for long/short starvation diagnostics.
        self._prob_long_samples: list[float] = []
        self._prob_short_samples: list[float] = []
        self._prob_drift_scans: int = 0
        if config.RADIO_TOWER_ENABLED:
            from radio_tower import RadioTowerListener

            self._radio_tower = RadioTowerListener(self.store)
        if not _skip_session_recovery:
            recover_orphan_session_export(self.store)

    # ------------------------------------------------------------------ #
    # Setup helpers                                                      #
    # ------------------------------------------------------------------ #
    def _resolve_darvas_daily_bounds(self) -> tuple[Optional[float], Optional[float]]:
        today = datetime.now(timezone.utc).date().isoformat()
        if (
            self._darvas_bounds_cache_day == today
            and self._darvas_daily_high is not None
            and self._darvas_daily_low is not None
        ):
            return self._darvas_daily_high, self._darvas_daily_low
        try:
            top, bottom, prev_day = data_pipeline.fetch_previous_utc_day_high_low()
            self._darvas_daily_high = top
            self._darvas_daily_low = bottom
            self._darvas_bounds_cache_day = today
            box_strategy.BoxStrategyEngine.log_box_bounds(
                top,
                bottom,
                prev_day,
                source="exchange_1d_startup",
                prev_day_bars=1,
            )
            return top, bottom
        except Exception as exc:
            logger.warning("Falling back to 15m aggregate bounds — daily fetch failed: %s", exc)
            return None, None

    def _mark_api_success(self) -> None:
        with self._api_state_lock:
            self._api_failures = 0
            self.state.connection_degraded = False
            self.state.connection_error = ""

    def _mark_api_failure(self, exc: Exception, context: str) -> None:
        self._api_state_lock.acquire()
        try:
            self._api_failures += 1
            failures = self._api_failures
            safe = config.sanitize_for_log(str(exc))
            self.state.connection_degraded = True
            self.state.connection_error = f"{context}: {safe}"
        finally:
            self._api_state_lock.release()
        if failures == config.EXCHANGE_DEGRADED_THRESHOLD:
            send_alert(
                "WARNING",
                "API connection degraded",
                self.state.connection_error,
                key="api_degraded",
            )
        if failures >= config.EXCHANGE_RECONNECT_THRESHOLD:
            self._reconnect_client()

    def _mark_stale_data(self, reason: str) -> None:
        """Mark heartbeat STALE/DEGRADED without counting as an exchange API failure."""
        safe = config.sanitize_for_log(reason)
        with self._api_state_lock:
            self.state.connection_degraded = True
            self.state.connection_error = safe
            self.state.last_action = "HOLD"
            self.state.last_event = "WARNING"
            self.state.last_reason = safe

    def _reconnect_client(self) -> None:
        """Attempt to rebuild the execution client after repeated failures."""
        try:
            logger.warning("Attempting exchange client reconnect …")
            self._connect()
            self._mark_api_success()
            send_alert(
                "INFO",
                "Exchange reconnected",
                config.execution_banner_text(),
                key="api_reconnected",
            )
        except Exception as exc:
            msg = config.sanitize_for_log(str(exc))
            logger.error("Reconnect failed: %s", msg)
            with self._api_state_lock:
                self.state.connection_error = f"Reconnect failed: {msg}"

    # ------------------------------------------------------------------ #
    # Exchange connection                                                #
    # ------------------------------------------------------------------ #
    def _connect(self) -> None:
        errors = config.validate_execution_config()
        if errors:
            raise RuntimeError("; ".join(errors))

        self._client = exchange_client.build_execution_client()
        banner = config.execution_banner_text()
        if config.execution_is_live():
            logger.critical("⚠️  %s", banner)
            send_alert("CRITICAL", "LIVE execution venue", banner, key="live_boot")
        else:
            logger.info("✅ %s", banner)
        self._configure_symbol()

    def _configure_symbol(self) -> None:
        """Set leverage / margin type and cache the symbol lot-size filters."""
        try:
            exchange_client.call_with_retry(
                self._client.futures_change_leverage,
                symbol=config.SYMBOL,
                leverage=config.LEVERAGE,
                label="futures_change_leverage",
            )
            logger.info("Set leverage to %dx on %s.", config.LEVERAGE, config.SYMBOL)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Could not set leverage: %s", exc)

        # Margin type (ISOLATED / CROSSED). Binance errors if already set; ignore.
        try:
            exchange_client.call_with_retry(
                self._client.futures_change_margin_type,
                symbol=config.SYMBOL,
                marginType=config.MARGIN_TYPE,
                label="futures_change_margin_type",
            )
            logger.info("Set margin type to %s on %s.", config.MARGIN_TYPE, config.SYMBOL)
        except Exception as exc:  # pragma: no cover - often "no need to change"
            logger.info("Margin type unchanged (%s).", exc)

        # Cache LOT_SIZE / precision from exchange info.
        try:
            info = exchange_client.call_with_retry(
                self._client.futures_exchange_info, label="futures_exchange_info"
            )
            for sym in info.get("symbols", []):
                if sym.get("symbol") == config.SYMBOL:
                    self._qty_precision = int(sym.get("quantityPrecision", 3))
                    self._price_precision = int(sym.get("pricePrecision", 2))
                    for flt in sym.get("filters", []):
                        if flt.get("filterType") == "LOT_SIZE":
                            self._step_size = float(flt["stepSize"])
                        elif flt.get("filterType") == "PRICE_FILTER":
                            self._tick_size = float(flt.get("tickSize", self._tick_size))
                        elif flt.get("filterType") in {"MIN_NOTIONAL", "NOTIONAL"}:
                            # Futures testnet commonly enforces min notional >= 50 USDT.
                            self._min_notional = float(
                                flt.get("notional", flt.get("minNotional", 50.0))
                            )
                    break
            logger.info(
                "Symbol filters -> step_size: %s | tick_size: %s | qty_precision: %d | "
                "price_precision: %d | min_notional: %.2f",
                self._step_size,
                self._tick_size,
                self._qty_precision,
                self._price_precision,
                self._min_notional,
            )
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Could not fetch futures exchange info: %s", exc)

    def _load_model(self) -> None:
        if not config.is_xgboost_ml_profile():
            self._model = None
            logger.info(
                "Active profile %s does not require ML model loading.",
                config.ACTIVE_PROFILE,
            )
            return
        self._model = model_brain.load_model()
        self._long_threshold, self._short_threshold, thr_source = resolve_live_thresholds()
        logger.info(
            "Loaded model from %s (long thr: %.3f | short thr: %.3f | source: %s)",
            config.MODEL_PATH,
            self._long_threshold,
            self._short_threshold,
            thr_source,
        )

    # ------------------------------------------------------------------ #
    # Account / market helpers                                           #
    # ------------------------------------------------------------------ #
    def _get_usdt_balance(self) -> float:
        """Return the available (free) USDT margin in the futures wallet."""
        try:
            balances = exchange_client.call_with_retry(
                self._client.futures_account_balance, label="futures_account_balance"
            )
            for bal in balances:
                if bal.get("asset") == config.MARGIN_ASSET:
                    val = float(bal.get("availableBalance", bal.get("balance", 0.0)))
                    self._last_known_balance = val
                    self._mark_api_success()
                    return val
        except Exception as exc:  # pragma: no cover - network dependent
            self._mark_api_failure(exc, "Futures balance query failed")
            logger.warning(
                "Futures balance query failed: %s",
                config.sanitize_for_log(str(exc)),
            )
        if self._last_known_balance > 0:
            return self._last_known_balance
        return 0.0

    def _get_total_wallet_balance(self) -> float:
        """Return total wallet balance (not just free margin) for sizing."""
        try:
            balances = exchange_client.call_with_retry(
                self._client.futures_account_balance, label="futures_account_balance"
            )
            for bal in balances:
                if bal.get("asset") == config.MARGIN_ASSET:
                    val = float(bal.get("balance", bal.get("availableBalance", 0.0)))
                    self._last_known_total_wallet = val
                    self._mark_api_success()
                    return val
        except Exception as exc:  # pragma: no cover - network dependent
            self._mark_api_failure(exc, "Futures total balance query failed")
            logger.warning(
                "Futures total balance query failed: %s",
                config.sanitize_for_log(str(exc)),
            )
        if self._last_known_total_wallet > 0:
            return self._last_known_total_wallet
        return 0.0

    def _compute_order_margin(self, total_wallet: float, atr_pct: float) -> PositionSizeResult:
        """Delegate sizing to the risk engine (volatility-aware margin)."""
        return self.risk.compute_position_size(
            total_wallet, atr_pct, exchange_min_notional=self._min_notional
        )

    def _current_unrealized(self, price: float) -> float:
        pos = self.state.position
        if pos is None:
            return 0.0
        qty = pos.quantity_open if pos.quantity_open > 0 else pos.quantity
        if pos.side == "LONG":
            return (price - pos.entry_price) * qty
        return (pos.entry_price - price) * qty

    def _post_sl_entry_blocked(self) -> tuple[bool, str]:
        """Return whether the post-stop-loss bar cooldown is still active."""
        if self._post_sl_cooldown_until is None:
            return False, ""
        now = datetime.now(timezone.utc)
        if now >= self._post_sl_cooldown_until:
            self._post_sl_cooldown_until = None
            return False, ""
        remaining = int((self._post_sl_cooldown_until - now).total_seconds())
        bars = config.POST_SL_COOLDOWN_BARS
        return (
            True,
            f"Post-SL cooldown — waiting {remaining}s ({bars}×{config.INTERVAL} bars) "
            f"before evaluating new entries.",
        )

    def _arm_post_sl_cooldown(self) -> None:
        seconds = compound_strategy.post_sl_cooldown_seconds()
        self._post_sl_cooldown_until = datetime.now(timezone.utc) + timedelta(
            seconds=seconds
        )
        logger.info(
            "Post-SL cooldown armed for %ds (%d bars).",
            seconds,
            config.POST_SL_COOLDOWN_BARS,
        )

    def _flatten_exchange_orphans(self, price: float, *, context: str = "") -> bool:
        """Market-close any open position on the exchange (in-memory may be FLAT)."""
        if self._client is None:
            return False
        try:
            snap = order_execution.fetch_open_position(self._client, config.SYMBOL)
        except Exception as exc:
            logger.error(
                "Exchange position read failed during flatten: %s",
                config.sanitize_for_log(str(exc)),
            )
            return False
        if snap["side"] == "FLAT" or snap["quantity"] <= 0:
            return False
        qty = self._round_step(snap["quantity"])
        if qty <= 0:
            qty = snap["quantity"]
        try:
            order_execution.flatten_position_market(
                self._client,
                symbol=config.SYMBOL,
                quantity=qty,
                position_side=snap["side"],
                step_size=self._step_size,
                qty_precision=self._qty_precision,
            )
        except Exception as exc:
            logger.error(
                "Emergency flatten failed: %s",
                config.sanitize_for_log(str(exc)),
            )
            send_alert(
                "CRITICAL",
                "Flatten failed",
                f"{context}: {exc}",
                key="flatten_failed",
            )
            return False
        note = context or "Risk circuit breaker"
        logger.warning(
            "%s — market-flattened exchange %s qty=%s",
            note,
            snap["side"],
            qty,
        )
        send_alert(
            "CRITICAL",
            "Position flattened",
            f"{note}: closed {snap['side']} qty {qty} @ ~${price:,.2f}",
            key="position_flattened",
        )
        return True

    def _apply_risk_decision(self, decision: RiskDecision, price: float) -> None:
        """Flatten (if required), log halt reason, optionally stop the loop."""
        if decision.flatten_positions:
            if self.state.position is not None:
                self._close_position(price, reason_code="MANUAL", force_market=True)
            self._flatten_exchange_orphans(price, context=decision.reason or "Risk halt")
        if decision.reason:
            self._set_reason(decision.event, "RISK_HALT", decision.reason)
            logger.warning(decision.reason)
            if decision.flatten_positions:
                send_alert(
                    "CRITICAL",
                    "Risk halt",
                    decision.reason,
                    key="risk_halt",
                )
        if decision.flatten_positions and (
            self.risk.state.kill_switch_active
            or "SESSION LOSS LIMIT" in (decision.reason or "")
            or "CONSECUTIVE LOSS LIMIT" in (decision.reason or "")
        ):
            self._stop_event.set()

    def activate_kill_switch(self, reason: str = "Kill switch activated by operator") -> None:
        """Emergency halt: flag file + flatten + stop the loop."""
        self.risk.trigger_kill_switch(reason)
        send_alert("CRITICAL", "Kill switch", reason, key="kill_switch")
        price = self.state.last_price
        if self.state.position is not None and price > 0:
            self._close_position(price, reason_code="MANUAL", force_market=True)
        elif price > 0:
            self._flatten_exchange_orphans(price, context=reason)
        self._stop_event.set()

    def confirm_risk_resume(self) -> str:
        """Clear consecutive-loss pause after operator confirmation."""
        decision = self.risk.confirm_manual_resume()
        return decision.reason

    def clear_kill_switch(self) -> None:
        """Remove kill-switch file after operator review."""
        self.risk.clear_kill_switch()

    def _round_step(self, quantity: float) -> float:
        return order_execution.round_quantity(
            quantity, self._step_size, self._qty_precision
        )

    def _confirm_exchange_flat(self) -> bool:
        """Return True when the exchange reports no open size (or client unavailable)."""
        if self._client is None:
            return True
        try:
            snap = order_execution.fetch_open_position(self._client, config.SYMBOL)
        except Exception as exc:
            logger.error(
                "Could not confirm exchange flat: %s",
                config.sanitize_for_log(str(exc)),
            )
            return False
        return snap["side"] == "FLAT" or float(snap.get("quantity", 0.0) or 0.0) <= 0

    def _recalibrate_position_from_exchange(self) -> dict[str, float | str]:
        """Fetch live position and optionally rebuild local state from it."""
        if self._client is None:
            return {"side": "FLAT", "quantity": 0.0, "entry_price": 0.0}
        snap = order_execution.fetch_open_position(self._client, config.SYMBOL)
        local = self.state.position
        if snap["side"] == "FLAT":
            if local is not None:
                logger.warning(
                    "Recalibrate: exchange FLAT but local held %s — clearing local ghost.",
                    local.side,
                )
                with self.state._lock:
                    self.state.position = None
            return snap
        if local is None:
            logger.critical(
                "Recalibrate: exchange %s qty=%s but local FLAT — orphan detected.",
                snap["side"],
                snap["quantity"],
            )
            return snap
        if local.side != snap["side"]:
            logger.critical(
                "Recalibrate: local %s vs exchange %s — adopting exchange side.",
                local.side,
                snap["side"],
            )
            local.side = str(snap["side"])
        live_qty = float(snap["quantity"])
        local_qty = local.quantity_open if local.quantity_open > 0 else local.quantity
        if abs(local_qty - live_qty) > max(self._step_size, 1e-9):
            logger.warning(
                "Recalibrate qty: local %.6f → exchange %.6f",
                local_qty,
                live_qty,
            )
            local.quantity = live_qty
            local.quantity_open = live_qty
        if float(snap.get("entry_price", 0.0) or 0.0) > 0:
            local.entry_price = float(snap["entry_price"])
        return snap

    def verify_exchange_alignment(self) -> bool:
        """End-of-loop safety net: compare local state vs live exchange position.

        Defensive structure
        -------------------
        1. Re-entrancy guard — never nest panic flattens (avoids infinite loops).
        2. Fetch live exchange position (source of truth).
        3. Compare to local BotState.position (FLAT / LONG / SHORT).
        4. On catastrophic desync → market-flatten exchange size, clear local,
           alert, and pause new entries until manual resume.
        5. Returns True when aligned (or check disabled / no client).
        """
        if not config.EXCHANGE_ALIGNMENT_CHECK:
            return True
        if self._client is None:
            return True
        # Prevent recursive panic if flatten itself triggers another iteration path.
        if self._alignment_panic_active:
            return False

        try:
            snap = order_execution.fetch_open_position(self._client, config.SYMBOL)
        except Exception as exc:
            logger.error(
                "Alignment check skipped — position fetch failed: %s",
                config.sanitize_for_log(str(exc)),
            )
            return True  # fail-open on read errors; do not panic-flatten blindly

        exchange_side = str(snap.get("side", "FLAT"))
        exchange_qty = float(snap.get("quantity", 0.0) or 0.0)
        local = self.state.position
        local_side = local.side if local is not None else "FLAT"

        desync = False
        detail = ""
        if local is None and exchange_side != "FLAT" and exchange_qty > 0:
            desync = True
            detail = (
                f"Local FLAT but exchange holds {exchange_side} qty={exchange_qty} "
                f"— ghost/orphan position."
            )
        elif local is not None and exchange_side == "FLAT":
            desync = True
            detail = (
                f"Local {local_side} but exchange FLAT — stale local state "
                "(close may have filled without ledger sync)."
            )
        elif local is not None and exchange_side not in {"FLAT", local_side}:
            desync = True
            detail = (
                f"Side mismatch: local {local_side} vs exchange {exchange_side} "
                f"qty={exchange_qty}."
            )

        if not desync:
            return True

        logger.critical("EXCHANGE DESYNC DETECTED: %s", detail)
        self._alignment_panic_active = True
        try:
            price = float(self.state.last_price or snap.get("entry_price") or 0.0)
            flattened = False
            if exchange_side != "FLAT" and exchange_qty > 0:
                flattened = self._flatten_exchange_orphans(
                    price,
                    context=f"Alignment panic: {detail}",
                )
                # Second confirm — do not loop forever if flatten fails.
                if not self._confirm_exchange_flat():
                    logger.critical(
                        "Alignment panic flatten did not clear exchange position — "
                        "manual intervention required."
                    )
            with self.state._lock:
                self.state.position = None
                self.state.last_action = "DESYNC_FLATTEN"
                self.state.last_event = "WARNING"
                self.state.last_reason = f"EXCHANGE DESYNC — {detail}"
            self._log_status(price or 0.0, transition=True)
            self.risk.require_manual_resume(
                f"Exchange desync panic flatten. {detail} "
                "New entries paused until dashboard Confirm Risk Resume."
            )
            send_alert(
                "CRITICAL",
                "Exchange desync panic",
                detail,
                key="exchange_desync",
            )
            return flattened and self._confirm_exchange_flat()
        finally:
            self._alignment_panic_active = False

    def _handle_reduce_only_rejection(self, exc: Exception, price: float) -> None:
        """On API -2022: recalibrate from exchange; flatten orphans; never spam."""
        logger.error(
            "ReduceOnly rejected (-2022): %s — recalibrating from exchange.",
            config.sanitize_for_log(str(exc)),
        )
        try:
            snap = self._recalibrate_position_from_exchange()
        except Exception as fetch_exc:
            logger.error(
                "Recalibrate after -2022 failed: %s",
                config.sanitize_for_log(str(fetch_exc)),
            )
            self._set_reason(
                "WARNING",
                "REDUCE_ONLY_REJECT",
                f"ReduceOnly -2022 and recalibrate failed: {fetch_exc}",
            )
            return

        if snap["side"] == "FLAT":
            with self.state._lock:
                self.state.position = None
            self._set_reason(
                "WARNING",
                "REDUCE_ONLY_RECALIBRATED",
                "ReduceOnly -2022: exchange already FLAT — cleared local position.",
            )
            return

        # Exchange still open — one emergency market sweep (guarded).
        if not self._alignment_panic_active:
            self._alignment_panic_active = True
            try:
                self._flatten_exchange_orphans(
                    price,
                    context="ReduceOnly -2022 emergency flatten",
                )
                if self._confirm_exchange_flat():
                    with self.state._lock:
                        self.state.position = None
                else:
                    self.risk.require_manual_resume(
                        "ReduceOnly -2022: emergency flatten did not clear position."
                    )
            finally:
                self._alignment_panic_active = False
        self._set_reason(
            "WARNING",
            "REDUCE_ONLY_REJECT",
            f"ReduceOnly -2022 handled — exchange was {snap['side']} qty={snap['quantity']}.",
        )

    # ------------------------------------------------------------------ #
    # Order execution (futures — post-only maker limits)                   #
    # ------------------------------------------------------------------ #
    def _execute_maker_limit(
        self,
        side: str,
        quantity: float,
        reduce_only: bool,
        book: Optional[order_execution.BookTicker] = None,
    ) -> order_execution.MakerOrderResult:
        """Submit a GTX post-only limit and wait for fill (maker / zero slippage)."""
        return order_execution.place_and_wait_post_only(
            self._client,
            symbol=config.SYMBOL,
            side=side,
            quantity=quantity,
            reduce_only=reduce_only,
            book=book,
            tick_size=self._tick_size,
            price_precision=self._price_precision,
        )

    def _fetch_book(self) -> order_execution.BookTicker:
        return order_execution.fetch_book_ticker(self._client, config.SYMBOL)

    def _log_directional_signal(self, signal: dict, direction: str) -> None:
        """Log model probability vs threshold on every inference scan."""
        p_long = signal["prob_long"]
        p_short = signal["prob_short"]
        long_thr = self._long_threshold
        short_thr = self._short_threshold
        long_ready = p_long > long_thr
        short_ready = p_short > short_thr
        ema_note = "EMA50 gate OFF" if config.is_xgboost_ml_profile() else (
            "EMA50 gate ON" if config.USE_EMA50_TREND_GATE else "EMA50 gate OFF"
        )
        logger.info(
            "SIGNAL EVAL | LONG %.4f vs thr %.4f (%s, gap %+.4f) | "
            "SHORT %.4f vs thr %.4f (%s, gap %+.4f) | CASH %.4f | decision=%s | %s",
            p_long,
            long_thr,
            "PASS" if long_ready else "below",
            p_long - long_thr,
            p_short,
            short_thr,
            "PASS" if short_ready else "below",
            p_short - short_thr,
            signal["prob_cash"],
            direction,
            ema_note,
        )
        # Rolling drift diagnostics — explains long starvation when thresholds are equal.
        self._prob_long_samples.append(float(p_long))
        self._prob_short_samples.append(float(p_short))
        self._prob_drift_scans += 1
        window = max(1, int(config.PROB_DRIFT_LOG_EVERY))
        if self._prob_drift_scans % window == 0:
            n = min(len(self._prob_long_samples), window)
            recent_l = self._prob_long_samples[-n:]
            recent_s = self._prob_short_samples[-n:]
            avg_l = sum(recent_l) / n
            avg_s = sum(recent_s) / n
            max_l = max(recent_l)
            max_s = max(recent_s)
            long_pass = sum(1 for v in recent_l if v > long_thr)
            short_pass = sum(1 for v in recent_s if v > short_thr)
            logger.info(
                "PROB DRIFT | last %d scans | avg LONG %.4f (max %.4f, thr-clears %d) | "
                "avg SHORT %.4f (max %.4f, thr-clears %d) | bias=%s | thr L/S %.3f/%.3f",
                n,
                avg_l,
                max_l,
                long_pass,
                avg_s,
                max_s,
                short_pass,
                "SHORT_HEAVY" if avg_s > avg_l + 0.02 else (
                    "LONG_HEAVY" if avg_l > avg_s + 0.02 else "BALANCED"
                ),
                long_thr,
                short_thr,
            )
            # Cap memory — keep at most 2 windows.
            cap = window * 2
            if len(self._prob_long_samples) > cap:
                self._prob_long_samples = self._prob_long_samples[-cap:]
                self._prob_short_samples = self._prob_short_samples[-cap:]

    def _reject_entry(
        self,
        direction: str,
        probability: float,
        reason_code: str,
        detail: str,
        *,
        event: str = "WARNING",
    ) -> None:
        """Log a threshold-passing signal that was blocked before execution."""
        msg = f"Skipped Entry [{direction} @ {probability * 100:.1f}%]: {detail}"
        logger.warning(msg)
        self._set_reason(event, f"SKIPPED_{direction}_{reason_code}", msg)

    def _current_candle_ts(self, candles) -> str:
        if "Timestamp" in candles.columns and not candles.empty:
            ts = candles["Timestamp"].iloc[-1]
            return str(ts)
        return self._now_iso()

    def _position_bars_held(self, pos: Position, candles) -> int:
        current_ts = self._current_candle_ts(candles)
        return order_execution.bars_elapsed(
            pos.entry_candle_ts,
            current_ts,
            config.INTERVAL,
        )

    @staticmethod
    def _atr14_from_candles(candles) -> float:
        if candles is None or candles.empty or len(candles) < 20:
            return 0.0
        frame = candles.copy()
        prev_close = frame["Close"].shift(1)
        tr = (
            pd.concat(
                [
                    frame["High"] - frame["Low"],
                    (frame["High"] - prev_close).abs(),
                    (frame["Low"] - prev_close).abs(),
                ],
                axis=1,
            )
            .max(axis=1)
            .rolling(14)
            .mean()
        )
        val = tr.iloc[-1]
        return float(val) if pd.notna(val) and float(val) > 0 else 0.0

    def _risk_pct_with_throttle(self) -> float:
        losses = int(self.risk.snapshot().consecutive_losses)
        if losses >= 6:
            return 0.005
        if losses >= 3:
            return 0.01
        return 0.02

    def _open_position(
        self,
        direction: str,
        price: float,
        probability: float,
        atr_pct: float,
        *,
        candles=None,
        book: Optional[order_execution.BookTicker] = None,
        bracket_override: Optional[tuple[float, float]] = None,
        signal_label: str = "XGBoost",
    ) -> None:
        """Open a LONG or SHORT with volatility-aware margin from the risk engine."""
        blocked, block_reason = self._post_sl_entry_blocked()
        if blocked:
            self._set_reason("CASH", f"BLOCKED_{direction}", block_reason)
            logger.warning(block_reason)
            return

        gate = self.risk.check_can_open()
        if not gate.allowed:
            self._set_reason(gate.event, f"BLOCKED_{direction}", gate.reason)
            logger.warning(gate.reason)
            return

        if self.state.connection_degraded:
            reason = (
                f"Valid {direction} signal blocked — API connection is DEGRADED "
                f"({self.state.connection_error or 'recent exchange failures'}). "
                f"Bracket management continues; new entries paused."
            )
            self._set_reason("WARNING", f"BLOCKED_{direction}", reason)
            logger.warning(reason)
            return

        if config.is_xgboost_ml_profile() and config.CONFLUENCE_GATE_ENABLED and not config.BOARDROOM_ENABLED:
            from confluence_gate import verify_btc_trade_safety

            if not verify_btc_trade_safety(direction, store=self.store):
                reason = (
                    f"Confluence Gate VETO — cross-market altcoin sentiment blocked "
                    f"local BTC {direction}. Model signal intercepted; staying FLAT."
                )
                self._set_reason("WARNING", f"BLOCKED_{direction}_RADAR", reason)
                logger.warning(reason)
                return

        if self._last_close_at is not None and config.REENTRY_COOLDOWN_SECONDS > 0:
            elapsed = (
                datetime.now(timezone.utc) - self._last_close_at
            ).total_seconds()
            if elapsed < config.REENTRY_COOLDOWN_SECONDS:
                remaining = int(config.REENTRY_COOLDOWN_SECONDS - elapsed)
                reason = (
                    f"Valid {direction} signal ({probability * 100:.1f}%), but "
                    f"re-entry cooldown active ({remaining}s remaining). "
                    f"Compounding pause — waiting before next entry."
                )
                self._set_reason("CASH", "HOLD", reason)
                return

        cooldown_sleep = 0.0 if config.is_darvas_box_profile() else (0.5 if config.is_compound_profile() else 1.5)
        if cooldown_sleep > 0:
            time.sleep(cooldown_sleep)
        total_wallet = self._get_total_wallet_balance()
        available = self._get_usdt_balance()
        sizing = self._compute_order_margin(total_wallet, atr_pct)
        order_usdt_value = sizing.margin_usdt
        notional = sizing.notional_usdt
        side = "BUY" if direction == "LONG" else "SELL"
        effective_alloc = sizing.base_pct * sizing.vol_scale * 100.0

        atr14 = self._atr14_from_candles(candles)
        if config.is_darvas_box_profile():
            if atr14 <= 0:
                self._reject_entry(direction, probability, "ATR", "ATR(14) unavailable for risk sizing.")
                return
            risk_pct = self._risk_pct_with_throttle()
            risk_cash = max(total_wallet, 0.0) * risk_pct
            sl_distance = 1.5 * atr14
            if risk_cash <= 0 or sl_distance <= 0:
                self._reject_entry(direction, probability, "RISK", "Invalid risk cash or SL distance.")
                return
            quantity = self._round_step(risk_cash / sl_distance)
            if quantity <= 0:
                self._reject_entry(direction, probability, "SIZE", "Risk-sized quantity rounded to zero.")
                return
            notional = quantity * price
            order_usdt_value = notional / max(float(config.LEVERAGE), 1.0)
            effective_alloc = risk_pct * 100.0
            logger.info(
                "Darvas risk throttle sizing | losses=%d | risk_pct=%.2f%% | qty=%.6f | atr14=%.4f",
                self.risk.snapshot().consecutive_losses,
                risk_pct * 100.0,
                quantity,
                atr14,
            )

        # --- Blocked: insufficient free margin for allocated slice -------- #
        if available < order_usdt_value:
            reason = (
                f"{signal_label} triggered a valid {side} signal "
                f"({probability * 100:.1f}% probability), but the order was "
                f"BLOCKED due to insufficient USDT balance "
                f"(need ${order_usdt_value:,.2f}, available ${available:,.2f})."
            )
            self._set_reason("WARNING", f"BLOCKED_{direction}", reason)
            logger.warning(reason)
            return

        # Exchange min-notional guard (e.g. -4164 if below 50 USDT on testnet).
        if notional < self._min_notional:
            reason = (
                f"{signal_label} triggered a valid {side} signal "
                f"({probability * 100:.1f}% probability), but the order was "
                f"BLOCKED because notional ${notional:,.2f} is below the "
                f"exchange minimum ${self._min_notional:,.2f}."
            )
            self._set_reason("WARNING", f"BLOCKED_{direction}", reason)
            logger.warning(reason)
            return

        if not config.is_darvas_box_profile():
            quantity = self._round_step(notional / price)
            if quantity <= 0:
                reason = (
                    f"Valid {side} signal ({probability * 100:.1f}%), but the "
                    f"computed size rounds to zero at ${price:,.2f}. Order skipped."
                )
                self._set_reason("WARNING", f"BLOCKED_{direction}", reason)
                logger.warning(reason)
                return

        sanity = self.risk.validate_order_sanity(price, quantity, notional)
        if not sanity.allowed:
            self._reject_entry(direction, probability, "SANITY", sanity.reason)
            return

        # --- Spread protection gate (audit liquidity filter) ------------- #
        try:
            book = book or self._fetch_book()
        except Exception as exc:  # pragma: no cover - network dependent
            self._reject_entry(
                direction,
                probability,
                "BOOK",
                f"Could not read order book: {config.sanitize_for_log(str(exc))}",
            )
            return

        spread_gate = order_execution.check_spread_gate(book)
        if not spread_gate.allowed:
            self._reject_entry(direction, probability, "SPREAD", spread_gate.reason)
            return

        # --- Execute post-only maker entry ------------------------------- #
        order_result = None
        fill_price = price
        attempt_qty = quantity
        for _ in range(6):
            order_result = self._execute_maker_limit(
                side, attempt_qty, reduce_only=False, book=book
            )
            if order_result.success:
                fill_price = order_result.fill_price
                quantity = attempt_qty
                break
            reason_text = order_result.reason or "Post-only entry did not fill"
            margin_insufficient = "margin" in reason_text.lower()
            if margin_insufficient:
                attempt_qty = self._round_step(attempt_qty * 0.97)
                if attempt_qty <= 0:
                    break
                continue
            self._reject_entry(direction, probability, "POST_ONLY", reason_text)
            return

        if order_result is None or not order_result.success:
            self._reject_entry(
                direction,
                probability,
                "POST_ONLY",
                "Post-only entry failed after auto-resizing attempts.",
            )
            return

        if bracket_override is not None:
            tp, sl = bracket_override
        else:
            tp, sl = bracket_prices(direction, fill_price, atr_pct)
        tp1 = tp
        tp2 = tp
        split_enabled = False
        tp1_qty = 0.0
        runner_qty = quantity
        if config.is_darvas_box_profile() and candles is not None and self._active_box and self._active_box.valid:
            box_h = max(self._active_box.height, fill_price * 0.0005)
            sl = fill_price - (1.5 * atr14) if direction == "LONG" else fill_price + (1.5 * atr14)
            tp1 = fill_price + box_h if direction == "LONG" else fill_price - box_h
            rr_distance = 2.0 * abs(fill_price - sl)
            tp2 = fill_price + rr_distance if direction == "LONG" else fill_price - rr_distance
            half = self._round_step(quantity * 0.5)
            min_qty = self._round_step(max(self._step_size, self._min_notional / max(fill_price, 1e-9)))
            if half > 0 and half >= min_qty and (quantity - half) >= min_qty:
                tp1_qty = half
                runner_qty = self._round_step(quantity - half)
                split_enabled = runner_qty > 0
            else:
                logger.warning(
                    "Split disabled: half-quantity below exchange floor (half=%.6f, min=%.6f).",
                    half,
                    min_qty,
                )
            tp = tp2
        entry_candle_ts = self._current_candle_ts(candles) if candles is not None else self._now_iso()

        floor_note = ""
        if sizing.exchange_floor_applied:
            floor_note = (
                f" [EXCHANGE_MIN_FLOOR: intended ${sizing.intended_margin_usdt:,.2f} "
                f"margin raised to ${order_usdt_value:,.2f} for min notional "
                f"${self._min_notional:,.2f}]"
            )
            logger.warning(
                "Exchange minimum margin floor applied: intended $%.2f → $%.2f "
                "(min notional $%.2f, %dx leverage).",
                sizing.intended_margin_usdt,
                order_usdt_value,
                self._min_notional,
                config.LEVERAGE,
            )

        order_mode = "POST-ONLY LIMIT (GTX/maker)" if config.USE_POST_ONLY_MAKER else "LIMIT"
        reason = (
            f"{signal_label} triggered {side} signal ({probability * 100:.1f}% "
            f"probability). Entering {direction} via {order_mode} at "
            f"${fill_price:,.2f} (spread {spread_gate.spread_pct * 100:.4f}%). "
            f"{effective_alloc:.0f}% vol-scaled allocation "
            f"(${order_usdt_value:,.2f} USDT margin, scale {sizing.vol_scale:.2f}, "
            f"{config.LEVERAGE}x → ${notional:,.2f} notional). "
            f"Audit brackets TP ${tp:,.2f} / SL ${sl:,.2f}; "
            f"entry bar {entry_candle_ts}; max hold {config.FORWARD_WINDOW} bars."
            f"{floor_note}"
        )
        event = "BUY_LONG" if direction == "LONG" else "SHORT_ORDER"

        with self.state._lock:
            self.state.position = Position(
                side=direction,
                entry_price=fill_price,
                quantity=quantity,
                quantity_open=quantity,
                entry_time=self._now_iso(),
                entry_candle_ts=entry_candle_ts,
                take_profit_price=tp,
                stop_loss_price=sl,
                best_price=fill_price,
                tp1_price=tp1,
                tp2_price=tp2,
                tp1_quantity=tp1_qty,
                runner_quantity=runner_qty,
                split_enabled=split_enabled,
                atr_at_entry=atr14,
            )
            self.state.last_action = f"OPEN_{direction}"
            self.state.last_event = event
            self.state.last_reason = reason
        logger.info(reason)
        send_alert(
            "INFO",
            f"Opened {direction}",
            f"{direction} @ ${fill_price:,.2f} qty {quantity} TP ${tp:,.2f} SL ${sl:,.2f}",
            key=f"open_{direction}",
        )
        # The OPEN transition gets its own log row, even if a CLOSE already
        # happened earlier in this same iteration.
        self._log_status(fill_price, transition=True)

    def _set_reason(self, event: str, action: str, reason: str) -> None:
        """Thread-safely record the latest event category, action, and reason."""
        with self.state._lock:
            self.state.last_event = event
            self.state.last_action = action
            self.state.last_reason = reason

    _PRESERVE_SCAN_ACTION_PREFIXES = (
        "OPEN_",
        "CLOSE_",
        "SKIPPED_",
        "BLOCKED_",
        "EXIT_PENDING_",
        "RISK_",
    )

    def _finalize_scan_heartbeat(
        self,
        *,
        direction: str,
        signal: dict,
        live_price: float,
        pos: Optional[Position],
    ) -> None:
        """Mark this iteration as a visible SCAN row for the dashboard activity feed."""
        with self.state._lock:
            action = str(self.state.last_action or "")
            if any(action.startswith(prefix) for prefix in self._PRESERVE_SCAN_ACTION_PREFIXES):
                return
            self.state.last_action = "SCAN"
            self.state.last_event = "SCAN"
            if pos is not None:
                trail = " · trail ON" if pos.trail_active else ""
                self.state.last_reason = (
                    f"Scan Complete: Managing open {pos.side} @ ${live_price:,.2f} "
                    f"(entry ${pos.entry_price:,.2f}, TP ${pos.take_profit_price:,.2f}, "
                    f"SL ${pos.stop_loss_price:,.2f}{trail})."
                )
                return
            if config.is_xgboost_ml_profile():
                p_long = signal["prob_long"] * 100.0
                p_short = signal["prob_short"] * 100.0
                long_thr = self._long_threshold * 100.0
                short_thr = self._short_threshold * 100.0
                if direction == "CASH":
                    self.state.last_reason = (
                        "Scan Complete: Model checked, staying FLAT | "
                        f"LONG {p_long:.1f}%/{long_thr:.1f}% | "
                        f"SHORT {p_short:.1f}%/{short_thr:.1f}%"
                    )
                else:
                    self.state.last_reason = (
                        f"Scan Complete: Model signal {direction} | "
                        f"LONG {p_long:.1f}%/{long_thr:.1f}% | "
                        f"SHORT {p_short:.1f}%/{short_thr:.1f}%"
                    )
                return
            self.state.last_reason = (
                f"Scan Complete: Darvas scan -> {direction} @ ${live_price:,.2f}."
            )

    def _close_partial_position(
        self,
        pos: Position,
        close_qty: float,
        price: float,
        *,
        reason_code: str,
        book: Optional[order_execution.BookTicker] = None,
    ) -> bool:
        side = "SELL" if pos.side == "LONG" else "BUY"
        qty = self._round_step(close_qty)
        if qty <= 0:
            return False
        order_result = self._execute_maker_limit(side, qty, reduce_only=True, book=book)
        if not order_result.success:
            logger.warning("Partial close pending (%s): %s", reason_code, order_result.reason or "not filled")
            return False
        fill = order_result.fill_price
        if pos.side == "LONG":
            pnl = (fill - pos.entry_price) * qty
        else:
            pnl = (pos.entry_price - fill) * qty
        with self.state._lock:
            pos.quantity_open = max(0.0, pos.quantity_open - qty)
            pos.partial_realized_pnl += pnl
            self.state.realized_pnl += pnl
            self.state.last_action = f"PARTIAL_{pos.side}_{reason_code}"
            self.state.last_event = "FILL_SUCCESS"
            self.state.last_reason = f"Partial {reason_code} filled @ ${fill:,.2f} qty {qty:.6f}"
        logger.info(
            "Partial close %s %s qty %.6f @ %.2f pnl %+0.2f",
            pos.side,
            reason_code,
            qty,
            fill,
            pnl,
        )
        return True

    def _close_position(
        self,
        price: float,
        reason_code: str,
        *,
        book: Optional[order_execution.BookTicker] = None,
        force_market: bool = False,
    ) -> None:
        """Close the open position; never clear local state until exchange is flat.

        ``reason_code`` is one of ``TP``, ``SL``, ``TIMEOUT``, ``FLIP``, or
        ``MANUAL``. Close side/qty are taken from the live exchange position when
        available so ReduceOnly (-2022) cannot spam against a mismatched book.
        """
        pos = self.state.position
        if pos is None:
            return

        # --- Sync with exchange before ordering --------------------------- #
        exchange_side = pos.side
        quantity = self._round_step(
            pos.quantity_open if pos.quantity_open > 0 else pos.quantity
        )
        if self._client is not None:
            try:
                snap = order_execution.fetch_open_position(self._client, config.SYMBOL)
                if snap["side"] == "FLAT" or float(snap["quantity"]) <= 0:
                    logger.warning(
                        "Close %s skipped — exchange already FLAT (local %s). "
                        "Clearing ghost local state without recording a false fill.",
                        reason_code,
                        pos.side,
                    )
                    with self.state._lock:
                        self.state.position = None
                        self.state.last_action = f"CLOSE_{pos.side}_{reason_code}_GHOST"
                        self.state.last_event = "WARNING"
                        self.state.last_reason = (
                            f"Local {pos.side} cleared — exchange already flat "
                            f"before {reason_code} close."
                        )
                    self._log_status(price, transition=True)
                    return
                exchange_side = str(snap["side"])
                quantity = self._round_step(float(snap["quantity"]))
                if quantity <= 0:
                    quantity = float(snap["quantity"])
                if exchange_side != pos.side:
                    logger.warning(
                        "Close side desync: local %s vs exchange %s — using exchange.",
                        pos.side,
                        exchange_side,
                    )
                    pos.side = exchange_side
            except Exception as exc:
                logger.warning(
                    "Pre-close position sync failed (%s) — using local state.",
                    config.sanitize_for_log(str(exc)),
                )

        if quantity <= 0:
            quantity = pos.quantity_open if pos.quantity_open > 0 else pos.quantity

        # Closing a LONG means SELL; closing a SHORT means BUY.
        side = "SELL" if pos.side == "LONG" else "BUY"
        use_maker = config.USE_POST_ONLY_MAKER and not force_market
        fill_price = price
        exit_mode = "MARKET"
        order_ok = False

        def _market_close() -> float:
            order = exchange_client.call_with_retry(
                self._client.futures_create_order,
                symbol=config.SYMBOL,
                side=side,
                type="MARKET",
                quantity=quantity,
                reduceOnly=True,
                label="futures_close_market",
                attempts=2,
            )
            return self._extract_fill_price(order, fallback=price)

        try:
            if use_maker:
                order_result = self._execute_maker_limit(
                    side, quantity, reduce_only=True, book=book
                )
                if order_result.success:
                    fill_price = order_result.fill_price
                    exit_mode = "POST-ONLY LIMIT (GTX/maker)"
                    order_ok = True
                else:
                    detail = order_result.reason or "Post-only exit did not fill"
                    # TIMEOUT / risk exits escalate immediately — never idle as ghost.
                    if reason_code in {"TIMEOUT", "MANUAL", "SL", "TP", "FLIP"}:
                        logger.warning(
                            "Maker exit failed for %s (%s) — escalating to MARKET sweep.",
                            reason_code,
                            detail,
                        )
                        fill_price = _market_close()
                        exit_mode = "MARKET (escalated)"
                        order_ok = True
                    else:
                        logger.warning(
                            "Skipped Exit [%s %s]: %s — will retry next scan.",
                            pos.side,
                            reason_code,
                            detail,
                        )
                        self._set_reason(
                            "WARNING",
                            f"EXIT_PENDING_{reason_code}",
                            f"Post-only exit pending for {pos.side}: {detail}",
                        )
                        return
            else:
                fill_price = _market_close()
                exit_mode = "MARKET"
                order_ok = True
        except Exception as exc:
            if order_execution.is_reduce_only_reject(exc):
                self._handle_reduce_only_rejection(exc, price)
                # If exchange is now flat we can finish the ledger; else abort.
                if not self._confirm_exchange_flat():
                    return
                fill_price = price
                exit_mode = "MARKET (recalibrated after -2022)"
                order_ok = True
            elif reason_code in {"TIMEOUT", "MANUAL", "SL", "TP"}:
                logger.error(
                    "Close order failed for %s: %s — emergency flatten.",
                    reason_code,
                    config.sanitize_for_log(str(exc)),
                )
                if not self._flatten_exchange_orphans(
                    price, context=f"{reason_code} close failure"
                ):
                    self._set_reason(
                        "WARNING",
                        f"EXIT_FAILED_{reason_code}",
                        f"Close failed and emergency flatten failed: {exc}",
                    )
                    return
                fill_price = price
                exit_mode = "MARKET (emergency flatten)"
                order_ok = True
            else:
                raise

        if not order_ok:
            return

        # --- Hard gate: never mark closed locally until exchange is flat --- #
        if not self._confirm_exchange_flat():
            logger.error(
                "%s close reported success but exchange still open — emergency sweep.",
                reason_code,
            )
            if not self._flatten_exchange_orphans(
                fill_price, context=f"{reason_code} residual position"
            ):
                self._set_reason(
                    "WARNING",
                    f"EXIT_PENDING_{reason_code}",
                    f"{reason_code} residual size remains on exchange — not clearing local.",
                )
                return
            if not self._confirm_exchange_flat():
                self.risk.require_manual_resume(
                    f"{reason_code} close left residual size; manual flatten required."
                )
                return

        if pos.side == "LONG":
            pnl = (fill_price - pos.entry_price) * quantity
            pct = (fill_price - pos.entry_price) / pos.entry_price * 100.0
        else:  # SHORT profits when price falls
            pnl = (pos.entry_price - fill_price) * quantity
            pct = (pos.entry_price - fill_price) / pos.entry_price * 100.0
        pnl += pos.partial_realized_pnl

        if reason_code == "TP":
            event = "FILL_SUCCESS"
            reason = (
                f"Target price reached ({pct:+.2f}% gain). Executed {exit_mode} "
                f"{side} to close {pos.side} position at ${fill_price:,.2f}."
            )
        elif reason_code == "SL":
            event = "STOP_LOSS"
            reason = (
                f"Stop-loss triggered ({pct:+.2f}%). Executed {exit_mode} "
                f"{side} to close {pos.side} position at ${fill_price:,.2f} "
                f"and protect capital."
            )
        elif reason_code == "TIMEOUT":
            event = "FILL_SUCCESS"
            bars = config.FORWARD_WINDOW
            reason = (
                f"Time horizon reached ({bars} bars / {config.INTERVAL} without "
                f"organic TP/SL; {pct:+.2f}%). Executed {exit_mode} {side} to "
                f"close {pos.side} at ${fill_price:,.2f}. Exchange confirmed FLAT."
            )
        elif reason_code == "MANUAL":
            event = "STOP_LOSS"
            reason = (
                f"Risk engine force-close of {pos.side} position ({pct:+.2f}%) "
                f"at ${fill_price:,.2f} via {exit_mode} {side}."
            )
        else:  # FLIP
            event = "FILL_SUCCESS"
            reason = (
                f"Signal reversed. Closing {pos.side} position ({pct:+.2f}%) at "
                f"${fill_price:,.2f} via {exit_mode} {side} before flipping "
                f"direction."
            )

        exit_ts = self._now_iso()
        peak = max(pos.peak_unrealized, pnl)

        with self.state._lock:
            self.state.realized_pnl += pnl
            self.state.completed_trades += 1
            if pos.side == "LONG":
                self.state.long_trades += 1
            else:
                self.state.short_trades += 1
            self.state.position = None
            self.state.last_action = f"CLOSE_{pos.side}_{reason_code}"
            self.state.last_event = event
            self.state.last_reason = reason
        logger.info(reason)
        if reason_code == "SL":
            self._arm_post_sl_cooldown()
        if reason_code != "FLIP":
            self._last_close_at = datetime.now(timezone.utc)
        if reason_code in ("TP", "SL", "TIMEOUT"):
            self._bracket_closed_this_iteration = True

        # Ground-truth ledger: one atomic row per completed trade.
        self.store.record_trade(
            TradeRecord(
                session_id=self.session_id or "",
                side=pos.side,
                entry_ts=pos.entry_time,
                exit_ts=exit_ts,
                entry_price=pos.entry_price,
                exit_price=fill_price,
                quantity=pos.quantity,
                tp_price=pos.take_profit_price,
                sl_price=pos.stop_loss_price,
                peak_unrealized=peak,
                realized_pnl=pnl,
                outcome=reason_code,
            )
        )
        pause = self.risk.record_trade_close(pnl)
        if not pause.allowed:
            self._set_reason(pause.event, "RISK_HALT", pause.reason)
            logger.warning(pause.reason)
            if pause.flatten_positions:
                self._flatten_exchange_orphans(fill_price, context=pause.reason)
                send_alert(
                    "CRITICAL",
                    "Consecutive-loss circuit breaker",
                    pause.reason,
                    key="consecutive_loss_halt",
                )
                self._stop_event.set()
        send_alert(
            "INFO",
            f"Closed {pos.side} ({reason_code})",
            f"PnL ${pnl:+,.2f} exit ${fill_price:,.2f}",
            key=f"close_{reason_code}",
        )
        # The CLOSE transition gets its own log row, even if an OPEN follows
        # in this same iteration.
        self._log_status(fill_price, transition=True)

    @staticmethod
    def _extract_fill_price(order: dict, fallback: float) -> float:
        """Compute the average fill price from a futures market order response."""
        try:
            avg = order.get("avgPrice")
            if avg is not None and float(avg) > 0:
                return float(avg)
            fills = order.get("fills", [])
            if fills:
                total_qty = sum(float(f["qty"]) for f in fills)
                total_quote = sum(float(f["price"]) * float(f["qty"]) for f in fills)
                if total_qty > 0:
                    return total_quote / total_qty
        except (TypeError, ValueError, KeyError) as exc:
            logger.warning("Could not extract fill price from order response: %s", exc)
        return fallback

    # ------------------------------------------------------------------ #
    # Logging                                                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _log_status(self, price: float, transition: bool = False) -> None:
        """Write one status row to the SQLite store.

        ``transition=True`` marks rows written by open/close transitions so
        the end-of-iteration heartbeat knows not to duplicate them.
        """
        with self.state._lock:
            pos = self.state.position
            open_status = pos.side if pos else "FLAT"
            if pos and pos.side == "LONG":
                qty = pos.quantity_open if pos.quantity_open > 0 else pos.quantity
                unrealized = (price - pos.entry_price) * qty
            elif pos and pos.side == "SHORT":
                qty = pos.quantity_open if pos.quantity_open > 0 else pos.quantity
                unrealized = (pos.entry_price - price) * qty
            else:
                unrealized = 0.0
            row = StatusRow(
                ts=self._now_iso(),
                session_id=self.session_id or "",
                price=round(price, 2),
                prob_long=round(self.state.prob_long, 4),
                prob_short=round(self.state.prob_short, 4),
                prob_cash=round(self.state.prob_cash, 4),
                direction=self.state.signal_direction,
                balance=round(self.state.usdt_balance, 2),
                open_position=open_status,
                realized_pnl=round(self.state.realized_pnl, 2),
                unrealized_pnl=round(unrealized, 2),
                entry_price=round(pos.entry_price, 2) if pos else None,
                tp_price=round(pos.take_profit_price, 2) if pos else None,
                sl_price=round(pos.stop_loss_price, 2) if pos else None,
                action=self.state.last_action,
                event=self.state.last_event,
                reason=self.state.last_reason,
            )
        try:
            self.store.log_status(row)
        except Exception as exc:  # pragma: no cover - disk issues
            logger.error("Failed to write status row: %s", exc)
        if not transition:
            self._write_runtime_snapshot()
        if transition:
            self._transition_logged = True

    # ------------------------------------------------------------------ #
    # Core loop                                                          #
    # ------------------------------------------------------------------ #
    def _iteration(self) -> None:
        self._transition_logged = False
        self._bracket_closed_this_iteration = False

        candles = data_pipeline.fetch_latest_candles()
        if candles.empty:
            msg = "No live candles returned — possible data/API outage."
            logger.warning(msg)
            self._mark_stale_data(msg)
            send_alert("WARNING", "Stale market data", msg, key="empty_candles")
            with self.state._lock:
                self.state.last_action = "SCAN"
                self.state.last_event = "SCAN"
                self.state.last_reason = f"Scan Complete: STALE — {msg}"
            self._log_status(self.state.last_price or 0.0)
            return

        price = float(candles["Close"].iloc[-1])
        live_price = price
        self.risk.update_last_good_price(live_price)
        unrealized = self._current_unrealized(live_price)

        kill = self.risk.check_kill_switch()
        if not kill.allowed:
            self._apply_risk_decision(kill, price)
            if not self._transition_logged:
                self._log_status(price)
            return

        with self.state._lock:
            realized = self.state.realized_pnl
        loss_limit = self.risk.check_session_loss_limit(realized, unrealized)
        if not loss_limit.allowed:
            self._apply_risk_decision(loss_limit, price)
            if not self._transition_logged:
                self._log_status(price)
            return

        from feature_factory import compute_live_features

        if config.is_darvas_box_profile():
            atr_pct = config.RISK_ATR_BASELINE_PCT
        else:
            enriched = compute_live_features(candles)
            atr_pct = (
                float(enriched["atr_pct"].dropna().iloc[-1])
                if not enriched["atr_pct"].dropna().empty
                else config.RISK_ATR_BASELINE_PCT
            )

        box_state: Optional[box_strategy.BoxState] = None
        entry_brackets: Optional[tuple[float, float]] = None
        signal_label = "XGBoost"
        if config.is_xgboost_ml_profile():
            try:
                signal = model_brain.predict_latest(self._model, candles)
            except (ValueError, IndexError, KeyError) as exc:
                msg = f"Inference unavailable: {exc}"
                logger.warning(msg)
                self._mark_stale_data(msg)
                with self.state._lock:
                    self.state.last_action = "SCAN"
                    self.state.last_event = "SCAN"
                    self.state.last_reason = f"Scan Complete: STALE — {msg}"
                self._log_status(live_price)
                return
            direction = model_brain.decide_direction(
                signal["prob_long"],
                signal["prob_short"],
                signal["trend"],
                self._long_threshold,
                self._short_threshold,
                price_vs_ema50=signal.get("price_vs_ema50", 0.0),
                prob_cash=signal.get("prob_cash"),
                use_ema50_gate=False,
            )
            self._log_directional_signal(signal, direction)
            execution_direction = direction
            boardroom_resolution = None
            if config.BOARDROOM_ENABLED:
                from agent_boardroom import resolve_boardroom_verdict

                boardroom_resolution = resolve_boardroom_verdict(
                    direction, store=self.store
                )
                execution_direction = boardroom_resolution.execution_direction
                if boardroom_resolution.override:
                    with self.state._lock:
                        self.state.last_reason = (
                            f"Boardroom override → {boardroom_resolution.verdict}: "
                            f"{boardroom_resolution.detail}"
                        )
        else:
            daily_high, daily_low = self._resolve_darvas_daily_bounds()
            box_state = self._box_engine.evaluate(
                candles,
                daily_high=daily_high,
                daily_low=daily_low,
            )
            self._active_box = box_state
            direction = "CASH"
            if box_state.valid and box_state.volume_ok and box_state.trend_ok and box_state.adx_ok:
                if live_price > box_state.top:
                    direction = "LONG"
                elif live_price < box_state.bottom:
                    direction = "SHORT"
                if self._darvas_consumed_signal_ts == box_state.timestamp:
                    direction = "CASH"
            signal = {
                "prob_long": 1.0 if direction == "LONG" else 0.0,
                "prob_short": 1.0 if direction == "SHORT" else 0.0,
                "prob_cash": 1.0 if direction == "CASH" else 0.0,
                "trend": 0.0,
            }
            signal_label = "Darvas Box"
            execution_direction = direction
            boardroom_resolution = None
            if box_state.valid and direction in {"LONG", "SHORT"}:
                entry_brackets = self._box_brackets(direction, live_price, box_state)
            logger.info(
                "BOX SCAN | top %.8f | bottom %.8f | middle %.8f | closed %.8f | live %.8f | "
                "breakout=%s | volume_ok=%s | trend_ok=%s | adx_ok=%s | signal_bar=%s",
                box_state.top,
                box_state.bottom,
                box_state.middle_line,
                box_state.close,
                live_price,
                direction,
                box_state.volume_ok,
                box_state.trend_ok,
                box_state.adx_ok,
                box_state.timestamp,
            )

        # Balance fetch must stay OUTSIDE state._lock — _get_usdt_balance() updates
        # connection flags via _mark_api_success(), which also acquires the lock.
        usdt_balance = self._get_usdt_balance()
        with self.state._lock:
            self.state.last_price = live_price
            self.state.prob_long = signal["prob_long"]
            self.state.prob_short = signal["prob_short"]
            self.state.prob_cash = signal["prob_cash"]
            self.state.signal_direction = direction
            self.state.usdt_balance = usdt_balance
            self.state.last_action = "HOLD"
            self.state.last_event = "WAIT"
            if config.is_xgboost_ml_profile():
                self.state.last_reason = (
                    f"Scanning market — LONG {signal['prob_long'] * 100:.1f}% | "
                    f"SHORT {signal['prob_short'] * 100:.1f}% | direction {direction}."
                )
            elif box_state and box_state.valid:
                self.state.last_reason = (
                    f"Darvas box active ({box_state.bottom:,.2f} - {box_state.top:,.2f}); "
                    f"closed ${box_state.close:,.2f} / live ${live_price:,.2f} -> {direction}."
                )
            else:
                self.state.last_reason = (
                    "Darvas box not confirmed yet — waiting for enough candles."
                )

        pos = self.state.position

        # --- Bracket management for any open position --------------------- #
        if pos is not None:
            bars_held = self._position_bars_held(pos, candles)
            # Track the best floating PnL seen while the position is open.
            if pos.side == "LONG":
                qty = pos.quantity_open if pos.quantity_open > 0 else pos.quantity
                floating = (live_price - pos.entry_price) * qty
            else:
                qty = pos.quantity_open if pos.quantity_open > 0 else pos.quantity
                floating = (pos.entry_price - live_price) * qty
            with self.state._lock:
                pos.peak_unrealized = max(pos.peak_unrealized, floating)
                trail_note = " · trail ON" if pos.trail_active else ""
                self.state.last_reason = (
                    f"Managing open {pos.side} — entry ${pos.entry_price:,.2f}, "
                    f"TP ${pos.take_profit_price:,.2f}, SL ${pos.stop_loss_price:,.2f}, "
                    f"bar {bars_held}/{config.FORWARD_WINDOW}{trail_note}."
                )
            if config.use_forward_window_exit() and bars_held >= config.FORWARD_WINDOW:
                logger.warning(
                    "TIMEOUT exit triggered — %s held %d/%d bars without organic TP/SL.",
                    pos.side,
                    bars_held,
                    config.FORWARD_WINDOW,
                )
                self._close_position(live_price, reason_code="TIMEOUT")
                pos = None
            elif pos is not None:
                if config.is_darvas_box_profile():
                    if pos.split_enabled and not pos.tp1_hit:
                        tp1_hit = (live_price >= pos.tp1_price) if pos.side == "LONG" else (live_price <= pos.tp1_price)
                        if tp1_hit:
                            if self._close_partial_position(pos, pos.tp1_quantity, live_price, reason_code="TP1"):
                                pos.tp1_hit = True
                                pos.stop_loss_price = pos.entry_price
                                pos.trail_active = True
                    if pos.split_enabled and pos.tp1_hit:
                        atr_now = self._atr14_from_candles(candles)
                        if atr_now > 0:
                            if pos.side == "LONG":
                                candidate = live_price - (1.5 * atr_now)
                                if candidate > pos.stop_loss_price:
                                    pos.stop_loss_price = candidate
                            else:
                                candidate = live_price + (1.5 * atr_now)
                                if candidate < pos.stop_loss_price:
                                    pos.stop_loss_price = candidate
                    else:
                        self._trail_stop_from_box(pos, self._active_box or box_strategy.BoxState(valid=False))
                else:
                    compound_strategy.update_trailing_stop(pos, price)
                if pos.side == "LONG":
                    target = pos.tp2_price if (config.is_darvas_box_profile() and pos.split_enabled) else pos.take_profit_price
                    if live_price >= target:
                        self._close_position(live_price, reason_code="TP")
                        pos = None
                    elif live_price <= pos.stop_loss_price:
                        self._close_position(live_price, reason_code="SL")
                        pos = None
                elif pos.side == "SHORT":
                    target = pos.tp2_price if (config.is_darvas_box_profile() and pos.split_enabled) else pos.take_profit_price
                    if live_price <= target:
                        self._close_position(live_price, reason_code="TP")
                        pos = None
                    elif live_price >= pos.stop_loss_price:
                        self._close_position(live_price, reason_code="SL")
                        pos = None

        # --- Directional entry / flip logic ------------------------------ #
        trade_direction = (
            execution_direction if config.is_xgboost_ml_profile() else direction
        )
        post_sl_blocked, post_sl_reason = self._post_sl_entry_blocked()
        if post_sl_blocked and self.state.position is None:
            self._set_reason("CASH", "HOLD", post_sl_reason)
        elif self._bracket_closed_this_iteration:
            pass
        elif trade_direction == "LONG":
            if pos is not None and pos.side == "SHORT":
                self._close_position(live_price, reason_code="FLIP")
                pos = None
            if pos is None:
                self._open_position(
                    "LONG",
                    live_price,
                    signal["prob_long"],
                    atr_pct,
                    candles=candles,
                    bracket_override=entry_brackets,
                    signal_label=signal_label,
                )
                if config.is_darvas_box_profile() and box_state and self.state.position is not None:
                    self._darvas_consumed_signal_ts = box_state.timestamp
        elif trade_direction == "SHORT":
            if pos is not None and pos.side == "LONG":
                self._close_position(live_price, reason_code="FLIP")
                pos = None
            if pos is None:
                self._open_position(
                    "SHORT",
                    live_price,
                    signal["prob_short"],
                    atr_pct,
                    candles=candles,
                    bracket_override=entry_brackets,
                    signal_label=signal_label,
                )
                if config.is_darvas_box_profile() and box_state and self.state.position is not None:
                    self._darvas_consumed_signal_ts = box_state.timestamp
        else:
            # CASH: explain why no trade was taken (only when flat; if a position
            # is open the bracket messages above already describe the state).
            if pos is None:
                if (
                    config.is_xgboost_ml_profile()
                    and boardroom_resolution is not None
                    and not boardroom_resolution.passthrough
                ):
                    cash_reason = boardroom_resolution.detail
                    logger.info("Boardroom hold: %s", cash_reason)
                elif config.is_xgboost_ml_profile():
                    cash_reason = self._cash_reason(signal)
                    if (
                        signal["prob_long"] > self._long_threshold
                        or signal["prob_short"] > self._short_threshold
                    ):
                        logger.info("Skipped Entry [CASH]: %s", cash_reason)
                else:
                    cash_reason = self._box_cash_reason(
                        box_state or box_strategy.BoxState(valid=False)
                    )
                self._set_reason("CASH", "HOLD", cash_reason)

        # Scan heartbeat — one SQLite row every iteration (visible in Session Activity).
        if not self._transition_logged:
            self._finalize_scan_heartbeat(
                direction=direction,
                signal=signal,
                live_price=live_price,
                pos=self.state.position,
            )
            self._log_status(price)
        else:
            self._write_runtime_snapshot()

        # Safety net — always run last so a ghost/orphan cannot survive a scan.
        self.verify_exchange_alignment()

    def _write_runtime_snapshot(self) -> None:
        import bot_runtime

        with self.state._lock:
            pos = self.state.position
            risk_snap = self.risk.snapshot()
            payload = {
                "pid": os.getpid(),
                "session_id": self.session_id or "",
                "running": bool(self.state.running),
                "last_price": float(self.state.last_price or 0.0),
                "usdt_balance": float(self.state.usdt_balance or 0.0),
                "connection_degraded": bool(self.state.connection_degraded),
                "connection_error": str(self.state.connection_error or ""),
                "last_error": str(self.state.last_error or ""),
                "signal_direction": str(self.state.signal_direction or "CASH"),
                "prob_long": float(self.state.prob_long),
                "prob_short": float(self.state.prob_short),
                "prob_cash": float(self.state.prob_cash),
                "last_action": str(self.state.last_action or ""),
                "last_event": str(self.state.last_event or ""),
                "active_profile": str(config.ACTIVE_PROFILE),
                "position": {
                    "side": pos.side if pos else "FLAT",
                    "entry_price": float(pos.entry_price) if pos else None,
                    "tp_price": float(pos.take_profit_price) if pos else None,
                    "sl_price": float(pos.stop_loss_price) if pos else None,
                },
                "box": self._active_box.as_dict() if self._active_box else {},
                "risk": {
                    "manual_resume_required": risk_snap.manual_resume_required,
                    "halted": risk_snap.halted,
                    "halt_reason": risk_snap.halt_reason,
                    "consecutive_wins": risk_snap.consecutive_wins,
                    "consecutive_losses": risk_snap.consecutive_losses,
                },
            }
        try:
            bot_runtime.write_runtime_snapshot(payload)
        except Exception as exc:  # pragma: no cover - disk issues
            logger.warning("Runtime snapshot write failed: %s", exc)

    def _cash_reason(self, signal: dict) -> str:
        """Build a verbose explanation for staying in CASH."""
        p_long = signal["prob_long"] * 100.0
        p_short = signal["prob_short"] * 100.0
        long_thr = self._long_threshold * 100.0
        short_thr = self._short_threshold * 100.0
        trend = signal["trend"]

        # Trend-blocked cases take priority in the explanation.
        if config.USE_TREND_FILTER and trend <= 0 and p_long > long_thr:
            return (
                f"LONG probability {p_long:.1f}% cleared the {long_thr:.1f}% "
                f"threshold, but price is BELOW its EMA200 trend "
                f"({trend * 100:.2f}%). Long entries are blocked in a downtrend. "
                f"Sitting safely in CASH."
            )
        if config.USE_TREND_FILTER and trend >= 0 and p_short > short_thr:
            return (
                f"SHORT probability {p_short:.1f}% cleared the {short_thr:.1f}% "
                f"threshold, but price is ABOVE its EMA200 trend "
                f"({trend * 100:.2f}%). Short entries are blocked in an uptrend. "
                f"Sitting safely in CASH."
            )
        return (
            f"Skipped trade because best signal was below threshold "
            f"(LONG {p_long:.1f}% vs {long_thr:.1f}% | "
            f"SHORT {p_short:.1f}% vs {short_thr:.1f}%). Sitting safely in CASH."
        )

    def _box_cash_reason(self, box_state: box_strategy.BoxState) -> str:
        if not box_state.valid:
            return box_state.reason or "Waiting for a confirmed Darvas box."
        return (
            f"Price inside Darvas box ({box_state.bottom:,.2f} - {box_state.top:,.2f}). "
            "Staying FLAT until breakout close."
        )

    def _box_brackets(
        self,
        direction: str,
        entry_price: float,
        box_state: box_strategy.BoxState,
    ) -> tuple[float, float]:
        atr = max(box_state.atr14, entry_price * 0.001)
        sl_distance = 1.5 * atr
        if direction == "LONG":
            sl = entry_price - sl_distance
            tp = entry_price + (2.0 * sl_distance)
        else:
            sl = entry_price + sl_distance
            tp = entry_price - (2.0 * sl_distance)
        return tp, sl

    def _trail_stop_from_box(self, pos: Position, box_state: box_strategy.BoxState) -> None:
        if not box_state.valid:
            return
        if pos.side == "LONG":
            candidate = box_state.bottom * (1.0 - config.BOX_STOP_BUFFER_PCT)
            if candidate > pos.stop_loss_price:
                logger.info(
                    "Darvas trailing stop LONG: %.2f -> %.2f (new box bottom %.2f)",
                    pos.stop_loss_price,
                    candidate,
                    box_state.bottom,
                )
                pos.stop_loss_price = candidate
                pos.trail_active = True
        elif pos.side == "SHORT":
            candidate = box_state.top * (1.0 + config.BOX_STOP_BUFFER_PCT)
            if candidate < pos.stop_loss_price:
                logger.info(
                    "Darvas trailing stop SHORT: %.2f -> %.2f (new box top %.2f)",
                    pos.stop_loss_price,
                    candidate,
                    box_state.top,
                )
                pos.stop_loss_price = candidate
                pos.trail_active = True

    def _write_boot_status(self) -> None:
        """Write an immediate heartbeat so the dashboard sees the engine is alive."""
        boot_price = self.state.last_price
        if boot_price <= 0:
            try:
                candles = data_pipeline.fetch_latest_candles()
                if not candles.empty:
                    boot_price = float(candles["Close"].iloc[-1])
            except Exception as exc:  # pragma: no cover - network dependent
                logger.warning(
                    "Boot price fetch failed: %s",
                    config.sanitize_for_log(str(exc)),
                )
                boot_price = 0.0

        usdt_balance = self._get_usdt_balance()
        with self.state._lock:
            if boot_price > 0:
                self.state.last_price = boot_price
            self.state.usdt_balance = usdt_balance
            self.state.last_action = "HOLD"
            self.state.last_event = "BOOT"
            self.state.last_reason = (
                f"Engine booted — scanning every {config.LOOP_SLEEP_SECONDS}s."
            )
        self._log_status(boot_price)
        self._write_runtime_snapshot()

    def _log_iteration_error(self, exc: str, *, degraded: bool = False) -> None:
        """Persist a visible row when an iteration throws (loop stays alive)."""
        if degraded:
            with self._api_state_lock:
                self.state.connection_degraded = True
                self.state.connection_error = exc
        with self.state._lock:
            self.state.last_action = "ERROR"
            self.state.last_event = "WARNING"
            self.state.last_reason = f"Iteration failed: {exc}"
        self._log_status(self.state.last_price or 0.0)

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        """Return True for network / exchange-busy errors worth a short backoff."""
        msg = str(exc).lower()
        markers = (
            "timeout",
            "timed out",
            "connection",
            "network",
            "busy",
            "rate limit",
            "too many requests",
            "503",
            "502",
            "504",
            "exchange",
            "remote end closed",
            "ssl",
            "temporarily unavailable",
        )
        return any(marker in msg for marker in markers)

    def _run(self) -> None:
        self._write_runtime_snapshot()
        try:
            try:
                self._connect()
                self._load_model()
            except Exception as exc:
                self.state.last_error = str(exc)
                self.state.running = False
                reason = (
                    f"Engine startup FAILED: {exc}. Check API credentials, network, "
                    f"and that the model artifact exists."
                )
                self._set_reason("WARNING", "STARTUP_FAILED", reason)
                self._log_status(self.state.last_price or 0.0)
                logger.error(reason)
                self._instance_lock.release()
                return

            self.state.running = True
            self._session_started_at = datetime.now(timezone.utc)
            start_equity = self._get_total_wallet_balance()
            self.risk.begin_session(start_equity)
            if self.risk.kill_switch_file_active():
                msg = (
                    f"Kill switch file present ({config.KILL_SWITCH_FILE}). "
                    "Remove it or call clear_kill_switch() before trading."
                )
                self.state.last_error = msg
                self.state.running = False
                logger.error(msg)
                self._instance_lock.release()
                return
            self._session_armed = True
            _write_active_session_marker(self)
            _register_process_shutdown_hook(self)
            if config.is_darvas_box_profile():
                self._resolve_darvas_daily_bounds()
            logger.info(
                "Trading loop started (interval=%ss, session=%s).",
                config.LOOP_SLEEP_SECONDS,
                self.session_id,
            )
            self._write_boot_status()
            if self._radio_tower is not None:
                self._radio_tower.start()
            while not self._stop_event.is_set():
                try:
                    self._iteration()
                except Exception as exc:  # keep the loop alive on transient errors
                    safe = config.sanitize_for_log(str(exc))
                    self.state.last_error = safe
                    logger.error("Iteration error: %s", safe)
                    if order_execution.is_reduce_only_reject(exc):
                        self._handle_reduce_only_rejection(
                            exc, float(self.state.last_price or 0.0)
                        )
                        self._log_iteration_error(safe, degraded=False)
                    else:
                        transient = self._is_transient_error(exc)
                        if transient:
                            self._mark_api_failure(exc, "Iteration")
                        self._log_iteration_error(safe, degraded=transient)
                        send_alert(
                            "ERROR",
                            "Iteration error",
                            safe,
                            key="iteration_error",
                        )
                        if transient:
                            self._stop_event.wait(config.EXCHANGE_RETRY_BASE_DELAY)
                self._stop_event.wait(config.LOOP_SLEEP_SECONDS)
        finally:
            if self._radio_tower is not None:
                self._radio_tower.stop()
            self.state.running = False
            try:
                import bot_runtime

                self._write_runtime_snapshot()
                bot_runtime.clear_runtime_snapshot()
            except Exception:
                pass
            if self._session_armed:
                logger.info("Trading loop exited — finalizing session export.")
                self._finalize_session_shutdown(
                    shutdown_ts=infer_session_shutdown_ts(
                        self.store,
                        self.session_id or "",
                    )
                )
                self._session_armed = False
            else:
                logger.info("Trading loop stopped.")

    # ------------------------------------------------------------------ #
    # Public control API                                                 #
    # ------------------------------------------------------------------ #
    def _finalize_session_shutdown(
        self,
        shutdown_ts: Optional[datetime] = None,
    ) -> Optional[SessionReport]:
        """Write session markdown + CSV once for the current session."""
        if self.session_id is None or self._session_started_at is None:
            return None
        end_ts = shutdown_ts or infer_session_shutdown_ts(
            self.store,
            self.session_id,
        )
        report = generate_session_summary_report(self, shutdown_ts=end_ts)
        if report is not None:
            _mark_session_exported(self.session_id)
            _clear_active_session_marker()
        return report

    def start(self) -> bool:
        """Start the engine. Returns False if another instance holds the lock."""
        global _REPORT_ALREADY_WRITTEN
        if self._thread and self._thread.is_alive():
            logger.info("Bot already running.")
            return True
        recover_orphan_session_export(self.store)
        if not self._instance_lock.acquire():
            msg = (
                "Another bot engine instance is already running on this machine "
                "(instance lock held). Refusing to start a second engine on the "
                "same account."
            )
            self.state.last_error = msg
            logger.error(msg)
            return False
        if self.risk.kill_switch_file_active():
            msg = (
                f"Kill switch is ACTIVE ({config.KILL_SWITCH_FILE}). "
                "Clear it before booting the engine."
            )
            self.state.last_error = msg
            self._instance_lock.release()
            logger.error(msg)
            return False
        cfg_errors = config.validate_execution_config()
        if cfg_errors:
            msg = "; ".join(cfg_errors)
            self.state.last_error = msg
            self._instance_lock.release()
            logger.error("Refusing to boot: %s", msg)
            return False
        _REPORT_ALREADY_WRITTEN = False
        self._last_session_report = None
        self.session_id = uuid.uuid4().hex[:12]
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="bot-loop")
        self._thread.start()
        return True

    def stop(self) -> Optional[SessionReport]:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=config.LOOP_SLEEP_SECONDS + 5)
        self._instance_lock.release()
        return self._finalize_session_shutdown()


def _register_signal_handlers() -> None:
    """Ensure SIGINT / SIGTERM route through the same clean shutdown path."""

    def _handler(signum: int, _frame) -> None:  # pragma: no cover - signal path
        sig_name = signal.Signals(signum).name
        logger.info("%s received; initiating clean shutdown ...", sig_name)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


def main() -> None:
    if not config.credentials_present():
        logger.warning(
            "API credentials look like placeholders. Set API_KEY/SECRET_KEY "
            "in your environment or .env before live execution."
        )
    bot = TradingBot()
    _register_signal_handlers()
    if not bot.start():
        raise SystemExit(1)
    try:
        while bot._thread and bot._thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Interrupt received; shutting down ...")
    finally:
        bot.stop()


if __name__ == "__main__":
    main()
