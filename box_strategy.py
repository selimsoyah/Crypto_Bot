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
    def previous_utc_day_bounds(
        candles: pd.DataFrame,
        as_of: Optional[pd.Timestamp] = None,
    ) -> tuple[float, float, float, date, pd.DataFrame]:
        """Return (box_top, box_bottom, middle_line, prev_day, prev_day_rows)."""
        frame = BoxStrategyEngine._with_utc_timestamps(candles)
        if frame.empty:
            raise ValueError("No candle timestamps available for previous-day box bounds.")

        if as_of is None:
            as_of = frame["Timestamp"].iloc[-1]
        as_of = pd.Timestamp(as_of)
        if as_of.tzinfo is None:
            as_of = as_of.tz_localize("UTC")
        else:
            as_of = as_of.tz_convert("UTC")

        session_date = as_of.date()
        prev_day = session_date - timedelta(days=1)
        day_start = pd.Timestamp(datetime(prev_day.year, prev_day.month, prev_day.day, tzinfo=timezone.utc))
        day_end = day_start + timedelta(days=1) - pd.Timedelta(seconds=1)

        mask = (frame["Timestamp"] >= day_start) & (frame["Timestamp"] <= day_end)
        prev_rows = frame.loc[mask]
        if prev_rows.empty:
            raise ValueError(f"No OHLCV rows for previous UTC day {prev_day.isoformat()}.")

        top = float(prev_rows["High"].max())
        bottom = float(prev_rows["Low"].min())
        if top <= bottom:
            raise ValueError("Previous-day high/low produced an invalid box range.")
        middle = (top + bottom) / 2.0
        return top, bottom, middle, prev_day, prev_rows

    def _update_active_box_number(self, session_utc_date: date) -> int:
        if self._tracked_session_date is None:
            self._tracked_session_date = session_utc_date
            self._active_box_number = 1
        elif session_utc_date > self._tracked_session_date:
            self._active_box_number += 1
            self._tracked_session_date = session_utc_date
        return self._active_box_number

    def _volume_ok(self, candles: pd.DataFrame) -> tuple[bool, float, float]:
        if "Volume" not in candles.columns or candles.empty:
            return True, 0.0, 0.0
        vol = float(candles["Volume"].iloc[-1])
        vol_sma = float(candles["Volume"].tail(20).mean())
        if vol_sma <= 0:
            return True, vol, vol_sma
        ok = vol > (self.volume_filter_multiplier * vol_sma)
        return ok, vol, vol_sma

    def evaluate(self, candles: pd.DataFrame) -> BoxState:
        frame = self._with_utc_timestamps(candles)
        if frame.empty:
            return BoxState(valid=False, reason="No candle data available for box evaluation.")

        close = float(frame["Close"].iloc[-1])
        ts = frame["Timestamp"].iloc[-1]
        session_date = ts.date()
        active_box_number = self._update_active_box_number(session_date)

        try:
            top, bottom, middle, prev_day, _prev_rows = self.previous_utc_day_bounds(frame, as_of=ts)
        except ValueError as exc:
            return BoxState(
                valid=False,
                close=close,
                timestamp=str(ts),
                active_box_number=active_box_number,
                reason=str(exc),
            )

        vol_ok, vol, vol_sma = self._volume_ok(frame)

        breakout = "CASH"
        reason = (
            f"Price inside previous-day box ({bottom:,.2f} - {top:,.2f}) "
            f"anchored from {prev_day.isoformat()} UTC."
        )
        if close > top:
            if vol_ok:
                breakout = "LONG"
                reason = f"Close broke above previous-day high ({top:,.2f})."
            else:
                reason = "Long breakout above previous-day high blocked by volume filter."
        elif close < bottom:
            if vol_ok:
                breakout = "SHORT"
                reason = f"Close broke below previous-day low ({bottom:,.2f})."
            else:
                reason = "Short breakout below previous-day low blocked by volume filter."

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
