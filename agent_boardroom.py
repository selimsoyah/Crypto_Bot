"""
agent_boardroom.py
==================
Executive boardroom — resolves local ML signals against Global Radar sentiment.

When fleet momentum is extreme, the boardroom may **override** a counter-trend
local signal instead of leaving the bot stuck in cash. Resolutions are appended
to ``boardroom_consensus_log.csv`` for audit.
"""

from __future__ import annotations

import csv
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
from confluence_gate import compute_global_sentiment
from trade_store import TradeStore

logger = config.configure_logging(__name__)

# Rigid conviction bands (global bullish %).
EXTREME_BULLISH_MIN: float = 85.0
MODERATE_BULLISH_MIN: float = 70.0
NEUTRAL_MIN: float = 30.1
NEUTRAL_MAX: float = 69.9
MODERATE_BEARISH_MAX: float = 30.0
EXTREME_BEARISH_MAX: float = 15.0

REGIME_EXTREME_BULLISH = "EXTREME_BULLISH_OVERRIDE"
REGIME_MODERATE_BULLISH = "MODERATE_BULLISH_HUNTING"
REGIME_NEUTRAL = "NEUTRAL"
REGIME_MODERATE_BEARISH = "MODERATE_BEARISH_HUNTING"
REGIME_EXTREME_BEARISH = "EXTREME_BEARISH_OVERRIDE"

VERDICT_EXECUTE_LONG = "EXECUTE LONG"
VERDICT_EXECUTE_SHORT = "EXECUTE SHORT"
VERDICT_EXECUTE_LONG_OVERRIDE = "EXECUTE LONG (OVERRIDE)"
VERDICT_EXECUTE_SHORT_OVERRIDE = "EXECUTE SHORT (OVERRIDE)"
VERDICT_STAND_ASIDE = "STAND ASIDE (CONFLICT)"
VERDICT_HOLD_CASH = "HOLD CASH"

_BOARDROOM_CSV_COLUMNS = [
    "timestamp",
    "global_sentiment_pct",
    "sentiment_regime",
    "local_signal",
    "verdict",
    "detail",
]

_csv_lock = threading.Lock()


@dataclass
class BoardroomResolution:
    """Outcome of one boardroom consensus check."""

    regime: str
    global_sentiment_pct: float
    local_signal: str
    verdict: str
    execution_direction: str
    override: bool
    detail: str
    passthrough: bool = False


def boardroom_log_path() -> str:
    return str(config.BASE_DIR / "boardroom_consensus_log.csv")


def _normalize_local_signal(local_action: str) -> str:
    action = str(local_action or "").strip().upper()
    if action in {"BUY", "LONG"}:
        return "BUY"
    if action in {"SELL", "SHORT"}:
        return "SELL"
    return "CASH"


def classify_sentiment_regime(global_bullish_pct: float) -> str:
    """Map global bullish % to a rigid conviction band."""
    pct = float(global_bullish_pct)
    if pct >= EXTREME_BULLISH_MIN:
        return REGIME_EXTREME_BULLISH
    if pct >= MODERATE_BULLISH_MIN:
        return REGIME_MODERATE_BULLISH
    if NEUTRAL_MIN <= pct <= NEUTRAL_MAX:
        return REGIME_NEUTRAL
    if EXTREME_BEARISH_MAX < pct <= MODERATE_BEARISH_MAX:
        return REGIME_MODERATE_BEARISH
    if pct <= EXTREME_BEARISH_MAX:
        return REGIME_EXTREME_BEARISH
    # Gap between 30.0 and 30.1 — treat as moderate bearish per <=30 rule.
    if pct <= MODERATE_BEARISH_MAX:
        return REGIME_MODERATE_BEARISH
    return REGIME_NEUTRAL


