"""
compound_strategy.py
====================
Path B — active compounding logic shared by the bot, trainer, and dashboard.

Centralises bracket math, trailing-stop updates, threshold-distance helpers,
and compound position-size multipliers so live execution matches backtests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

import config

if TYPE_CHECKING:
    from bot_loop import Position


@dataclass
class BracketLevels:
    """Take-profit and stop-loss prices for one directional entry."""

    take_profit: float
    stop_loss: float
    tp_pct: float
    sl_pct: float


def effective_brackets(
    direction: str,
    entry_price: float,
    atr_pct: Optional[float] = None,
) -> BracketLevels:
    """Return TP/SL prices — fixed audit brackets or ATR-scaled in compound mode."""
    if direction not in ("LONG", "SHORT"):
        raise ValueError(f"Unknown direction: {direction!r}")

    tp_pct = config.TAKE_PROFIT_PCT
    sl_pct = config.STOP_LOSS_PCT

    audit_fixed = config.is_compound_profile() and config.EXECUTION_AUDIT_PARITY
    if (
        not audit_fixed
        and config.is_compound_profile()
        and config.USE_ATR_BRACKETS
        and atr_pct
    ):
        atr = max(float(atr_pct), 1e-6)
        tp_pct = max(
            config.ATR_BRACKET_TP_MULT * atr,
            config.TAKE_PROFIT_PCT * 0.5,
        )
        sl_pct = max(
            config.ATR_BRACKET_SL_MULT * atr,
            config.STOP_LOSS_PCT * 0.5,
        )
        tp_pct = min(tp_pct, config.TAKE_PROFIT_PCT * 2.0)
        sl_pct = min(sl_pct, config.STOP_LOSS_PCT * 2.0)

    if direction == "LONG":
        return BracketLevels(
            take_profit=entry_price * (1.0 + tp_pct),
            stop_loss=entry_price * (1.0 - sl_pct),
            tp_pct=tp_pct,
            sl_pct=sl_pct,
        )
    return BracketLevels(
        take_profit=entry_price * (1.0 - tp_pct),
        stop_loss=entry_price * (1.0 + sl_pct),
        tp_pct=tp_pct,
        sl_pct=sl_pct,
    )


def update_trailing_stop(pos: "Position", price: float) -> bool:
    """Ratchet the stop toward price once activation profit is reached.

    Returns ``True`` when the stop was moved this tick.
    """
    if config.is_compound_profile() and config.EXECUTION_AUDIT_PARITY:
        return False
    if not config.TRAILING_STOP_ENABLED:
        return False

    moved = False
    if pos.side == "LONG":
        if price > pos.best_price:
            pos.best_price = price
        gain = (pos.best_price - pos.entry_price) / pos.entry_price
        if gain >= config.TRAILING_STOP_ACTIVATION_PCT:
            pos.trail_active = True
            candidate = pos.best_price * (1.0 - config.TRAILING_STOP_DISTANCE_PCT)
            if candidate > pos.stop_loss_price:
                pos.stop_loss_price = candidate
                moved = True
    else:
        if price < pos.best_price or pos.best_price <= 0:
            pos.best_price = price
        gain = (pos.entry_price - pos.best_price) / pos.entry_price
        if gain >= config.TRAILING_STOP_ACTIVATION_PCT:
            pos.trail_active = True
            candidate = pos.best_price * (1.0 + config.TRAILING_STOP_DISTANCE_PCT)
            if candidate < pos.stop_loss_price:
                pos.stop_loss_price = candidate
                moved = True
    return moved


def threshold_distance(
    prob_long: float,
    prob_short: float,
    long_thr: float,
    short_thr: float,
) -> dict:
    """How far live probabilities are from firing (for dashboard)."""
    return {
        "long_gap": max(0.0, long_thr - prob_long),
        "short_gap": max(0.0, short_thr - prob_short),
        "long_ready": prob_long > long_thr,
        "short_ready": prob_short > short_thr,
        "nearest_side": (
            "LONG"
            if prob_long >= prob_short
            else "SHORT"
        ),
    }


def compound_size_multiplier(consecutive_wins: int, consecutive_losses: int) -> float:
    """Scale next margin from recent streak (wins up, losses down)."""
    if not config.is_compound_profile():
        return 1.0

    mult = 1.0
    for _ in range(min(consecutive_wins, config.COMPOUND_STREAK_CAP)):
        mult *= config.COMPOUND_WIN_STREAK_BOOST
    for _ in range(min(consecutive_losses, config.COMPOUND_STREAK_CAP)):
        mult *= config.COMPOUND_LOSS_STREAK_CUT
    return max(
        config.COMPOUND_MIN_SIZE_MULT,
        min(config.COMPOUND_MAX_SIZE_MULT, mult),
    )
