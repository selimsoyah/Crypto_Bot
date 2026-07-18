"""Tests for agent_boardroom executive override logic."""

from datetime import datetime, timedelta, timezone

import pytest

import agent_boardroom
import config
from trade_store import ExternalSignal, TradeStore


def _recent_ts(minutes_ago: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_bullish(store: TradeStore, n: int = 10) -> None:
    for i in range(n):
        store.insert_external_signal_if_new(
            ExternalSignal(
                timestamp=_recent_ts(30 - i),
                symbol="SOL",
                action="BUY",
                price=1.0,
                quantity=1.0,
                agent_name="FleetBot",
                content="",
            )
        )


def _seed_bearish(store: TradeStore, n: int = 10) -> None:
    for i in range(n):
        store.insert_external_signal_if_new(
            ExternalSignal(
                timestamp=_recent_ts(30 - i),
                symbol="SOL",
                action="SHORT",
                price=1.0,
                quantity=1.0,
                agent_name="FleetBot",
                content="",
            )
        )


def _seed_mixed(store: TradeStore, bullish: int, bearish: int) -> None:
    idx = 0
    for _ in range(bullish):
        store.insert_external_signal_if_new(
            ExternalSignal(
                timestamp=_recent_ts(30 - idx),
                symbol="SOL",
                action="BUY",
                price=1.0,
                quantity=1.0,
                agent_name="FleetBot",
                content="",
            )
        )
        idx += 1
    for _ in range(bearish):
        store.insert_external_signal_if_new(
            ExternalSignal(
                timestamp=_recent_ts(30 - idx),
                symbol="AVAX",
                action="SHORT",
                price=1.0,
                quantity=1.0,
                agent_name="FleetBot",
                content="",
            )
        )
        idx += 1


def test_classify_regime_boundaries():
    assert agent_boardroom.classify_sentiment_regime(85.0) == agent_boardroom.REGIME_EXTREME_BULLISH
    assert agent_boardroom.classify_sentiment_regime(84.9) == agent_boardroom.REGIME_MODERATE_BULLISH
    assert agent_boardroom.classify_sentiment_regime(70.0) == agent_boardroom.REGIME_MODERATE_BULLISH
    assert agent_boardroom.classify_sentiment_regime(69.9) == agent_boardroom.REGIME_NEUTRAL
    assert agent_boardroom.classify_sentiment_regime(30.1) == agent_boardroom.REGIME_NEUTRAL
    assert agent_boardroom.classify_sentiment_regime(30.0) == agent_boardroom.REGIME_MODERATE_BEARISH
    assert agent_boardroom.classify_sentiment_regime(15.1) == agent_boardroom.REGIME_MODERATE_BEARISH
    assert agent_boardroom.classify_sentiment_regime(15.0) == agent_boardroom.REGIME_EXTREME_BEARISH


def test_moderate_bullish_long_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOARDROOM_ENABLED", True)
    store = TradeStore(db_path=str(tmp_path / "br.db"))
    _seed_mixed(store, bullish=8, bearish=2)  # 80% bullish

    buy = agent_boardroom.resolve_boardroom_verdict("LONG", store=store, log=False)
    assert buy.verdict == agent_boardroom.VERDICT_EXECUTE_LONG
    assert buy.execution_direction == "LONG"

    sell = agent_boardroom.resolve_boardroom_verdict("SHORT", store=store, log=False)
    assert sell.verdict == agent_boardroom.VERDICT_STAND_ASIDE
    assert sell.execution_direction == "CASH"


def test_extreme_bullish_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOARDROOM_ENABLED", True)
    store = TradeStore(db_path=str(tmp_path / "br2.db"))
    _seed_bullish(store, n=10)

    res = agent_boardroom.resolve_boardroom_verdict("SHORT", store=store, log=False)
    assert res.verdict == agent_boardroom.VERDICT_EXECUTE_LONG_OVERRIDE
    assert res.execution_direction == "LONG"
    assert res.override is True
    assert "Executive Override" in res.detail


def test_extreme_bearish_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOARDROOM_ENABLED", True)
    store = TradeStore(db_path=str(tmp_path / "br3.db"))
    _seed_bearish(store, n=10)

    res = agent_boardroom.resolve_boardroom_verdict("LONG", store=store, log=False)
    assert res.verdict == agent_boardroom.VERDICT_EXECUTE_SHORT_OVERRIDE
    assert res.execution_direction == "SHORT"
    assert res.override is True
    assert "Executive Override" in res.detail


def test_neutral_holds_cash(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOARDROOM_ENABLED", True)
    store = TradeStore(db_path=str(tmp_path / "br4.db"))
    _seed_mixed(store, bullish=5, bearish=5)  # 50%

    res = agent_boardroom.resolve_boardroom_verdict("LONG", store=store, log=False)
    assert res.verdict == agent_boardroom.VERDICT_HOLD_CASH
    assert res.execution_direction == "CASH"


def test_moderate_bearish_short_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOARDROOM_ENABLED", True)
    store = TradeStore(db_path=str(tmp_path / "br5.db"))
    _seed_mixed(store, bullish=2, bearish=8)  # 20% bullish

    sell = agent_boardroom.resolve_boardroom_verdict("SHORT", store=store, log=False)
    assert sell.verdict == agent_boardroom.VERDICT_EXECUTE_SHORT
    assert sell.execution_direction == "SHORT"

    buy = agent_boardroom.resolve_boardroom_verdict("LONG", store=store, log=False)
    assert buy.verdict == agent_boardroom.VERDICT_STAND_ASIDE
    assert buy.execution_direction == "CASH"


def test_boardroom_log_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BOARDROOM_ENABLED", True)
    log_path = tmp_path / "boardroom_consensus_log.csv"
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    store = TradeStore(db_path=str(tmp_path / "br6.db"))
    _seed_bullish(store, n=10)

    agent_boardroom.resolve_boardroom_verdict("SHORT", store=store, log=True)

    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "EXECUTE LONG (OVERRIDE)" in text
    assert "Executive Override" in text
