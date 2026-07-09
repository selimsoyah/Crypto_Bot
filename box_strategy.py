"""
box_strategy.py
===============
Darvas-style box breakout engine used by the ``darvas_box`` trading profile.

Active session boundaries are anchored to the **previous UTC calendar day's**
high and low (00:00–23:59 UTC), not a rolling intraday window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

import config

logger = config.configure_logging(__name__)


@dataclass(frozen=True)
class BoxState:
    valid: bool
    top: float = 0.0
    bottom: float = 0.0
    middle_line: float = 0.0
    height: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    volume_sma20: float = 0.0
    volume_ok: bool = True
    breakout: str = "CASH"  # LONG | SHORT | CASH
    timestamp: str = ""
    reason: str = ""
    active_box_number: int = 0
    prev_day: str = ""

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "box_top": self.top,
            "box_bottom": self.bottom,
            "middle_line": self.middle_line,
            "box_height": self.height,
            "close": self.close,
            "volume": self.volume,
            "volume_sma20": self.volume_sma20,
            "volume_ok": self.volume_ok,
            "breakout": self.breakout,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "active_box_number": self.active_box_number,
            "prev_day": self.prev_day,
        }


class BoxStrategyEngine:
    """Compute daily-anchored box boundaries and breakout decisions from OHLCV bars."""

    def __init__(
        self,
        *,
        lookback_candles: int = 20,
        confirmation_candles: int = 3,
        risk_to_reward_ratio: float = 2.0,
        volume_filter_multiplier: float = 1.2,
    ) -> None:
        self.lookback_candles = max(5, int(lookback_candles))
        self.confirmation_candles = max(1, int(confirmation_candles))
        self.risk_to_reward_ratio = float(risk_to_reward_ratio)
        self.volume_filter_multiplier = float(volume_filter_multiplier)
        self._active_box_number = 0
        self._tracked_session_date: Optional[date] = None
        self._logged_box_number: Optional[int] = None

    @staticmethod
    def _with_utc_timestamps(candles: pd.DataFrame) -> pd.DataFrame:
        if candles is None or candles.empty:
            return pd.DataFrame()
        frame = candles.copy()
        if "Timestamp" not in frame.columns:
            return frame
        frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], utc=True, errors="coerce")
        return frame.dropna(subset=["Timestamp"])

    @staticmethod
    def interval_timedelta(interval: str | None = None) -> pd.Timedelta:
        token = str(interval or config.INTERVAL).strip().lower()
        if token.endswith("m"):
            return pd.Timedelta(minutes=int(token[:-1]))
        if token.endswith("h"):
            return pd.Timedelta(hours=int(token[:-1]))
        if token.endswith("d"):
            return pd.Timedelta(days=int(token[:-1]))
        raise ValueError(f"Unsupported interval token: {token}")

    @staticmethod
    def last_closed_bar_index(frame: pd.DataFrame, interval: str | None = None) -> int:
        """Index of the latest fully closed interval bar (never the forming candle)."""
        if frame is None or frame.empty:
            return -1
        if len(frame) == 1:
            return 0
        last_ts = pd.Timestamp(frame["Timestamp"].iloc[-1])
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        else:
            last_ts = last_ts.tz_convert("UTC")
        bar_end = last_ts + BoxStrategyEngine.interval_timedelta(interval)
        now = pd.Timestamp.now(tz="UTC")
        if now < bar_end:
            return len(frame) - 2
        return len(frame) - 1

    @staticmethod
    def previous_utc_day_bounds(
        candles: pd.DataFrame,
        as_of: Optional[pd.Timestamp] = None,
        *,
        daily_high: Optional[float] = None,
        daily_low: Optional[float] = None,
    ) -> tuple[float, float, float, date, pd.DataFrame]:
        """Return (box_top, box_bottom, middle_line, prev_day, prev_day_rows)."""
        frame = BoxStrategyEngine._with_utc_timestamps(candles)
        if frame.empty and (daily_high is None or daily_low is None):
            raise ValueError("No candle timestamps available for previous-day box bounds.")

        if as_of is None:
            if frame.empty:
                as_of = pd.Timestamp.now(tz="UTC")
            else:
                closed_idx = BoxStrategyEngine.last_closed_bar_index(frame)
                as_of = frame["Timestamp"].iloc[closed_idx]
        as_of = pd.Timestamp(as_of)
        if as_of.tzinfo is None:
            as_of = as_of.tz_localize("UTC")
        else:
            as_of = as_of.tz_convert("UTC")

        session_date = as_of.date()
        prev_day = session_date - timedelta(days=1)
        day_start = pd.Timestamp(datetime(prev_day.year, prev_day.month, prev_day.day, tzinfo=timezone.utc))
        next_day_start = day_start + timedelta(days=1)

        prev_rows = pd.DataFrame()
        if not frame.empty:
            mask = (frame["Timestamp"] >= day_start) & (frame["Timestamp"] < next_day_start)
            prev_rows = frame.loc[mask]

        if daily_high is not None and daily_low is not None:
            top = float(daily_high)
            bottom = float(daily_low)
        elif not prev_rows.empty:
            top = float(prev_rows["High"].max())
            bottom = float(prev_rows["Low"].min())
        else:
            raise ValueError(f"No OHLCV rows for previous UTC day {prev_day.isoformat()}.")

        if top <= bottom:
            raise ValueError("Previous-day high/low produced an invalid box range.")
        middle = (top + bottom) / 2.0
        return top, bottom, middle, prev_day, prev_rows

    @staticmethod
    def log_box_bounds(
        top: float,
        bottom: float,
        prev_day: date | str,
        *,
        source: str,
        prev_day_bars: int = 0,
        closed_close: float | None = None,
    ) -> None:
        middle = (top + bottom) / 2.0
        logger.info(
            "DARVAS BOX BOUNDS | source=%s | prev_day=%s | box_top=%.8f | box_bottom=%.8f | "
            "middle_line=%.8f (visual only) | prev_day_bars=%d | closed_close=%s",
            source,
            prev_day,
            top,
            bottom,
            middle,
            prev_day_bars,
            f"{closed_close:.8f}" if closed_close is not None else "n/a",
        )

    def _update_active_box_number(self, session_utc_date: date) -> int:
        if self._tracked_session_date is None:
            self._tracked_session_date = session_utc_date
            self._active_box_number = 1
        elif session_utc_date > self._tracked_session_date:
            self._active_box_number += 1
            self._tracked_session_date = session_utc_date
        return self._active_box_number

    def _volume_ok(self, candles: pd.DataFrame, closed_idx: int) -> tuple[bool, float, float]:
        if "Volume" not in candles.columns or candles.empty or closed_idx < 0:
            return True, 0.0, 0.0
        vol = float(candles["Volume"].iloc[closed_idx])
        vol_sma = float(candles["Volume"].tail(20).mean())
        if vol_sma <= 0:
            return True, vol, vol_sma
        ok = vol > (self.volume_filter_multiplier * vol_sma)
        return ok, vol, vol_sma

    def evaluate(
        self,
        candles: pd.DataFrame,
        *,
        daily_high: Optional[float] = None,
        daily_low: Optional[float] = None,
    ) -> BoxState:
        frame = self._with_utc_timestamps(candles)
        if frame.empty:
            return BoxState(valid=False, reason="No candle data available for box evaluation.")

        closed_idx = self.last_closed_bar_index(frame)
        if closed_idx < 0:
            return BoxState(valid=False, reason="No closed candle available for box evaluation.")

        closed_row = frame.iloc[closed_idx]
        close = float(closed_row["Close"])
        ts = closed_row["Timestamp"]
        session_date = pd.Timestamp(ts).date()
        active_box_number = self._update_active_box_number(session_date)

        try:
            top, bottom, middle, prev_day, prev_rows = self.previous_utc_day_bounds(
                frame,
                as_of=ts,
                daily_high=daily_high,
                daily_low=daily_low,
            )
        except ValueError as exc:
            return BoxState(
                valid=False,
                close=close,
                timestamp=str(ts),
                active_box_number=active_box_number,
                reason=str(exc),
            )

        bounds_source = "exchange_1d" if daily_high is not None and daily_low is not None else "15m_aggregate"
        if active_box_number != getattr(self, "_logged_box_number", None):
            self._logged_box_number = active_box_number
            self.log_box_bounds(
                top,
                bottom,
                prev_day,
                source=bounds_source,
                prev_day_bars=len(prev_rows),
                closed_close=close,
            )

        vol_ok, vol, vol_sma = self._volume_ok(frame, closed_idx)

        breakout = "CASH"
        reason = (
            f"Closed 15m bar inside previous-day box ({bottom:,.2f} - {top:,.2f}) "
            f"anchored from {prev_day.isoformat()} UTC."
        )
        # Strict outer-boundary triggers only — middle_line is never used for entries.
        if close > top:
            if vol_ok:
                breakout = "LONG"
                reason = (
                    f"Closed 15m bar broke above previous-day high: "
                    f"close {close:,.2f} > box_top {top:,.2f}."
                )
            else:
                reason = (
                    f"Long breakout blocked by volume filter "
                    f"(close {close:,.2f} > box_top {top:,.2f})."
                )
        elif close < bottom:
            if vol_ok:
                breakout = "SHORT"
                reason = (
                    f"Closed 15m bar broke below previous-day low: "
                    f"close {close:,.2f} < box_bottom {bottom:,.2f}."
                )
            else:
                reason = (
                    f"Short breakout blocked by volume filter "
                    f"(close {close:,.2f} < box_bottom {bottom:,.2f})."
                )

        return BoxState(
            valid=True,
            top=top,
            bottom=bottom,
            middle_line=middle,
            height=top - bottom,
            close=close,
            volume=vol,
            volume_sma20=vol_sma,
            volume_ok=vol_ok,
            breakout=breakout,
            timestamp=str(ts),
            reason=reason,
            active_box_number=active_box_number,
            prev_day=prev_day.isoformat(),
        )