def _resolution_from_regime(
    regime: str,
    local: str,
    global_pct: float,
) -> BoardroomResolution:
    if regime == REGIME_NEUTRAL:
        return BoardroomResolution(
            regime=regime,
            global_sentiment_pct=global_pct,
            local_signal=local,
            verdict=VERDICT_HOLD_CASH,
            execution_direction="CASH",
            override=False,
            detail=(
                f"Neutral radar band ({NEUTRAL_MIN:.1f}–{NEUTRAL_MAX:.1f}% bullish). "
                "Ignore local signal; hold cash."
            ),
        )

    if regime == REGIME_MODERATE_BULLISH:
        if local == "BUY":
            return BoardroomResolution(
                regime=regime,
                global_sentiment_pct=global_pct,
                local_signal=local,
                verdict=VERDICT_EXECUTE_LONG,
                execution_direction="LONG",
                override=False,
                detail=(
                    f"Long-only hunting mode ({MODERATE_BULLISH_MIN:.0f}–"
                    f"{EXTREME_BULLISH_MIN - 0.1:.1f}% bullish). Local BUY aligned."
                ),
            )
        if local == "SELL":
            return BoardroomResolution(
                regime=regime,
                global_sentiment_pct=global_pct,
                local_signal=local,
                verdict=VERDICT_STAND_ASIDE,
                execution_direction="CASH",
                override=False,
                detail=(
                    f"Long-only hunting mode ({MODERATE_BULLISH_MIN:.0f}–"
                    f"{EXTREME_BULLISH_MIN - 0.1:.1f}% bullish). Local SELL conflicts — "
                    "stand aside to protect capital."
                ),
            )
        return BoardroomResolution(
            regime=regime,
            global_sentiment_pct=global_pct,
            local_signal=local,
            verdict=VERDICT_HOLD_CASH,
            execution_direction="CASH",
            override=False,
            detail="Long-only hunting mode — local model has no BUY signal.",
        )

    if regime == REGIME_EXTREME_BULLISH:
        if local == "BUY":
            return BoardroomResolution(
                regime=regime,
                global_sentiment_pct=global_pct,
                local_signal=local,
                verdict=VERDICT_EXECUTE_LONG,
                execution_direction="LONG",
                override=False,
                detail=(
                    f"Extreme bullish radar (≥{EXTREME_BULLISH_MIN:.0f}%). "
                    "Local BUY aligned."
                ),
            )
        if local == "SELL":
            detail = (
                "Executive Override! Radar is overwhelmingly Bullish. "
                "Local Bot counter-trend bias bypassed."
            )
            return BoardroomResolution(
                regime=regime,
                global_sentiment_pct=global_pct,
                local_signal=local,
                verdict=VERDICT_EXECUTE_LONG_OVERRIDE,
                execution_direction="LONG",
                override=True,
                detail=detail,
            )
        return BoardroomResolution(
            regime=regime,
            global_sentiment_pct=global_pct,
            local_signal=local,
            verdict=VERDICT_HOLD_CASH,
            execution_direction="CASH",
            override=False,
            detail=(
                f"Extreme bullish radar (≥{EXTREME_BULLISH_MIN:.0f}%) but local "
                "model is flat — waiting for entry trigger."
            ),
        )

    if regime == REGIME_MODERATE_BEARISH:
        if local == "SELL":
            return BoardroomResolution(
                regime=regime,
                global_sentiment_pct=global_pct,
                local_signal=local,
                verdict=VERDICT_EXECUTE_SHORT,
                execution_direction="SHORT",
                override=False,
                detail=(
                    f"Short-only hunting mode ({EXTREME_BEARISH_MAX:.1f}–"
                    f"{MODERATE_BEARISH_MAX:.0f}% bullish). Local SELL aligned."
                ),
            )
        if local == "BUY":
            return BoardroomResolution(
                regime=regime,
                global_sentiment_pct=global_pct,
                local_signal=local,
                verdict=VERDICT_STAND_ASIDE,
                execution_direction="CASH",
                override=False,
                detail=(
                    f"Short-only hunting mode ({EXTREME_BEARISH_MAX:.1f}–"
                    f"{MODERATE_BEARISH_MAX:.0f}% bullish). Local BUY conflicts — "
                    "stand aside to protect capital."
                ),
            )
        return BoardroomResolution(
            regime=regime,
            global_sentiment_pct=global_pct,
            local_signal=local,
            verdict=VERDICT_HOLD_CASH,
            execution_direction="CASH",
            override=False,
            detail="Short-only hunting mode — local model has no SELL signal.",
        )

    # REGIME_EXTREME_BEARISH
    if local == "SELL":
        return BoardroomResolution(
            regime=regime,
            global_sentiment_pct=global_pct,
            local_signal=local,
            verdict=VERDICT_EXECUTE_SHORT,
            execution_direction="SHORT",
            override=False,
            detail=(
                f"Extreme bearish radar (≤{EXTREME_BEARISH_MAX:.0f}%). "
                "Local SELL aligned."
            ),
        )
    if local == "BUY":
        detail = (
            "Executive Override! Radar is overwhelmingly Bearish. "
            "Local Bot counter-trend bias bypassed."
        )
        return BoardroomResolution(
            regime=regime,
            global_sentiment_pct=global_pct,
            local_signal=local,
            verdict=VERDICT_EXECUTE_SHORT_OVERRIDE,
            execution_direction="SHORT",
            override=True,
            detail=detail,
        )
    return BoardroomResolution(
        regime=regime,
        global_sentiment_pct=global_pct,
        local_signal=local,
        verdict=VERDICT_HOLD_CASH,
        execution_direction="CASH",
        override=False,
        detail=(
            f"Extreme bearish radar (≤{EXTREME_BEARISH_MAX:.0f}%) but local "
            "model is flat — waiting for entry trigger."
        ),
    )


