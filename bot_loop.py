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
"""

from __future__ import annotations

import fcntl
import os
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import config
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

_REPORT_LOCK = threading.Lock()
_REPORT_ALREADY_WRITTEN = False


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

    def __init__(self, store: Optional[TradeStore] = None) -> None:
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
        self._last_known_balance: float = 0.0
        self._last_known_total_wallet: float = 0.0
        self._last_close_at: Optional[datetime] = None
        self._bracket_closed_this_iteration: bool = False

    # ------------------------------------------------------------------ #
    # Setup helpers                                                      #
    # ------------------------------------------------------------------ #
    def _mark_api_success(self) -> None:
        self._api_failures = 0
        with self.state._lock:
            self.state.connection_degraded = False
            self.state.connection_error = ""

    def _mark_api_failure(self, exc: Exception, context: str) -> None:
        self._api_failures += 1
        safe = config.sanitize_for_log(str(exc))
        with self.state._lock:
            self.state.connection_degraded = True
            self.state.connection_error = f"{context}: {safe}"
        if self._api_failures == config.EXCHANGE_DEGRADED_THRESHOLD:
            send_alert(
                "WARNING",
                "API connection degraded",
                self.state.connection_error,
                key="api_degraded",
            )
        if self._api_failures >= config.EXCHANGE_RECONNECT_THRESHOLD:
            self._reconnect_client()

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
            with self.state._lock:
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
            self._client.futures_change_leverage(
                symbol=config.SYMBOL, leverage=config.LEVERAGE
            )
            logger.info("Set leverage to %dx on %s.", config.LEVERAGE, config.SYMBOL)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Could not set leverage: %s", exc)

        # Margin type (ISOLATED / CROSSED). Binance errors if already set; ignore.
        try:
            self._client.futures_change_margin_type(
                symbol=config.SYMBOL, marginType=config.MARGIN_TYPE
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
        if pos.side == "LONG":
            return (price - pos.entry_price) * pos.quantity
        return (pos.entry_price - price) * pos.quantity

    def _apply_risk_decision(self, decision: RiskDecision, price: float) -> None:
        """Flatten (if required), log halt reason, optionally stop the loop."""
        if decision.flatten_positions and self.state.position is not None:
            self._close_position(price, reason_code="MANUAL")
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
            self.risk.state.kill_switch_active or "SESSION LOSS LIMIT" in decision.reason
        ):
            self._stop_event.set()

    def activate_kill_switch(self, reason: str = "Kill switch activated by operator") -> None:
        """Emergency halt: flag file + flatten + stop the loop."""
        self.risk.trigger_kill_switch(reason)
        send_alert("CRITICAL", "Kill switch", reason, key="kill_switch")
        price = self.state.last_price
        if self.state.position is not None and price > 0:
            self._close_position(price, reason_code="MANUAL")
        self._stop_event.set()

    def confirm_risk_resume(self) -> str:
        """Clear consecutive-loss pause after operator confirmation."""
        decision = self.risk.confirm_manual_resume()
        return decision.reason

    def clear_kill_switch(self) -> None:
        """Remove kill-switch file after operator review."""
        self.risk.clear_kill_switch()

    def _round_step(self, quantity: float) -> float:
        step = self._step_size
        if step <= 0:
            return round(quantity, self._qty_precision)
        floored = int(quantity / step) * step
        return float(round(floored, self._qty_precision))

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
        """Log every scan; highlight threshold-clearing predictions."""
        p_long = signal["prob_long"] * 100.0
        p_short = signal["prob_short"] * 100.0
        long_thr = self._long_threshold * 100.0
        short_thr = self._short_threshold * 100.0
        long_ready = signal["prob_long"] > self._long_threshold
        short_ready = signal["prob_short"] > self._short_threshold
        level = logger.info if (long_ready or short_ready) else logger.debug
        level(
            "SIGNAL SCAN | LONG %.2f%% (thr %.2f%% %s) | SHORT %.2f%% (thr %.2f%% %s) "
            "| decision=%s | CASH %.2f%%",
            p_long,
            long_thr,
            "READY" if long_ready else "below",
            p_short,
            short_thr,
            "READY" if short_ready else "below",
            direction,
            signal["prob_cash"] * 100.0,
        )

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

    def _open_position(
        self,
        direction: str,
        price: float,
        probability: float,
        atr_pct: float,
        *,
        candles=None,
        book: Optional[order_execution.BookTicker] = None,
    ) -> None:
        """Open a LONG or SHORT with volatility-aware margin from the risk engine."""
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

        cooldown_sleep = 0.5 if config.is_compound_profile() else 1.5
        time.sleep(cooldown_sleep)
        total_wallet = self._get_total_wallet_balance()
        available = self._get_usdt_balance()
        sizing = self._compute_order_margin(total_wallet, atr_pct)
        order_usdt_value = sizing.margin_usdt
        notional = sizing.notional_usdt
        side = "BUY" if direction == "LONG" else "SELL"
        effective_alloc = sizing.base_pct * sizing.vol_scale * 100.0

        # --- Blocked: insufficient free margin for allocated slice -------- #
        if available < order_usdt_value:
            reason = (
                f"XGBoost triggered a valid {side} signal "
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
                f"XGBoost triggered a valid {side} signal "
                f"({probability * 100:.1f}% probability), but the order was "
                f"BLOCKED because notional ${notional:,.2f} is below the "
                f"exchange minimum ${self._min_notional:,.2f}."
            )
            self._set_reason("WARNING", f"BLOCKED_{direction}", reason)
            logger.warning(reason)
            return

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

        tp, sl = bracket_prices(direction, fill_price, atr_pct)
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
            f"XGBoost triggered {side} signal ({probability * 100:.1f}% "
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
                entry_time=self._now_iso(),
                entry_candle_ts=entry_candle_ts,
                take_profit_price=tp,
                stop_loss_price=sl,
                best_price=fill_price,
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

    def _close_position(
        self,
        price: float,
        reason_code: str,
        *,
        book: Optional[order_execution.BookTicker] = None,
    ) -> None:
        """Close the open position with a reduce-only post-only limit order.

        ``reason_code`` is one of ``TP``, ``SL``, ``TIMEOUT``, ``FLIP``, or
        ``MANUAL``. The CLOSE transition is logged as its own row and the
        completed trade is written atomically to the ground-truth ``trades`` table.
        """
        pos = self.state.position
        if pos is None:
            return

        # Closing a LONG means SELL; closing a SHORT means BUY.
        side = "SELL" if pos.side == "LONG" else "BUY"
        quantity = self._round_step(pos.quantity)
        if quantity <= 0:
            quantity = pos.quantity

        if config.USE_POST_ONLY_MAKER:
            order_result = self._execute_maker_limit(
                side, quantity, reduce_only=True, book=book
            )
            if not order_result.success:
                detail = order_result.reason or "Post-only exit did not fill"
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
            fill_price = order_result.fill_price
            exit_mode = "POST-ONLY LIMIT (GTX/maker)"
        else:  # pragma: no cover - legacy path
            order = exchange_client.call_with_retry(
                self._client.futures_create_order,
                symbol=config.SYMBOL,
                side=side,
                type="MARKET",
                quantity=quantity,
                reduceOnly=True,
                label="futures_create_order",
            )
            fill_price = self._extract_fill_price(order, fallback=price)
            exit_mode = "MARKET"

        if pos.side == "LONG":
            pnl = (fill_price - pos.entry_price) * pos.quantity
            pct = (fill_price - pos.entry_price) / pos.entry_price * 100.0
        else:  # SHORT profits when price falls
            pnl = (pos.entry_price - fill_price) * pos.quantity
            pct = (pos.entry_price - fill_price) / pos.entry_price * 100.0

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
                f"close {pos.side} at ${fill_price:,.2f}."
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
                unrealized = (price - pos.entry_price) * pos.quantity
            elif pos and pos.side == "SHORT":
                unrealized = (pos.entry_price - price) * pos.quantity
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
            with self.state._lock:
                self.state.connection_degraded = True
                self.state.connection_error = msg
                self.state.last_action = "HOLD"
                self.state.last_event = "WARNING"
                self.state.last_reason = msg
            send_alert("WARNING", "Stale market data", msg, key="empty_candles")
            self._log_status(self.state.last_price or 0.0)
            return

        price = float(candles["Close"].iloc[-1])
        self.risk.update_last_good_price(price)
        unrealized = self._current_unrealized(price)

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

        enriched = compute_live_features(candles)
        atr_pct = float(enriched["atr_pct"].dropna().iloc[-1]) if not enriched["atr_pct"].dropna().empty else config.RISK_ATR_BASELINE_PCT

        signal = model_brain.predict_latest(self._model, candles)
        direction = model_brain.decide_direction(
            signal["prob_long"],
            signal["prob_short"],
            signal["trend"],
            self._long_threshold,
            self._short_threshold,
        )
        self._log_directional_signal(signal, direction)

        # Balance fetch must stay OUTSIDE state._lock — _get_usdt_balance() updates
        # connection flags via _mark_api_success(), which also acquires the lock.
        usdt_balance = self._get_usdt_balance()
        with self.state._lock:
            self.state.last_price = price
            self.state.prob_long = signal["prob_long"]
            self.state.prob_short = signal["prob_short"]
            self.state.prob_cash = signal["prob_cash"]
            self.state.signal_direction = direction
            self.state.usdt_balance = usdt_balance
            self.state.last_action = "HOLD"
            self.state.last_event = "WAIT"
            self.state.last_reason = (
                f"Scanning market — LONG {signal['prob_long'] * 100:.1f}% | "
                f"SHORT {signal['prob_short'] * 100:.1f}% | direction {direction}."
            )

        pos = self.state.position

        # --- Bracket management for any open position --------------------- #
        if pos is not None:
            bars_held = self._position_bars_held(pos, candles)
            # Track the best floating PnL seen while the position is open.
            if pos.side == "LONG":
                floating = (price - pos.entry_price) * pos.quantity
            else:
                floating = (pos.entry_price - price) * pos.quantity
            with self.state._lock:
                pos.peak_unrealized = max(pos.peak_unrealized, floating)
                trail_note = " · trail ON" if pos.trail_active else ""
                self.state.last_reason = (
                    f"Managing open {pos.side} — entry ${pos.entry_price:,.2f}, "
                    f"TP ${pos.take_profit_price:,.2f}, SL ${pos.stop_loss_price:,.2f}, "
                    f"bar {bars_held}/{config.FORWARD_WINDOW}{trail_note}."
                )
            if config.EXECUTION_AUDIT_PARITY and bars_held >= config.FORWARD_WINDOW:
                logger.warning(
                    "TIMEOUT exit triggered — %s held %d/%d bars without organic TP/SL.",
                    pos.side,
                    bars_held,
                    config.FORWARD_WINDOW,
                )
                self._close_position(price, reason_code="TIMEOUT")
                pos = None
            elif pos is not None:
                compound_strategy.update_trailing_stop(pos, price)
                if pos.side == "LONG":
                    if price >= pos.take_profit_price:
                        self._close_position(price, reason_code="TP")
                        pos = None
                    elif price <= pos.stop_loss_price:
                        self._close_position(price, reason_code="SL")
                        pos = None
                elif pos.side == "SHORT":
                    if price <= pos.take_profit_price:
                        self._close_position(price, reason_code="TP")
                        pos = None
                    elif price >= pos.stop_loss_price:
                        self._close_position(price, reason_code="SL")
                        pos = None

        # --- Directional entry / flip logic ------------------------------ #
        if self._bracket_closed_this_iteration:
            pass
        elif direction == "LONG":
            if pos is not None and pos.side == "SHORT":
                self._close_position(price, reason_code="FLIP")
                pos = None
            if pos is None:
                self._open_position(
                    "LONG", price, signal["prob_long"], atr_pct, candles=candles
                )
        elif direction == "SHORT":
            if pos is not None and pos.side == "LONG":
                self._close_position(price, reason_code="FLIP")
                pos = None
            if pos is None:
                self._open_position(
                    "SHORT", price, signal["prob_short"], atr_pct, candles=candles
                )
        else:
            # CASH: explain why no trade was taken (only when flat; if a position
            # is open the bracket messages above already describe the state).
            if pos is None:
                cash_reason = self._cash_reason(signal)
                if (
                    signal["prob_long"] > self._long_threshold
                    or signal["prob_short"] > self._short_threshold
                ):
                    logger.info("Skipped Entry [CASH]: %s", cash_reason)
                self._set_reason("CASH", "HOLD", cash_reason)

        # Heartbeat row — skipped when a transition already logged itself this
        # iteration so the terminal shows exactly one row per discrete event.
        if not self._transition_logged:
            self._log_status(price)

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

    def _log_iteration_error(self, exc: str) -> None:
        """Persist a visible row when an iteration throws (loop stays alive)."""
        with self.state._lock:
            self.state.last_action = "ERROR"
            self.state.last_event = "WARNING"
            self.state.last_reason = f"Iteration failed: {exc}"
        self._log_status(self.state.last_price or 0.0)

    def _run(self) -> None:
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
        logger.info(
            "Trading loop started (interval=%ss, session=%s).",
            config.LOOP_SLEEP_SECONDS,
            self.session_id,
        )
        self._write_boot_status()
        while not self._stop_event.is_set():
            try:
                self._iteration()
            except Exception as exc:  # keep the loop alive on transient errors
                safe = config.sanitize_for_log(str(exc))
                self.state.last_error = safe
                logger.error("Iteration error: %s", safe)
                self._log_iteration_error(safe)
                send_alert(
                    "ERROR",
                    "Iteration error",
                    safe,
                    key="iteration_error",
                )
            self._stop_event.wait(config.LOOP_SLEEP_SECONDS)

        self.state.running = False
        logger.info("Trading loop stopped.")

    # ------------------------------------------------------------------ #
    # Public control API                                                 #
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        """Start the engine. Returns False if another instance holds the lock."""
        global _REPORT_ALREADY_WRITTEN
        if self._thread and self._thread.is_alive():
            logger.info("Bot already running.")
            return True
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
        return generate_session_summary_report(self)


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
