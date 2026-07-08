"""
box_strategy.py
===============
Darvas-style box breakout engine used by the ``darvas_box`` trading profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass(frozen=True)
class BoxState:
    valid: bool
    top: float = 0.0
    bottom: float = 0.0
    height: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    volume_sma20: float = 0.0
    volume_ok: bool = True
    breakout: str = "CASH"  # LONG | SHORT | CASH
    timestamp: str = ""
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "box_top": self.top,
            "box_bottom": self.bottom,
            "box_height": self.height,
            "close": self.close,
            "volume": self.volume,
            "volume_sma20": self.volume_sma20,
            "volume_ok": self.volume_ok,
            "breakout": self.breakout,
            "timestamp": self.timestamp,
            "reason": self.reason,
        }


class BoxStrategyEngine:
    """Compute active box boundaries and breakout decisions from OHLCV bars."""

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

    def _last_window(self, candles: pd.DataFrame) -> Optional[pd.DataFrame]:
        need = self.lookback_candles + self.confirmation_candles
        if candles is None or candles.empty or len(candles) < need:
            return None
        return candles.iloc[-self.lookback_candles :].copy()

    def _confirmed_top(self, window: pd.DataFrame) -> float:
        top = float(window["High"].max())
        tail = window.iloc[-self.confirmation_candles :]
        if float(tail["High"].max()) <= top:
            return top
        return 0.0

    def _confirmed_bottom(self, window: pd.DataFrame) -> float:
        bottom = float(window["Low"].min())
        tail = window.iloc[-self.confirmation_candles :]
        if float(tail["Low"].min()) >= bottom:
            return bottom
        return 0.0

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
        window = self._last_window(candles)
        if window is None:
            return BoxState(valid=False, reason="Not enough candles for box construction.")

        top = self._confirmed_top(window)
        bottom = self._confirmed_bottom(window)
        if top <= 0 or bottom <= 0 or top <= bottom:
            return BoxState(valid=False, reason="Box boundaries not confirmed.")

        close = float(candles["Close"].iloc[-1])
        vol_ok, vol, vol_sma = self._volume_ok(candles)
        ts = ""
        if "Timestamp" in candles.columns and not candles.empty:
            ts = str(candles["Timestamp"].iloc[-1])

        breakout = "CASH"
        reason = "Price remains inside active box."
        if close > top:
            if vol_ok:
                breakout = "LONG"
                reason = "Close broke above box top."
            else:
                reason = "Long breakout detected but volume filter blocked entry."
        elif close < bottom:
            if vol_ok:
                breakout = "SHORT"
                reason = "Close broke below box bottom."
            else:
                reason = "Short breakout detected but volume filter blocked entry."

        return BoxState(
            valid=True,
            top=top,
            bottom=bottom,
            height=top - bottom,
            close=close,
            volume=vol,
            volume_sma20=vol_sma,
            volume_ok=vol_ok,
            breakout=breakout,
            timestamp=ts,
            reason=reason,
        )

