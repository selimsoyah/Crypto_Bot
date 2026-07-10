"""
dashboard_stats.py
==================
Pure, testable statistics helpers for the Streamlit dashboard.

Keeps metric logic out of ``dashboard.py`` so Phase 4 tests can verify the UI
does not mislead operators (stale log vs exchange, session PnL, win rate).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

import config
import exchange_client


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def reconcile_manual_exchange_close(
    *,
    store,
    log: pd.DataFrame,
    trades: pd.DataFrame,
    exchange_pos: Optional[dict] = None,
    symbol: str = config.SYMBOL,
    client=None,
) -> dict:
    """Backfill manual exchange closes into ``trades`` so stats stay accurate.

    This handles the common orphan case: status log still shows LONG/SHORT while
    the exchange is already flat because the operator manually closed in Binance.
    """
    result = {"inserted": False, "message": ""}
    if log.empty:
        return result
    if not exchange_pos or exchange_pos.get("status") != "flat":
        return result

    last = log.iloc[-1]
    side = str(last.get("Open_Position", "FLAT") or "FLAT").upper()
    if side not in ("LONG", "SHORT"):
        return result

    session_id = str(last.get("Session_Id", "") or "")
    entry_ts = str(last.get("Timestamp", "") or "")
    if not session_id or not entry_ts:
        return result

    # One close per entry. If present, reconciliation already happened.
    if not trades.empty:
        dup = trades[
            (trades["session_id"].astype(str) == session_id)
            & (trades["side"].astype(str).str.upper() == side)
            & (trades["entry_ts"].astype(str) == entry_ts)
        ]
        if not dup.empty:
            return result

    if client is None:
        try:
            client = exchange_client.build_execution_client()
        except Exception as exc:
            result["message"] = (
                "Manual close reconciliation unavailable: "
                f"{config.sanitize_for_log(str(exc))}"
            )
            return result
    try:
        fills = exchange_client.call_with_retry(
            client.futures_account_trades,
            symbol=symbol,
            limit=200,
            label="futures_account_trades",
        )
    except Exception as exc:
        result["message"] = f"Manual close reconciliation failed: {config.sanitize_for_log(str(exc))}"
        return result

    close_side = "SELL" if side == "LONG" else "BUY"
    entry_dt = pd.to_datetime(entry_ts, errors="coerce", utc=True)
    if pd.isna(entry_dt):
        return result
    entry_ms = int(entry_dt.timestamp() * 1000)
    close_fills = [
        f
        for f in fills
        if str(f.get("side", "")).upper() == close_side
        and int(_safe_float(f.get("time"), 0)) >= entry_ms
        and _safe_float(f.get("qty"), 0.0) > 0.0
    ]
    if not close_fills:
        return result

    # Pick the latest closing order and aggregate its child fills.
    latest = max(close_fills, key=lambda x: int(_safe_float(x.get("time"), 0)))
    latest_order_id = str(latest.get("orderId", ""))
    order_fills = [
        f for f in close_fills if str(f.get("orderId", "")) == latest_order_id
    ] or [latest]

    qty = sum(_safe_float(f.get("qty"), 0.0) for f in order_fills)
    if qty <= 0:
        return result
    quote = sum(
        _safe_float(f.get("price"), 0.0) * _safe_float(f.get("qty"), 0.0)
        for f in order_fills
    )
    exit_price = quote / qty if quote > 0 else _safe_float(last.get("Current_Price"), 0.0)
    realized = sum(_safe_float(f.get("realizedPnl"), 0.0) for f in order_fills)
    exit_ms = max(int(_safe_float(f.get("time"), 0)) for f in order_fills)
    exit_ts = datetime.fromtimestamp(exit_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    from trade_store import StatusRow, TradeRecord

    trade = TradeRecord(
        session_id=session_id,
        side=side,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_price=_safe_float(last.get("Entry_Price"), 0.0),
        exit_price=exit_price,
        quantity=qty,
        tp_price=_safe_float(last.get("TP_Price"), 0.0),
        sl_price=_safe_float(last.get("SL_Price"), 0.0),
        peak_unrealized=abs(_safe_float(last.get("Unrealized_PNL"), 0.0)),
        realized_pnl=realized,
        outcome="MANUAL",
    )
    store.record_trade(trade)

    # Write a matching terminal row so activity feed and state don't stay stale.
    status = StatusRow(
        ts=exit_ts,
        session_id=session_id,
        price=exit_price,
        prob_long=_safe_float(last.get("Prob_Long"), 0.0),
        prob_short=_safe_float(last.get("Prob_Short"), 0.0),
        prob_cash=_safe_float(last.get("Prob_Cash"), 0.0),
        direction=str(last.get("Direction", "CASH") or "CASH"),
        balance=_safe_float(last.get("Current_Balance"), 0.0),
        open_position="FLAT",
        realized_pnl=_safe_float(last.get("Realized_PNL"), 0.0) + realized,
        unrealized_pnl=0.0,
        entry_price=None,
        tp_price=None,
        sl_price=None,
        action=f"CLOSE_{side}_MANUAL",
        event="FILL_SUCCESS" if realized >= 0 else "STOP_LOSS",
        reason=(
            f"Manual exchange close detected ({close_side}) and reconciled into "
            f"dashboard ledger at ${exit_price:,.2f}."
        ),
    )
    store.log_status(status)
    result["inserted"] = True
    result["message"] = (
        f"Reconciled manual close: {side} {qty:.4f} @ ${exit_price:,.2f} "
        f"(PnL {realized:+.2f})."
    )
    return result


def count_profitable_wins(trades: pd.DataFrame) -> int:
    """Count wins: explicit TP **or** any close with positive realized PnL."""
    if trades.empty:
        return 0
    return int(((trades["outcome"] == "TP") | (trades["realized_pnl"] > 0)).sum())


def compute_closed_trade_stats(trades: pd.DataFrame) -> dict:
    """Net closed PnL and win-rate from the ground-truth trades ledger."""
    result = {
        "net_closed_pnl": 0.0,
        "total_closed": 0,
        "wins": 0,
        "win_rate": 0.0,
    }
    if trades.empty:
        return result
    result["total_closed"] = int(len(trades))
    result["wins"] = count_profitable_wins(trades)
    result["win_rate"] = 100.0 * result["wins"] / result["total_closed"]
    result["net_closed_pnl"] = float(trades["realized_pnl"].sum())
    return result


def compute_stats(log: pd.DataFrame, trades: pd.DataFrame) -> dict:
    """Derive top-card statistics from the status log + trades ledger."""
    stats = {
        "balance": 0.0,
        "open_status": "FLAT",
        "long_trades": 0,
        "short_trades": 0,
        "total_trades": 0,
        "win_rate": 0.0,
        "prob_long": 0.0,
        "prob_short": 0.0,
        "prob_cash": 0.0,
        "direction": "CASH",
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "allocation_label": (
            f"{config.CASH_ALLOCATION_PCT:.0%} base · vol-scaled · "
            f"{config.LEVERAGE}x"
        ),
        "data_warnings": [],
    }
    if log.empty:
        stats["data_warnings"].append(
            "No status log rows — balance/position metrics are unavailable."
        )
    else:
        last = log.iloc[-1]
        stats["balance"] = float(last.get("Current_Balance", 0.0) or 0.0)
        stats["open_status"] = str(last.get("Open_Position", "FLAT"))
        stats["prob_long"] = float(last.get("Prob_Long", 0.0) or 0.0)
        stats["prob_short"] = float(last.get("Prob_Short", 0.0) or 0.0)
        stats["prob_cash"] = float(last.get("Prob_Cash", 0.0) or 0.0)
        stats["direction"] = str(last.get("Direction", "CASH"))
        stats["unrealized_pnl"] = float(last.get("Unrealized_PNL", 0.0) or 0.0)

    if not trades.empty:
        stats["total_trades"] = int(len(trades))
        stats["long_trades"] = int((trades["side"] == "LONG").sum())
        stats["short_trades"] = int((trades["side"] == "SHORT").sum())
        stats["win_rate"] = compute_closed_trade_stats(trades)["win_rate"]
        stats["realized_pnl"] = float(trades["realized_pnl"].sum())
    return stats


def get_live_position_pnl(log: pd.DataFrame) -> dict:
    """Read floating PnL from the latest status row."""
    flat = {
        "open": False,
        "unrealized_pnl": 0.0,
        "pct_change": 0.0,
        "side": "FLAT",
        "price": 0.0,
    }
    if log.empty:
        return flat

    last = log.iloc[-1]
    side = str(last.get("Open_Position", "FLAT")).upper()
    if side not in ("LONG", "SHORT"):
        return flat

    unrealized = float(last.get("Unrealized_PNL", 0.0) or 0.0)
    price = float(last.get("Current_Price", 0.0) or 0.0)
    entry = float(last.get("Entry_Price", 0.0) or 0.0)

    if entry > 0 and price > 0:
        pct = (
            ((price - entry) / entry * 100.0)
            if side == "LONG"
            else ((entry - price) / entry * 100.0)
        )
    else:
        pct = 0.0

    return {
        "open": True,
        "unrealized_pnl": unrealized,
        "pct_change": pct,
        "side": side,
        "price": price,
    }


def _parse_position_row(row: dict) -> dict:
    amt = float(row.get("positionAmt", 0.0))
    entry = float(row.get("entryPrice", 0.0))
    mark = float(row.get("markPrice", entry))
    unrealized = float(row.get("unRealizedProfit", row.get("unrealizedProfit", 0.0)))
    leverage = int(float(row.get("leverage", config.LEVERAGE)))
    notional = abs(amt) * mark
    margin = notional / leverage if leverage > 0 else notional
    side = "LONG" if amt > 0 else "SHORT"
    if side == "LONG":
        pct = ((mark - entry) / entry * 100.0) if entry else 0.0
    else:
        pct = ((entry - mark) / entry * 100.0) if entry else 0.0
    return {
        "status": "ok",
        "side": side,
        "quantity": abs(amt),
        "entry_price": entry,
        "mark_price": mark,
        "unrealized_pnl": unrealized,
        "pct_change": pct,
        "leverage": leverage,
        "notional": notional,
        "margin": margin,
    }


def fetch_live_position(
    symbol: str = config.SYMBOL,
    client=None,
) -> dict:
    """Pull active futures position; never conflate API error with flat."""
    if not config.credentials_present():
        return {"status": "error", "message": "API credentials not configured."}
    if client is None:
        try:
            client = exchange_client.build_execution_client()
        except Exception as exc:
            return {
                "status": "error",
                "message": config.sanitize_for_log(str(exc)),
            }
    try:
        rows = exchange_client.call_with_retry(
            client.futures_position_information,
            symbol=symbol,
            label="futures_position_information",
        )
        for row in rows:
            amt = float(row.get("positionAmt", 0.0))
            if abs(amt) > 1e-12:
                return _parse_position_row(row)
    except Exception as exc:
        return {
            "status": "error",
            "message": config.sanitize_for_log(str(exc)),
        }
    return {"status": "flat"}


def reconcile_floating_pnl(
    log: pd.DataFrame,
    exchange_pos: Optional[dict] = None,
) -> dict:
    """Choose the honest floating-PnL source for the live PnL strip."""
    if exchange_pos and exchange_pos.get("status") == "ok":
        return {
            "open": True,
            "unrealized_pnl": exchange_pos["unrealized_pnl"],
            "pct_change": exchange_pos["pct_change"],
            "side": exchange_pos["side"],
            "price": exchange_pos["mark_price"],
            "source": "exchange",
            "warning": "",
        }

    log_pnl = get_live_position_pnl(log)

    if exchange_pos and exchange_pos.get("status") == "error":
        if log_pnl["open"]:
            return {
                **log_pnl,
                "source": "log_stale",
                "warning": exchange_pos.get("message", "Exchange query failed."),
            }
        return {
            "open": False,
            "unrealized_pnl": 0.0,
            "pct_change": 0.0,
            "side": "FLAT",
            "price": 0.0,
            "source": "unknown",
            "warning": exchange_pos.get("message", "Exchange query failed."),
        }

    if log_pnl["open"]:
        return {**log_pnl, "source": "log", "warning": ""}
    return {**log_pnl, "source": "flat", "warning": ""}


def compute_session_risk_pnl(bot) -> tuple[float, float, float]:
    """Return (session_pnl_pct, realized, unrealized) matching the bot loop."""
    realized = bot.state.realized_pnl
    unrealized = 0.0
    pos = bot.state.position
    price = bot.state.last_price
    if pos is not None and price > 0:
        if pos.side == "LONG":
            unrealized = (price - pos.entry_price) * pos.quantity
        else:
            unrealized = (pos.entry_price - price) * pos.quantity
    pnl_pct = bot.risk.session_pnl_pct(realized, unrealized)
    return pnl_pct, realized, unrealized


def allocation_label_from_risk(risk_snap) -> str:
    """Human-readable sizing label from the risk engine snapshot."""
    scale = getattr(risk_snap, "last_vol_scale", 1.0)
    effective = config.CASH_ALLOCATION_PCT * scale * 100.0
    return (
        f"{effective:.0f}% vol-scaled margin · "
        f"{config.CASH_ALLOCATION_PCT:.0%} base · {config.LEVERAGE}x"
    )


def _parse_ts(ts: str) -> Optional[pd.Timestamp]:
    if not ts or (isinstance(ts, float) and pd.isna(ts)):
        return None
    try:
        return pd.to_datetime(ts, errors="coerce")
    except (TypeError, ValueError):
        return None


_ACTIVITY_ACTION_PREFIXES = (
    "OPEN_",
    "CLOSE_",
    "SKIPPED_",
    "BLOCKED_",
    "EXIT_PENDING_",
    "RISK_",
)
_ACTIVITY_EVENTS = frozenset(
    {
        "WARNING",
        "BUY_LONG",
        "SHORT_ORDER",
        "STOP_LOSS",
        "FILL_SUCCESS",
        "BOOT",
        "SCAN",
    }
)
_SCAN_ACTIONS = frozenset({"SCAN"})


def _is_activity_row(action: str, event: str) -> bool:
    action = action or ""
    event = event or ""
    if action in _SCAN_ACTIONS or event in _SCAN_ACTIONS:
        return True
    if any(action.startswith(prefix) for prefix in _ACTIVITY_ACTION_PREFIXES):
        return True
    return event in _ACTIVITY_EVENTS


def status_to_activity_rows(log: pd.DataFrame, max_rows: int = 60) -> list[dict]:
    """Convert status-log rows into operator-facing activity (includes scan heartbeats)."""
    if log.empty:
        return []

    rows: list[dict] = []
    for _, row in log.iterrows():
        raw_action = str(row.get("Action", "") or "")
        event = str(row.get("Event", "") or "")
        if not _is_activity_row(raw_action, event):
            continue

        action = raw_action
        if "RADAR" in raw_action:
            action = raw_action.replace("BLOCKED_", "🛡️ ")

        ts = _parse_ts(str(row.get("Timestamp", "")))
        time_str = ts.strftime("%H:%M:%S") if ts is not None and pd.notna(ts) else "--:--:--"
        reason = str(row.get("Reason", "") or "")
        if len(reason) > 120:
            reason = reason[:117] + "..."

        tone = "info"
        if raw_action == "SCAN" or event == "SCAN":
            tone = "scan"
        elif "RADAR" in raw_action:
            tone = "radar"
        elif raw_action.startswith(("SKIPPED_", "BLOCKED_", "EXIT_PENDING_")) or event == "WARNING":
            tone = "warn"
        elif raw_action.startswith("OPEN_") or event in ("BUY_LONG", "SHORT_ORDER"):
            tone = "open"
        elif raw_action.startswith("CLOSE_") or event in ("STOP_LOSS", "FILL_SUCCESS"):
            tone = "close"

        rows.append(
            {
                "time": time_str,
                "action": action,
                "event": event,
                "position": str(row.get("Open_Position", "FLAT") or "FLAT"),
                "reason": reason,
                "tone": tone,
            }
        )

    if max_rows and len(rows) > max_rows:
        rows = rows[-max_rows:]
    return rows


def open_position_row(exchange_pos: Optional[dict]) -> Optional[dict]:
    """Build a synthetic open-position row when the exchange still holds size."""
    if not exchange_pos or exchange_pos.get("status") != "ok":
        return None
    pnl = float(exchange_pos.get("unrealized_pnl", 0.0) or 0.0)
    side = str(exchange_pos.get("side", "FLAT"))
    return {
        "time": "LIVE",
        "side": "Up" if side == "LONG" else "Down",
        "entry": float(exchange_pos.get("entry_price", 0.0) or 0.0),
        "exit": float(exchange_pos.get("mark_price", 0.0) or 0.0),
        "sh": "—",
        "status": "OPEN",
        "pnl": pnl,
        "won": pnl >= 0,
        "open": True,
    }


def position_mismatch_warning(
    log: pd.DataFrame,
    exchange_pos: Optional[dict],
) -> str:
    """Warn when exchange position disagrees with the latest status-log row."""
    if log.empty or not exchange_pos:
        return ""
    if exchange_pos.get("status") != "ok":
        return ""
    log_side = str(log.iloc[-1].get("Open_Position", "FLAT") or "FLAT").upper()
    ex_side = str(exchange_pos.get("side", "FLAT") or "FLAT").upper()
    if log_side == "FLAT" and ex_side in ("LONG", "SHORT"):
        return (
            f"Exchange reports an open {ex_side} position, but the status log shows FLAT. "
            "Open PnL is from the exchange; boot the engine or flatten manually."
        )
    if log_side in ("LONG", "SHORT") and exchange_pos.get("status") == "flat":
        return (
            f"Status log shows {log_side}, but the exchange reports flat. "
            "Metrics may be stale until the next scan."
        )
    return ""


def trades_to_log_rows(trades: pd.DataFrame) -> list[dict]:
    """Convert ground-truth trades into terminal log row dicts."""
    if trades.empty:
        return []

    rows: list[dict] = []
    for _, t in trades.iterrows():
        pnl = float(t.get("realized_pnl", 0.0) or 0.0)
        side = str(t.get("side", "LONG")).upper()
        entry_ts = _parse_ts(str(t.get("entry_ts", "")))
        exit_ts = _parse_ts(str(t.get("exit_ts", "")))
        entry_price = float(t.get("entry_price", 0.0) or 0.0)

        exit_price = float(t.get("exit_price", 0.0) or 0.0)

        time_str = exit_ts.strftime("%H:%M:%S") if exit_ts is not None and pd.notna(exit_ts) else "--:--:--"
        market_str = entry_ts.strftime("%H:%M") if entry_ts is not None and pd.notna(entry_ts) else "--:--"

        if entry_ts is not None and exit_ts is not None and pd.notna(entry_ts) and pd.notna(exit_ts):
            hold_min = max(1, int((exit_ts - entry_ts).total_seconds() // 60))
        else:
            hold_min = 0

        rows.append(
            {
                "time": time_str,
                "market": market_str,
                "side": "Up" if side == "LONG" else "Down",
                "entry": entry_price,
                "exit": exit_price,
                "sh": hold_min,
                "status": "WON" if pnl > 0 else "LOST",
                "pnl": pnl,
                "won": pnl > 0,
            }
        )
    return rows


def essential_metrics(
    trades: pd.DataFrame,
    log: pd.DataFrame,
    exchange_pos: Optional[dict] = None,
    bot=None,
    runtime_snapshot: Optional[dict] = None,
) -> dict:
    """Minimal headline stats for the redesigned dashboard."""
    closed = compute_closed_trade_stats(trades)
    live = reconcile_floating_pnl(log, exchange_pos)
    compound = compute_compound_metrics(trades, bot=bot)
    open_side = "FLAT"
    open_pnl = 0.0
    if live.get("open"):
        open_side = str(live.get("side", "FLAT"))
        open_pnl = float(live.get("unrealized_pnl", 0.0))

    wallet = 0.0
    if bot is not None and bot.state.running and bot.state.usdt_balance > 0:
        wallet = float(bot.state.usdt_balance)
    elif runtime_snapshot and float(runtime_snapshot.get("usdt_balance", 0.0) or 0.0) > 0:
        wallet = float(runtime_snapshot.get("usdt_balance", 0.0))
    elif not log.empty:
        wallet = float(log.iloc[-1].get("Current_Balance", 0.0) or 0.0)

    direction = "CASH"
    if not log.empty:
        direction = str(log.iloc[-1].get("Direction", "CASH"))

    return {
        "wallet_balance": wallet,
        "net_pnl": closed["net_closed_pnl"],
        "open_pnl": open_pnl,
        "open_side": open_side,
        "direction": direction,
        "win_rate": closed["win_rate"],
        "total_trades": closed["total_closed"],
        "wins": closed["wins"],
        "losses": max(0, closed["total_closed"] - closed["wins"]),
        **compound,
    }


def compute_compound_metrics(
    trades: pd.DataFrame,
    bot=None,
    runtime_snapshot: Optional[dict] = None,
    lookback_days: int = 7,
) -> dict:
    """Path B compounding stats — expectancy, weekly activity, streak sizing."""
    empty = {
        "trades_7d": 0,
        "pnl_7d": 0.0,
        "expectancy": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "size_mult": 1.0,
        "consecutive_wins": 0,
        "consecutive_losses": 0,
        "profile": config.TRADING_PROFILE,
    }
    if trades.empty and bot is None:
        return empty

    now = pd.Timestamp.now(tz="UTC")
    cutoff = now - pd.Timedelta(days=lookback_days)
    recent = trades.copy()
    if not recent.empty and "exit_ts" in recent.columns:
        recent["_exit"] = pd.to_datetime(recent["exit_ts"], errors="coerce", utc=True)
        recent = recent[recent["_exit"] >= cutoff]

    pnl_7d = float(recent["realized_pnl"].sum()) if not recent.empty else 0.0
    trades_7d = int(len(recent))

    wins = recent[recent["realized_pnl"] > 0]["realized_pnl"] if not recent.empty else pd.Series(dtype=float)
    losses = recent[recent["realized_pnl"] <= 0]["realized_pnl"] if not recent.empty else pd.Series(dtype=float)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    win_rate = len(wins) / trades_7d if trades_7d else 0.0
    loss_rate = 1.0 - win_rate if trades_7d else 0.0
    expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)

    size_mult = 1.0
    consecutive_wins = 0
    consecutive_losses = 0
    risk = {}
    if isinstance(runtime_snapshot, dict):
        risk = runtime_snapshot.get("risk", {}) if isinstance(runtime_snapshot.get("risk"), dict) else {}
    if risk:
        consecutive_wins = int(risk.get("consecutive_wins", 0) or 0)
        consecutive_losses = int(risk.get("consecutive_losses", 0) or 0)
    elif bot is not None:
        snap = bot.risk.snapshot()
        consecutive_wins = snap.consecutive_wins
        consecutive_losses = snap.consecutive_losses
    if consecutive_wins or consecutive_losses or bot is not None:
        from compound_strategy import compound_size_multiplier

        size_mult = compound_size_multiplier(consecutive_wins, consecutive_losses)

    return {
        "trades_7d": trades_7d,
        "pnl_7d": pnl_7d,
        "expectancy": expectancy,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "size_mult": size_mult,
        "consecutive_wins": consecutive_wins,
        "consecutive_losses": consecutive_losses,
        "profile": config.TRADING_PROFILE,
    }


def equity_curve_points(trades: pd.DataFrame) -> list[dict]:
    """Cumulative closed PnL points for a simple equity chart."""
    if trades.empty:
        return []
    df = trades.sort_values("exit_ts").copy()
    df["_exit"] = pd.to_datetime(df["exit_ts"], errors="coerce")
    cumulative = df["realized_pnl"].cumsum()
    points = []
    for ts, pnl, cum in zip(df["_exit"], df["realized_pnl"], cumulative):
        if pd.isna(ts):
            continue
        points.append(
            {
                "time": ts.strftime("%m-%d %H:%M"),
                "trade_pnl": float(pnl),
                "equity_pnl": float(cum),
            }
        )
    return points


def threshold_distance(
    prob_long: float,
    prob_short: float,
    long_thr: float,
    short_thr: float,
) -> dict:
    """How far live probabilities are from firing (dashboard helper)."""
    from compound_strategy import threshold_distance as _dist

    return _dist(prob_long, prob_short, long_thr, short_thr)


def bot_health(
    bot,
    log: pd.DataFrame,
    exchange_pos: Optional[dict] = None,
    runtime: Optional[dict] = None,
) -> dict:
    """Heartbeat snapshot — is the engine alive and scanning?"""
    import bot_runtime

    runtime = runtime or bot_runtime.engine_runtime_status(log)
    running = bool(runtime.get("running"))
    degraded = bool(runtime.get("degraded"))
    if not running and bot is not None and getattr(bot.state, "running", False):
        running = True
        degraded = bool(getattr(bot.state, "connection_degraded", False))
        if degraded:
            runtime = dict(runtime)
            runtime["connection_error"] = str(getattr(bot.state, "connection_error", "") or "")
    snapshot = runtime.get("snapshot", {}) or {}
    last_action = "—"
    last_event = "—"
    last_scan_str = "never"
    seconds_ago: Optional[float] = runtime.get("heartbeat_age")
    stale = bool(runtime.get("stale", True))
    open_position = "FLAT"
    position_source = "log"

    if not log.empty:
        last = log.iloc[-1]
        last_action = str(last.get("Action", "—") or "—")
        last_event = str(last.get("Event", "—") or "—")
        open_position = str(last.get("Open_Position", "FLAT") or "FLAT")
        ts_raw = last.get("Timestamp")
        last_scan = pd.to_datetime(ts_raw, errors="coerce")
        if pd.notna(last_scan):
            if last_scan.tzinfo is None:
                last_scan = last_scan.tz_localize("UTC")
            if seconds_ago is None:
                now = pd.Timestamp.now(tz="UTC")
                seconds_ago = max(0.0, (now - last_scan).total_seconds())
            last_scan_str = last_scan.strftime("%H:%M:%S UTC")

    pos = snapshot.get("position", {}) if isinstance(snapshot.get("position"), dict) else {}
    if running and pos.get("side") not in (None, "", "FLAT"):
        open_position = str(pos.get("side", open_position))

    if exchange_pos and exchange_pos.get("status") == "ok":
        open_position = str(exchange_pos.get("side", open_position))
        position_source = "exchange"

    mode = str(runtime.get("mode", "offline"))
    pid = int(runtime.get("pid", 0) or 0)
    if not running:
        if runtime.get("booting"):
            status = "BOOTING"
            detail = "Engine service is starting — waiting for first scan."
        elif runtime.get("process_alive") and stale:
            status = "STALE"
            detail = "Engine process is alive but heartbeat is stale — check logs."
        else:
            status = "OFFLINE"
            detail = "Boot the engine from the sidebar to start scanning."
    elif degraded:
        status = "DEGRADED"
        detail = runtime.get("connection_error") or "Exchange API issues — entries paused."
    elif running and log.empty:
        status = "BOOTING"
        detail = "Engine started — waiting for first scan to complete."
        stale = False
    elif stale:
        status = "STALE"
        if seconds_ago is not None:
            detail = f"No heartbeat in {int(seconds_ago)}s — check engine logs."
        else:
            detail = "No status log rows yet — check bot thread."
    else:
        status = "LIVE"
        mode_label = "systemd" if mode == "systemd" else "headless"
        detail = (
            f"{mode_label} engine pid {pid} · scanning every {config.LOOP_SLEEP_SECONDS}s · "
            f"last action {last_action}"
        )

    mismatch = position_mismatch_warning(log, exchange_pos)
    if mismatch and status in ("OFFLINE", "STALE"):
        detail = f"{detail} {mismatch}"

    return {
        "status": status,
        "running": running,
        "degraded": degraded,
        "stale": stale,
        "seconds_ago": seconds_ago,
        "last_scan": last_scan_str,
        "last_action": last_action,
        "last_event": last_event,
        "open_position": open_position,
        "position_source": position_source,
        "detail": detail,
        "mode": mode,
        "pid": pid,
    }


def darvas_box_stats(
    candles: pd.DataFrame | None,
    bot=None,
    runtime: Optional[dict] = None,
) -> dict:
    """Return active Darvas box boundaries for dashboard display."""
    empty = {
        "valid": False,
        "active_box_number": 0,
        "box_top": 0.0,
        "box_bottom": 0.0,
        "middle_line": 0.0,
        "box_height": 0.0,
        "breakout": "CASH",
        "prev_day": "",
        "reason": "No box data available.",
    }

    snapshot = (runtime or {}).get("snapshot", {}) if runtime else {}
    box = snapshot.get("box", {}) if isinstance(snapshot.get("box"), dict) else {}
    if box.get("valid"):
        return {
            "valid": True,
            "active_box_number": int(box.get("active_box_number", 0)),
            "box_top": float(box.get("box_top", 0.0)),
            "box_bottom": float(box.get("box_bottom", 0.0)),
            "middle_line": float(box.get("middle_line", 0.0)),
            "box_height": float(box.get("box_height", 0.0)),
            "breakout": str(box.get("breakout", "CASH")).upper(),
            "prev_day": str(box.get("prev_day", "")),
            "reason": str(box.get("reason", "")),
        }

    if bot is not None:
        box = getattr(bot, "_active_box", None)
        if box is not None and getattr(box, "valid", False):
            return {
                "valid": True,
                "active_box_number": int(getattr(box, "active_box_number", 0)),
                "box_top": float(getattr(box, "top", 0.0)),
                "box_bottom": float(getattr(box, "bottom", 0.0)),
                "middle_line": float(getattr(box, "middle_line", 0.0)),
                "box_height": float(getattr(box, "height", 0.0)),
                "breakout": str(getattr(box, "breakout", "CASH")).upper(),
                "prev_day": str(getattr(box, "prev_day", "")),
                "reason": str(getattr(box, "reason", "")),
            }

    if candles is None or candles.empty:
        return empty

    from box_strategy import BoxStrategyEngine

    engine = BoxStrategyEngine(
        lookback_candles=config.BOX_LOOKBACK_CANDLES,
        confirmation_candles=config.BOX_CONFIRMATION_CANDLES,
        risk_to_reward_ratio=config.BOX_RISK_REWARD_RATIO,
        volume_filter_multiplier=config.BOX_VOLUME_FILTER_MULTIPLIER,
    )
    state = engine.evaluate(candles)
    if not state.valid:
        empty["reason"] = state.reason
        empty["active_box_number"] = state.active_box_number
        return empty

    return {
        "valid": True,
        "active_box_number": state.active_box_number,
        "box_top": state.top,
        "box_bottom": state.bottom,
        "middle_line": state.middle_line,
        "box_height": state.height,
        "breakout": state.breakout,
        "prev_day": state.prev_day,
        "reason": state.reason,
    }


def confluence_desk_summary(store) -> dict[str, object]:
    """Compact gate snapshot for the Trading Desk — same rules as ``bot_loop``."""
    if not config.is_xgboost_ml_profile():
        return {"visible": False}
    window = config.CONFLUENCE_GATE_LOOKBACK_HOURS
    if not config.CONFLUENCE_GATE_ENABLED:
        return {
            "visible": True,
            "gate_enabled": False,
            "lookback_hours": window,
            "insight": "Confluence Gate is OFF — only your local XGBoost model filters BTC entries.",
            "insight_tone": "info",
        }
    try:
        from confluence_gate import gate_readiness_snapshot

        readiness = gate_readiness_snapshot(store, hours=window)
        vetoes = store.count_radar_vetoes()
    except Exception as exc:
        return {
            "visible": True,
            "gate_enabled": True,
            "error": str(exc),
            "insight": f"Could not load fleet sentiment: {exc}",
            "insight_tone": "warning",
        }
    return {
        "visible": True,
        "gate_enabled": True,
        "lookback_hours": window,
        "total_vetoes": vetoes,
        "readiness": readiness,
        "bullish_pct": float(readiness.get("bullish_pct", 0.0)),
        "insight": str(readiness.get("insight", "")),
        "insight_tone": str(readiness.get("insight_tone", "info")),
        "error": "",
    }


def radio_tower_metrics(store) -> dict[str, object]:
    """Summary metrics for the Global AI Radio Tower dashboard panel."""
    window = config.CONFLUENCE_GATE_LOOKBACK_HOURS
    try:
        from confluence_gate import gate_readiness_snapshot

        total = store.count_external_signals()
        readiness = gate_readiness_snapshot(store, hours=window)
        top_symbol = store.top_external_signal_symbol_in_window(window)
        symbol_activity = store.external_symbol_activity(window, limit=8)
        latest = store.read_external_signals_df(limit=15)
        vetoes = store.count_radar_vetoes()
        audit = store.read_radar_decisions_df(limit=25)
    except Exception as exc:
        return {
            "total": 0,
            "top_symbol": "—",
            "readiness": {},
            "symbol_activity": pd.DataFrame(),
            "latest": pd.DataFrame(),
            "total_vetoes": 0,
            "audit": pd.DataFrame(),
            "error": str(exc),
        }

    bullish_pct = float(readiness.get("bullish_pct", 0.0))
    bearish_pct = float(readiness.get("bearish_pct", 0.0))
    return {
        "total": total,
        "top_symbol": top_symbol,
        "readiness": readiness,
        "symbol_activity": symbol_activity,
        "latest": latest,
        "total_vetoes": vetoes,
        "audit": audit,
        "lookback_hours": window,
        "core_sentiment_pct": bullish_pct,
        "core_sentiment_detail": (
            f"{bullish_pct:.1f}% bullish · {bearish_pct:.1f}% bearish · "
            f"{int(readiness.get('total_ops', 0))} fleet ops / {window:.0f}h"
        ),
        "error": "",
    }


def confluence_radar_metrics(store) -> dict[str, object]:
    """Backward-compatible wrapper — prefer :func:`radio_tower_metrics`."""
    metrics = radio_tower_metrics(store)
    readiness = metrics.get("readiness", {}) or {}
    return {
        "core_sentiment_pct": metrics.get("core_sentiment_pct", 0.0),
        "core_sentiment_detail": metrics.get("core_sentiment_detail", ""),
        "total_vetoes": metrics.get("total_vetoes", 0),
        "audit": metrics.get("audit", pd.DataFrame()),
        "readiness": readiness,
        "error": metrics.get("error", ""),
    }


def format_radar_verdict(verdict: str) -> str:
    verdict_u = str(verdict or "").upper()
    if verdict_u == "VETO":
        return "🔴 VETOED"
    if verdict_u == "APPROVE":
        return "🟢 APPROVED"
    return verdict_u


def format_external_action_tag(action: str) -> str:
    """Readable action tag with simple direction hint for the signal table."""
    action_u = str(action or "").upper()
    if action_u in {"BUY", "COVER"}:
        return f"🟢 {action_u}"
    if action_u in {"SELL", "SHORT"}:
        return f"🔴 {action_u}"
    return action_u
