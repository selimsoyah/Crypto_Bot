from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DarvasBoxProfileSettings:
    """Profile-level settings for the Darvas-style box breakout runtime."""

    name: str = "darvas_box"
    strategy: str = "box_breakout"
    interval: str = "15m"
    uses_model: bool = False
    box_lookback_candles: int = 20
    box_confirmation_candles: int = 3
    risk_to_reward_ratio: float = 2.0
    volume_filter_multiplier: float = 1.2
    stop_buffer_pct: float = 0.0005

