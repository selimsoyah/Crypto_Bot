"""
confluence_gate.py
==================
Cross-market Confluence Gate — uses Radio Tower fleet signals as a macro
filter for local BTC/USDT entries.

READ-ONLY relative to external capital: this module only vetoes or approves
local execution; it never places orders on ai4trade.ai or third-party accounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
from trade_store import TradeStore

logger = config.configure_logging(__name__)

_BULLISH_ACTIONS = frozenset({"BUY", "COVER"})
_BEARISH_ACTIONS = frozenset({"SELL", "SHORT"})


@dataclass
class ConfluenceCheck:
    """Outcome of one Confluence Gate evaluation."""

    approved: bool
    intended_action: str
    global_bullish_pct: float
    verdict: str
    detail: str


def _normalize_local_action(local_action: str) -> str:
    action = str(local_action or "").strip().upper()
    if action in {"BUY", "LONG"}:
        return "LONG"
    if action in {"SELL", "SHORT"}:
        return "SHORT"
    return action


def compute_global_sentiment(
    store: TradeStore,
    *,
    hours: float | None = None,
) -> dict[str, float | int | str]:
    """Aggregate bullish vs bearish share across all symbols in the lookback window."""
    window = hours if hours is not None else config.CONFLUENCE_GATE_LOOKBACK_HOURS
    return store.external_market_bias(hours=window)


def gate_readiness_snapshot(
    store: TradeStore,
    *,
    hours: float | None = None,
) -> dict[str, object]:
    """Human-readable gate state — uses the same rules as ``verify_btc_trade_safety``."""
    window = hours if hours is not None else config.CONFLUENCE_GATE_LOOKBACK_HOURS
    sentiment = compute_global_sentiment(store, hours=window)
    bullish_pct = float(sentiment.get("bullish_pct", 0.0))
    bearish_pct = float(sentiment.get("bearish_pct", 0.0))
    bullish = int(sentiment.get("bullish", 0))
    bearish = int(sentiment.get("bearish", 0))
    total = int(sentiment.get("total", 0))
    long_min = config.CONFLUENCE_LONG_MIN_BULLISH_PCT
    short_max = config.CONFLUENCE_SHORT_MAX_BULLISH_PCT
    enabled = config.CONFLUENCE_GATE_ENABLED

    if not enabled:
        return {
            "gate_enabled": False,
            "lookback_hours": window,
            "bullish_pct": bullish_pct,
            "bearish_pct": bearish_pct,
            "bullish_count": bullish,
            "bearish_count": bearish,
            "total_ops": total,
            "long_allowed": True,
            "short_allowed": True,
            "long_status": "Gate OFF",
            "short_status": "Gate OFF",
            "insight": "Confluence Gate is disabled — local XGBoost model decides BTC entries alone.",
            "insight_tone": "info",
        }

    if total <= 0:
        return {
            "gate_enabled": True,
            "lookback_hours": window,
            "bullish_pct": bullish_pct,
            "bearish_pct": bearish_pct,
            "bullish_count": bullish,
            "bearish_count": bearish,
            "total_ops": total,
            "long_allowed": True,
            "short_allowed": True,
            "long_status": "PASS (fail-open)",
            "short_status": "PASS (fail-open)",
            "insight": (
                "No fleet operations in the gate window yet. The bot fail-opens and "
                "allows BTC entries until Radio Tower collects cross-market data."
            ),
            "insight_tone": "info",
        }

    long_allowed = bullish_pct >= long_min
    short_allowed = bullish_pct <= short_max

    if long_allowed:
        long_status = f"PASS (≥{long_min:.0f}% bullish)"
    else:
        long_status = f"BLOCKED (<{long_min:.0f}% bullish)"

    if short_allowed:
        short_status = f"PASS (≤{short_max:.0f}% bullish)"
    else:
        short_status = f"BLOCKED (>{short_max:.0f}% bullish)"

    if not long_allowed and not short_allowed:
        insight = (
            f"Fleet sentiment is split/neutral at {bullish_pct:.1f}% bullish in the last "
            f"{window:.0f}h. BTC LONG is blocked (risk-off); BTC SHORT is blocked (risk-on). "
            "Wait for clearer cross-market direction."
        )
        tone = "warning"
    elif not long_allowed:
        insight = (
            f"Altcoin fleet is risk-off ({bullish_pct:.1f}% bullish, {bearish} sell/short vs "
            f"{bullish} buy/cover in {window:.0f}h). BTC LONG signals from your local model "
            f"will be vetoed until bullish share reaches {long_min:.0f}%. SHORT still allowed."
        )
        tone = "warning"
    elif not short_allowed:
        insight = (
            f"Altcoin fleet is risk-on ({bullish_pct:.1f}% bullish in {window:.0f}h). BTC SHORT "
            f"signals will be vetoed above {short_max:.0f}% bullish. LONG entries have confluence."
        )
        tone = "warning"
    else:
        insight = (
            f"Cross-market confluence is aligned ({bullish_pct:.1f}% bullish in {window:.0f}h). "
            f"Both BTC LONG (≥{long_min:.0f}%) and SHORT (≤{short_max:.0f}%) pass the gate if your "
            "local model fires."
        )
        tone = "success"

    return {
        "gate_enabled": True,
        "lookback_hours": window,
        "bullish_pct": bullish_pct,
        "bearish_pct": bearish_pct,
        "bullish_count": bullish,
        "bearish_count": bearish,
        "total_ops": total,
        "long_allowed": long_allowed,
        "short_allowed": short_allowed,
        "long_status": long_status,
        "short_status": short_status,
        "insight": insight,
        "insight_tone": tone,
    }


def evaluate_confluence(
    local_action: str,
    store: TradeStore,
    *,
    hours: float | None = None,
) -> ConfluenceCheck:
    """Return approve/veto decision without persisting (used by tests)."""
    intended = _normalize_local_action(local_action)
    sentiment = compute_global_sentiment(store, hours=hours)
    bullish_pct = float(sentiment.get("bullish_pct", 0.0))
    total = int(sentiment.get("total", 0))

    if total <= 0:
        return ConfluenceCheck(
            approved=True,
            intended_action=intended,
            global_bullish_pct=bullish_pct,
            verdict="APPROVE",
            detail="Insufficient global fleet data — fail-open to local model.",
        )

    long_min = config.CONFLUENCE_LONG_MIN_BULLISH_PCT
    short_max = config.CONFLUENCE_SHORT_MAX_BULLISH_PCT

    if intended == "LONG" and bullish_pct < long_min:
        return ConfluenceCheck(
            approved=False,
            intended_action=intended,
            global_bullish_pct=bullish_pct,
            verdict="VETO",
            detail=(
                f"Global altcoin/crypto fleet risk-off — bullish {bullish_pct:.1f}% "
                f"< {long_min:.0f}% minimum for BTC LONG confluence."
            ),
        )

    if intended == "SHORT" and bullish_pct > short_max:
        return ConfluenceCheck(
            approved=False,
            intended_action=intended,
            global_bullish_pct=bullish_pct,
            verdict="VETO",
            detail=(
                f"Global fleet risk-on — bullish {bullish_pct:.1f}% "
                f"> {short_max:.0f}% maximum for BTC SHORT confluence."
            ),
        )

    return ConfluenceCheck(
        approved=True,
        intended_action=intended,
        global_bullish_pct=bullish_pct,
        verdict="APPROVE",
        detail=(
            f"Cross-market confluence aligned — global bullish {bullish_pct:.1f}% "
            f"supports BTC {intended}."
        ),
    )


def verify_btc_trade_safety(
    local_action: str,
    store: Optional[TradeStore] = None,
) -> bool:
    """Return True when cross-market sentiment allows the intended BTC trade.

  On database or analysis errors the gate **fail-opens** (returns True) so the
  live loop never crashes and the local model retains authority.
    """
    if not config.CONFLUENCE_GATE_ENABLED:
        return True

    intended = _normalize_local_action(local_action)
    if intended not in {"LONG", "SHORT"}:
        return True

    db = store or TradeStore()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    try:
        result = evaluate_confluence(intended, db)
        db.log_radar_decision(
            timestamp=ts,
            intended_action=result.intended_action,
            global_sentiment_pct=result.global_bullish_pct,
            verdict=result.verdict,
            detail=result.detail,
        )
        if result.approved:
            logger.info(
                "Confluence Gate APPROVE | BTC %s | global bullish %.1f%% | %s",
                result.intended_action,
                result.global_bullish_pct,
                result.detail,
            )
        else:
            logger.warning(
                "Confluence Gate VETO | BTC %s | global bullish %.1f%% | %s",
                result.intended_action,
                result.global_bullish_pct,
                result.detail,
            )
        return result.approved
    except Exception as exc:
        safe = config.sanitize_for_log(str(exc))
        logger.warning(
            "Confluence Gate fail-open (DB/analysis error): %s — approving BTC %s.",
            safe,
            intended,
        )
        try:
            db.log_radar_decision(
                timestamp=ts,
                intended_action=intended,
                global_sentiment_pct=0.0,
                verdict="APPROVE",
                detail=f"Fail-open after error: {safe}",
            )
        except Exception:
            pass
        return True
