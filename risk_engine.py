"""
risk_engine.py
==============
Hard, non-bypassable risk layer between model signals and order execution.

Sits in front of every new order and enforces:
    * Max session loss circuit breaker (realized + unrealized vs start equity)
    * Max consecutive-loss pause (manual resume required)
    * Volatility-aware position sizing (ATR-scaled margin; leverage separate)
    * Order sanity checks (price deviation guard)
    * Kill switch (file flag + in-memory; flattens and halts)

Nothing in ``bot_loop`` should place a new order without passing through this
module first.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import config

logger = config.configure_logging(__name__)


@dataclass
class RiskDecision:
    """Outcome of a risk check — the bot must obey ``allowed`` / ``flatten``."""

    allowed: bool
    reason: str = ""
    halt_new_orders: bool = False
    flatten_positions: bool = False
    event: str = "WARNING"


@dataclass
class RiskState:
    """Mutable session risk snapshot."""

    session_start_equity: float = 0.0
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    halted: bool = False
    halt_reason: str = ""
    manual_resume_required: bool = False
    kill_switch_active: bool = False
    last_good_price: float = 0.0
    last_vol_scale: float = 1.0


@dataclass
class PositionSizeResult:
    """Margin / notional sizing output (leverage applied separately)."""

    margin_usdt: float
    notional_usdt: float
    vol_scale: float
    base_pct: float
    intended_margin_usdt: float = 0.0
    exchange_floor_applied: bool = False


class RiskEngine:
    """Session-scoped risk controller for the live trading bot."""

    def __init__(self) -> None:
        self.state = RiskState()
        self._lock = __import__("threading").Lock()

    # ------------------------------------------------------------------ #
    # Session lifecycle                                                  #
    # ------------------------------------------------------------------ #
    def begin_session(self, starting_equity: float) -> None:
        """Reset counters at engine boot."""
        with self._lock:
            self.state = RiskState(
                session_start_equity=max(starting_equity, 0.0),
                last_good_price=0.0,
            )
        self._clear_manual_resume_file()
        logger.info(
            "Risk engine armed — session equity $%s, max loss %.1f%%, "
            "max consecutive losses %d.",
            f"{starting_equity:,.2f}",
            self._loss_limit_pct() * 100,
            config.RISK_MAX_CONSECUTIVE_LOSSES,
        )

    @staticmethod
    def _loss_limit_pct() -> float:
        if config.is_compound_profile():
            return config.RISK_MAX_WEEKLY_LOSS_PCT
        return config.RISK_MAX_DAILY_LOSS_PCT

    def snapshot(self) -> RiskState:
        with self._lock:
            return RiskState(**self.state.__dict__)

    # ------------------------------------------------------------------ #
    # Kill switch                                                        #
    # ------------------------------------------------------------------ #
    def kill_switch_file_active(self) -> bool:
        return os.path.exists(config.KILL_SWITCH_FILE)

    def trigger_kill_switch(self, reason: str = "Manual kill switch activated") -> None:
        """Engage kill switch: write flag file and halt in memory."""
        try:
            with open(config.KILL_SWITCH_FILE, "w", encoding="utf-8") as fh:
                fh.write(reason)
        except OSError as exc:
            logger.error("Could not write kill switch file: %s", exc)
        with self._lock:
            self.state.kill_switch_active = True
            self.state.halted = True
            self.state.halt_reason = reason
        logger.critical("KILL SWITCH ENGAGED: %s", reason)

    def clear_kill_switch(self) -> None:
        """Clear kill switch after operator review (does not auto-resume bot)."""
        try:
            if os.path.exists(config.KILL_SWITCH_FILE):
                os.remove(config.KILL_SWITCH_FILE)
        except OSError as exc:
            logger.warning("Could not remove kill switch file: %s", exc)
        with self._lock:
            self.state.kill_switch_active = False

    # ------------------------------------------------------------------ #
    # Manual resume (consecutive-loss pause)                               #
    # ------------------------------------------------------------------ #
    def _write_manual_resume_file(self) -> None:
        try:
            with open(config.RISK_MANUAL_RESUME_FILE, "w", encoding="utf-8") as fh:
                fh.write("manual resume required\n")
        except OSError as exc:
            logger.warning("Could not write manual resume file: %s", exc)

    def _clear_manual_resume_file(self) -> None:
        try:
            if os.path.exists(config.RISK_MANUAL_RESUME_FILE):
                os.remove(config.RISK_MANUAL_RESUME_FILE)
        except OSError as exc:
            logger.warning("Could not remove manual resume file: %s", exc)

    def confirm_manual_resume(self) -> RiskDecision:
        """Operator acknowledges consecutive-loss pause — allows trading again."""
        with self._lock:
            if not self.state.manual_resume_required:
                return RiskDecision(allowed=True, reason="No manual resume pending.")
            self.state.manual_resume_required = False
            self.state.consecutive_losses = 0
            self.state.consecutive_wins = 0
            if "CONSECUTIVE" in self.state.halt_reason.upper():
                self.state.halted = False
                self.state.halt_reason = ""
        self._clear_manual_resume_file()
        logger.info("Manual risk resume confirmed — consecutive-loss counter reset.")
        return RiskDecision(allowed=True, reason="Trading resumed after manual confirmation.")

    # ------------------------------------------------------------------ #
    # PnL / circuit breakers                                             #
    # ------------------------------------------------------------------ #
    def session_pnl_usdt(self, realized: float, unrealized: float) -> float:
        return realized + unrealized

    def session_pnl_pct(self, realized: float, unrealized: float) -> float:
        with self._lock:
            base = self.state.session_start_equity
        if base <= 0:
            return 0.0
        return self.session_pnl_usdt(realized, unrealized) / base

    def check_session_loss_limit(
        self, realized: float, unrealized: float
    ) -> RiskDecision:
        """Trip if session PnL <= -max daily loss % of starting equity."""
        pnl_pct = self.session_pnl_pct(realized, unrealized)
        limit = -self._loss_limit_pct()
        if pnl_pct <= limit:
            label = "WEEKLY" if config.is_compound_profile() else "SESSION"
            msg = (
                f"{label} LOSS LIMIT BREACHED: {pnl_pct * 100:.2f}% "
                f"(limit {limit * 100:.1f}% of ${self.state.session_start_equity:,.2f} "
                f"starting equity). Force-flattening and halting all new orders."
            )
            with self._lock:
                self.state.halted = True
                self.state.halt_reason = msg
            return RiskDecision(
                allowed=False,
                reason=msg,
                halt_new_orders=True,
                flatten_positions=True,
                event="STOP_LOSS",
            )
        return RiskDecision(allowed=True)

    def record_trade_close(self, realized_pnl: float) -> RiskDecision:
        """Update consecutive win/loss counters after a completed trade."""
        with self._lock:
            if realized_pnl < 0:
                self.state.consecutive_losses += 1
                self.state.consecutive_wins = 0
            else:
                self.state.consecutive_wins += 1
                self.state.consecutive_losses = 0
            streak = self.state.consecutive_losses

        if streak >= config.RISK_MAX_CONSECUTIVE_LOSSES:
            msg = (
                f"CONSECUTIVE LOSS LIMIT: {streak} losing trades in a row "
                f"(max {config.RISK_MAX_CONSECUTIVE_LOSSES}). "
                f"New entries PAUSED until manual resume."
            )
            with self._lock:
                self.state.manual_resume_required = True
                self.state.halted = True
                self.state.halt_reason = msg
            self._write_manual_resume_file()
            logger.warning(msg)
            return RiskDecision(
                allowed=False,
                reason=msg,
                halt_new_orders=True,
                event="WARNING",
            )
        return RiskDecision(allowed=True)

    # ------------------------------------------------------------------ #
    # Pre-trade gates                                                    #
    # ------------------------------------------------------------------ #
    def check_kill_switch(self) -> RiskDecision:
        if self.kill_switch_file_active() or self.state.kill_switch_active:
            with self._lock:
                self.state.kill_switch_active = True
                self.state.halted = True
                if not self.state.halt_reason:
                    self.state.halt_reason = "Kill switch file is active."
            return RiskDecision(
                allowed=False,
                reason=self.state.halt_reason or "Kill switch active.",
                halt_new_orders=True,
                flatten_positions=True,
                event="STOP_LOSS",
            )
        return RiskDecision(allowed=True)

    def check_can_open(self) -> RiskDecision:
        """Combined gate before any new entry order."""
        kill = self.check_kill_switch()
        if not kill.allowed:
            return kill
        with self._lock:
            if self.state.manual_resume_required:
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"Trading paused — manual resume required after "
                        f"{self.state.consecutive_losses} consecutive losses."
                    ),
                    halt_new_orders=True,
                    event="WARNING",
                )
            if self.state.halted:
                return RiskDecision(
                    allowed=False,
                    reason=self.state.halt_reason or "Risk engine halted.",
                    halt_new_orders=True,
                    event="WARNING",
                )
        return RiskDecision(allowed=True)

    def update_last_good_price(self, price: float) -> None:
        if price > 0:
            with self._lock:
                self.state.last_good_price = price

    def validate_order_sanity(
        self,
        price: float,
        quantity: float,
        notional: float,
    ) -> RiskDecision:
        """Reject orders with absurd price/qty vs last known good tick."""
        if price <= 0 or quantity <= 0 or notional <= 0:
            return RiskDecision(
                allowed=False,
                reason=f"Order sanity failed: invalid price/qty/notional "
                f"({price}, {quantity}, {notional}).",
                event="WARNING",
            )
        with self._lock:
            ref = self.state.last_good_price
        if ref > 0:
            deviation = abs(price - ref) / ref
            if deviation > config.RISK_ORDER_PRICE_DEVIATION_PCT:
                return RiskDecision(
                    allowed=False,
                    reason=(
                        f"Order BLOCKED — price ${price:,.2f} deviates "
                        f"{deviation * 100:.2f}% from last good tick ${ref:,.2f} "
                        f"(max {config.RISK_ORDER_PRICE_DEVIATION_PCT * 100:.1f}%). "
                        f"Possible stale data or API glitch."
                    ),
                    event="WARNING",
                )
        return RiskDecision(allowed=True)

    # ------------------------------------------------------------------ #
    # Volatility-aware sizing                                            #
    # ------------------------------------------------------------------ #
    def compute_position_size(
        self,
        total_wallet: float,
        atr_pct: float,
        leverage: Optional[int] = None,
        compound_mult: Optional[float] = None,
        exchange_min_notional: Optional[float] = None,
    ) -> PositionSizeResult:
        """Return margin and notional for the next trade.

        Margin scales down when ``atr_pct`` exceeds the baseline (choppy/high-vol
        conditions). Leverage is applied *after* margin sizing — decoupled from
        the risk budget. ``compound_mult`` applies win/loss streak scaling.

        When the vol-scaled allocation is below the exchange minimum notional,
        margin is raised only to ``exchange_min_notional / leverage`` — never
        to a fixed legacy floor that would override compounding on larger wallets.
        """
        from compound_strategy import compound_size_multiplier

        lev = leverage if leverage is not None else config.LEVERAGE
        baseline = max(config.RISK_ATR_BASELINE_PCT, 1e-9)
        atr = max(float(atr_pct), 1e-9)
        vol_ratio = atr / baseline
        scale = min(1.0, 1.0 / vol_ratio)
        scale = max(scale, config.RISK_VOL_SCALE_FLOOR)

        if compound_mult is None:
            with self._lock:
                wins = self.state.consecutive_wins
                losses = self.state.consecutive_losses
            compound_mult = compound_size_multiplier(wins, losses)

        intended_margin = total_wallet * config.CASH_ALLOCATION_PCT * scale * compound_mult
        min_notional = (
            exchange_min_notional
            if exchange_min_notional is not None
            else config.EXCHANGE_MIN_NOTIONAL_USDT
        )
        min_margin = float(min_notional) / max(lev, 1)

        exchange_floor_applied = False
        margin = intended_margin
        if margin < min_margin:
            margin = min_margin
            exchange_floor_applied = True

        notional = margin * lev

        with self._lock:
            self.state.last_vol_scale = scale

        return PositionSizeResult(
            margin_usdt=margin,
            notional_usdt=notional,
            vol_scale=scale,
            base_pct=config.CASH_ALLOCATION_PCT,
            intended_margin_usdt=intended_margin,
            exchange_floor_applied=exchange_floor_applied,
        )
