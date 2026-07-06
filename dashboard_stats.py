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
        client = exchange_client.build_execution_client()
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
    if bot is not None:
        snap = bot.risk.snapshot()
        consecutive_wins = snap.consecutive_wins
        consecutive_losses = snap.consecutive_losses
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


def bot_health(bot, log: pd.DataFrame) -> dict:
    """Heartbeat snapshot — is the engine alive and scanning?"""
    running = bool(bot is not None and bot.state.running)
    degraded = bool(bot is not None and bot.state.connection_degraded)
    last_action = "—"
    last_event = "—"
    last_scan_str = "never"
    seconds_ago: Optional[float] = None
    stale = True
    open_position = "FLAT"

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
            now = pd.Timestamp.now(tz="UTC")
            seconds_ago = max(0.0, (now - last_scan).total_seconds())
            last_scan_str = last_scan.strftime("%H:%M:%S UTC")
            stale_threshold = max(30.0, config.LOOP_SLEEP_SECONDS * 4)
            stale = seconds_ago > stale_threshold

    if not running:
        status = "OFFLINE"
        detail = "Boot the engine from the sidebar to start scanning."
    elif degraded:
        status = "DEGRADED"
        detail = bot.state.connection_error or "Exchange API issues — entries paused."
    elif running and log.empty:
        started = getattr(bot, "_session_started_at", None)
        boot_grace = max(30.0, config.LOOP_SLEEP_SECONDS * 4)
        if started is not None:
            now = pd.Timestamp.now(tz="UTC")
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            secs_since_boot = max(0.0, (now - started).total_seconds())
            if secs_since_boot <= boot_grace:
                status = "BOOTING"
                detail = "Engine started — waiting for first scan to complete."
                stale = False
            else:
                status = "STALE"
                detail = "No status rows written — check bot thread for errors."
        else:
            status = "STALE"
            detail = "No status log rows yet — check bot thread."
    elif stale:
        status = "STALE"
        if seconds_ago is not None:
            detail = f"No log update in {int(seconds_ago)}s — check bot thread."
        else:
            detail = "No status log rows yet — check bot thread."
    else:
        status = "LIVE"
        detail = f"Scanning every {config.LOOP_SLEEP_SECONDS}s · last action {last_action}"

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
        "detail": detail,
    }