def resolve_boardroom_verdict(
    local_action: str,
    store: Optional[TradeStore] = None,
    *,
    hours: float | None = None,
    log: bool = True,
) -> BoardroomResolution:
    """Resolve local ML signal against Global Radar sentiment."""
    if not config.BOARDROOM_ENABLED:
        local = _normalize_local_signal(local_action)
        direction = "LONG" if local == "BUY" else ("SHORT" if local == "SELL" else "CASH")
        return BoardroomResolution(
            regime="DISABLED",
            global_sentiment_pct=0.0,
            local_signal=local,
            verdict="PASSTHROUGH",
            execution_direction=direction,
            override=False,
            detail="Boardroom disabled — local model direction unchanged.",
            passthrough=True,
        )

    local = _normalize_local_signal(local_action)
    db = store or TradeStore()
    window = hours if hours is not None else config.CONFLUENCE_GATE_LOOKBACK_HOURS

    try:
        sentiment = compute_global_sentiment(db, hours=window)
        global_pct = float(sentiment.get("bullish_pct", 0.0))
        total = int(sentiment.get("total", 0))

        if total <= 0:
            direction = "LONG" if local == "BUY" else ("SHORT" if local == "SELL" else "CASH")
            resolution = BoardroomResolution(
                regime="NO_FLEET_DATA",
                global_sentiment_pct=global_pct,
                local_signal=local,
                verdict="PASSTHROUGH",
                execution_direction=direction,
                override=False,
                detail="Insufficient fleet data — fail-open to local model.",
                passthrough=True,
            )
        else:
            regime = classify_sentiment_regime(global_pct)
            resolution = _resolution_from_regime(regime, local, global_pct)

        if log:
            append_boardroom_log(resolution)

        if resolution.override:
            logger.warning(
                "Boardroom %s | local=%s | radar=%.1f%% | %s",
                resolution.verdict,
                local,
                global_pct,
                resolution.detail,
            )
        elif resolution.verdict in {
            VERDICT_EXECUTE_LONG,
            VERDICT_EXECUTE_SHORT,
            VERDICT_STAND_ASIDE,
        }:
            logger.info(
                "Boardroom %s | local=%s | radar=%.1f%% | %s",
                resolution.verdict,
                local,
                global_pct,
                resolution.detail,
            )

        return resolution
    except Exception as exc:
        safe = config.sanitize_for_log(str(exc))
        direction = "LONG" if local == "BUY" else ("SHORT" if local == "SELL" else "CASH")
        logger.warning("Boardroom fail-open: %s — using local direction %s.", safe, direction)
        resolution = BoardroomResolution(
            regime="ERROR",
            global_sentiment_pct=0.0,
            local_signal=local,
            verdict="PASSTHROUGH",
            execution_direction=direction,
            override=False,
            detail=f"Fail-open after error: {safe}",
            passthrough=True,
        )
        if log:
            append_boardroom_log(resolution)
        return resolution


def append_boardroom_log(resolution: BoardroomResolution) -> None:
    """Append one row to ``boardroom_consensus_log.csv``."""
    path = boardroom_log_path()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    row = {
        "timestamp": ts,
        "global_sentiment_pct": f"{resolution.global_sentiment_pct:.2f}",
        "sentiment_regime": resolution.regime,
        "local_signal": resolution.local_signal,
        "verdict": resolution.verdict,
        "detail": resolution.detail,
    }
    with _csv_lock:
        write_header = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_BOARDROOM_CSV_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow(row)
