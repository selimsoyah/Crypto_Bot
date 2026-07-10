"""
trade_store.py
==============
SQLite-backed persistence layer for the trading bot.

Why SQLite instead of the old CSV
---------------------------------
The bot thread (writer) and the Streamlit dashboard (reader) touch the log
concurrently. CSV appends have no transactional guarantees: a reader can see a
half-written row, and there is no way to write "one trade" atomically. SQLite
in WAL (write-ahead logging) mode gives us:

* atomic writes (a trade row is either fully visible or not at all),
* safe concurrent one-writer / many-readers access,
* real queries for the shutdown reporter (no log-parsing heuristics).

Two tables:

``status_log``
    One row per discrete bot state transition or heartbeat (scan, open,
    close, warning). This is the dashboard's terminal feed.

``trades``
    Ground truth: exactly one row per **completed** trade, written atomically
    at close time by the bot. The session reporter reads this table directly
    and never reconstructs trades from log rows.

A CSV export (:meth:`TradeStore.export_status_csv`) is kept for portability.
"""

from __future__ import annotations

import csv
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

import pandas as pd

import config

logger = config.configure_logging(__name__)

# Legacy column names kept for the dashboard and CSV export.
STATUS_COLUMNS = [
    "Timestamp",
    "Current_Price",
    "Prob_Long",
    "Prob_Short",
    "Prob_Cash",
    "Direction",
    "Current_Balance",
    "Open_Position",
    "Realized_PNL",
    "Unrealized_PNL",
    "Entry_Price",
    "TP_Price",
    "SL_Price",
    "Action",
    "Event",
    "Reason",
    "Session_Id",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS status_log (
    id             INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    session_id     TEXT NOT NULL DEFAULT '',
    price          REAL,
    prob_long      REAL,
    prob_short     REAL,
    prob_cash      REAL,
    direction      TEXT,
    balance        REAL,
    open_position  TEXT,
    realized_pnl   REAL,
    unrealized_pnl REAL,
    entry_price    REAL,
    tp_price       REAL,
    sl_price       REAL,
    action         TEXT,
    event          TEXT,
    reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_ts ON status_log (ts);
CREATE INDEX IF NOT EXISTS idx_status_session ON status_log (session_id);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY,
    session_id      TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_ts        TEXT NOT NULL,
    exit_ts         TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    exit_price      REAL NOT NULL,
    quantity        REAL NOT NULL,
    tp_price        REAL,
    sl_price        REAL,
    peak_unrealized REAL NOT NULL DEFAULT 0.0,
    realized_pnl    REAL NOT NULL,
    outcome         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_session ON trades (session_id);

CREATE TABLE IF NOT EXISTS external_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    action      TEXT NOT NULL,
    price       REAL NOT NULL DEFAULT 0.0,
    quantity    REAL NOT NULL DEFAULT 0.0,
    agent_name  TEXT NOT NULL DEFAULT 'Unknown Agent',
    content     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_external_signals_ts ON external_signals (timestamp);
CREATE INDEX IF NOT EXISTS idx_external_signals_symbol ON external_signals (symbol);
CREATE UNIQUE INDEX IF NOT EXISTS idx_external_signals_dedup
    ON external_signals (timestamp, symbol);
"""


@dataclass
class StatusRow:
    """One discrete bot state transition or heartbeat."""

    ts: str
    session_id: str
    price: float
    prob_long: float
    prob_short: float
    prob_cash: float
    direction: str
    balance: float
    open_position: str
    realized_pnl: float
    unrealized_pnl: float
    entry_price: Optional[float]
    tp_price: Optional[float]
    sl_price: Optional[float]
    action: str
    event: str
    reason: str


@dataclass
class TradeRecord:
    """Ground-truth record of one completed trade, written at close time."""

    session_id: str
    side: str  # LONG | SHORT
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    quantity: float
    tp_price: float
    sl_price: float
    peak_unrealized: float
    realized_pnl: float
    outcome: str  # TP | SL | FLIP | MANUAL


@dataclass
class ExternalSignal:
    """One observed operation from the public ai4trade.ai fleet (read-only)."""

    timestamp: str
    symbol: str
    action: str
    price: float
    quantity: float
    agent_name: str = "Unknown Agent"
    content: str = ""


class TradeStore:
    """Thread-safe SQLite store (WAL mode, one connection per operation)."""

    _SQLITE_TIMEOUT_SEC: float = 30.0
    _BUSY_TIMEOUT_MS: int = 30_000

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or config.DB_FILE
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=self._SQLITE_TIMEOUT_SEC)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {self._BUSY_TIMEOUT_MS}")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ #
    # Writers (bot thread)                                               #
    # ------------------------------------------------------------------ #
    def log_status(self, row: StatusRow) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO status_log
                    (ts, session_id, price, prob_long, prob_short, prob_cash,
                     direction, balance, open_position, realized_pnl,
                     unrealized_pnl, entry_price, tp_price, sl_price,
                     action, event, reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.ts, row.session_id, row.price, row.prob_long,
                    row.prob_short, row.prob_cash, row.direction, row.balance,
                    row.open_position, row.realized_pnl, row.unrealized_pnl,
                    row.entry_price, row.tp_price, row.sl_price,
                    row.action, row.event, row.reason,
                ),
            )

    def record_trade(self, trade: TradeRecord) -> None:
        """Insert one completed trade atomically."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO trades
                    (session_id, side, entry_ts, exit_ts, entry_price,
                     exit_price, quantity, tp_price, sl_price,
                     peak_unrealized, realized_pnl, outcome)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    trade.session_id, trade.side, trade.entry_ts, trade.exit_ts,
                    trade.entry_price, trade.exit_price, trade.quantity,
                    trade.tp_price, trade.sl_price, trade.peak_unrealized,
                    trade.realized_pnl, trade.outcome,
                ),
            )

    def external_signal_exists(self, timestamp: str, symbol: str) -> bool:
        """Return True when this timestamp/symbol pair is already stored."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM external_signals
                WHERE timestamp = ? AND symbol = ?
                LIMIT 1
                """,
                (timestamp, symbol),
            ).fetchone()
        return row is not None

    def insert_external_signal_if_new(self, signal: ExternalSignal) -> bool:
        """Insert one external signal unless timestamp+symbol already exists."""
        if self.external_signal_exists(signal.timestamp, signal.symbol):
            return False
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO external_signals
                    (timestamp, symbol, action, price, quantity, agent_name, content)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    signal.timestamp,
                    signal.symbol,
                    signal.action,
                    signal.price,
                    signal.quantity,
                    signal.agent_name,
                    signal.content,
                ),
            )
        return True

    def count_external_signals(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM external_signals").fetchone()
        return int(row[0]) if row else 0

    def top_external_signal_symbol(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT symbol, COUNT(*) AS cnt
                FROM external_signals
                GROUP BY symbol
                ORDER BY cnt DESC, symbol ASC
                LIMIT 1
                """
            ).fetchone()
        return str(row[0]) if row else "—"

    def external_market_bias(self, hours: float = 12.0) -> dict[str, object]:
        """Aggregate BUY/SHORT sentiment over the recent window."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT action, COUNT(*) AS cnt
                FROM external_signals
                WHERE datetime(replace(substr(timestamp, 1, 19), 'T', ' '))
                      >= datetime('now', ?)
                GROUP BY action
                """,
                (f"-{int(hours)} hours",),
            ).fetchall()

        bullish_actions = {"BUY", "COVER"}
        bearish_actions = {"SELL", "SHORT"}
        bullish = 0
        bearish = 0
        total = 0
        for action, cnt in rows:
            action_u = str(action or "").upper()
            n = int(cnt)
            total += n
            if action_u in bullish_actions:
                bullish += n
            elif action_u in bearish_actions:
                bearish += n

        if total == 0:
            label = "NEUTRAL"
        elif bullish > bearish * 1.15:
            label = "BULLISH"
        elif bearish > bullish * 1.15:
            label = "BEARISH"
        else:
            label = "NEUTRAL"

        return {
            "label": label,
            "bullish": bullish,
            "bearish": bearish,
            "total": total,
            "bullish_pct": (100.0 * bullish / total) if total else 0.0,
            "bearish_pct": (100.0 * bearish / total) if total else 0.0,
        }

    def read_external_signals_df(self, limit: int = 15) -> pd.DataFrame:
        """Return the latest external signals (newest first)."""
        limit = max(1, int(limit))
        with self._connect() as conn:
            return pd.read_sql_query(
                """
                SELECT id, timestamp, symbol, action, price, quantity,
                       agent_name, content
                FROM external_signals
                ORDER BY id DESC
                LIMIT ?
                """,
                conn,
                params=(limit,),
            )

    # ------------------------------------------------------------------ #
    # Readers (dashboard / reporter)                                     #
    # ------------------------------------------------------------------ #
    def read_status_df(
        self,
        session_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Return status rows (chronological) using the legacy column names."""
        query = """
            SELECT ts, price, prob_long, prob_short, prob_cash, direction,
                   balance, open_position, realized_pnl, unrealized_pnl,
                   entry_price, tp_price, sl_price, action, event, reason,
                   session_id
            FROM status_log
        """
        params: list = []
        if session_id:
            query += " WHERE session_id = ?"
            params.append(session_id)
        if limit:
            inner = """
                SELECT id, ts, price, prob_long, prob_short, prob_cash, direction,
                       balance, open_position, realized_pnl, unrealized_pnl,
                       entry_price, tp_price, sl_price, action, event, reason,
                       session_id
                FROM status_log
            """
            if session_id:
                inner += " WHERE session_id = ?"
            inner += " ORDER BY id DESC LIMIT ?"
            query = f"""
                SELECT ts, price, prob_long, prob_short, prob_cash, direction,
                       balance, open_position, realized_pnl, unrealized_pnl,
                       entry_price, tp_price, sl_price, action, event, reason,
                       session_id
                FROM ({inner}) AS recent
                ORDER BY recent.id
            """
            if session_id:
                params.append(int(limit))
            else:
                params = [int(limit)]
        else:
            query += " ORDER BY id"
        with self._connect() as conn:
            df = pd.read_sql_query(query, conn, params=params)
        df.columns = STATUS_COLUMNS
        if not df.empty:
            df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
        return df

    def read_trades_df(self, session_id: Optional[str] = None) -> pd.DataFrame:
        query = "SELECT * FROM trades"
        params: list = []
        if session_id:
            query += " WHERE session_id = ?"
            params.append(session_id)
        query += " ORDER BY id"
        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=params)

    def session_balance_bounds(self, session_id: str) -> tuple[float, float]:
        """Return (first, last) logged balance for a session (0.0 if none)."""
        with self._connect() as conn:
            first = conn.execute(
                "SELECT balance FROM status_log WHERE session_id=? ORDER BY id ASC LIMIT 1",
                (session_id,),
            ).fetchone()
            last = conn.execute(
                "SELECT balance FROM status_log WHERE session_id=? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        return (
            float(first[0]) if first and first[0] is not None else 0.0,
            float(last[0]) if last and last[0] is not None else 0.0,
        )

    # ------------------------------------------------------------------ #
    # Export                                                             #
    # ------------------------------------------------------------------ #
    def export_status_csv(self, path: Optional[str] = None) -> str:
        """Export the full status log to CSV for portability. Returns the path."""
        out_path = path or config.LOG_FILE
        df = self.read_status_df()
        tmp_path = f"{out_path}.tmp"
        df.to_csv(tmp_path, index=False, quoting=csv.QUOTE_MINIMAL)
        os.replace(tmp_path, out_path)
        logger.info("Exported %d status rows to %s", len(df), out_path)
        return out_path
