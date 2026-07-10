"""Tests for the cross-market Confluence Gate."""

from datetime import datetime, timedelta, timezone

import pytest

import config
import confluence_gate
from trade_store import ExternalSignal, TradeStore


def _recent_ts(minutes_ago: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_signals(store: TradeStore, rows: list[tuple[str, str]]) -> None:
    for idx, (action, symbol) in enumerate(rows):
        store.insert_external_signal_if_new(
            ExternalSignal(
                timestamp=_recent_ts(30 - idx),
                symbol=symbol,
                action=action,
                price=1.0,
                quantity=1.0,
                agent_name="FleetBot",
                content="",
            )
        )


def test_veto_long_when_global_bearish(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFLUENCE_LONG_MIN_BULLISH_PCT", 40.0)
    store = TradeStore(db_path=str(tmp_path / "gate.db"))
    _seed_signals(
        store,
        [("SELL", "SOL"), ("SHORT", "AVAX"), ("SHORT", "NEAR"), ("BUY", "BTC")],
    )
    result = confluence_gate.evaluate_confluence("LONG", store, hours=4.0)
    assert result.approved is False
    assert result.verdict == "VETO"


def test_veto_short_when_global_bullish(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFLUENCE_SHORT_MAX_BULLISH_PCT", 60.0)
    store = TradeStore(db_path=str(tmp_path / "gate2.db"))
    _seed_signals(
        store,
        [("BUY", "SOL"), ("COVER", "AVAX"), ("BUY", "NEAR"), ("SELL", "ETH")],
    )
    result = confluence_gate.evaluate_confluence("SHORT", store, hours=4.0)
    assert result.approved is False
    assert result.verdict == "VETO"


def test_approve_when_confluence_aligned(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFLUENCE_LONG_MIN_BULLISH_PCT", 40.0)
    store = TradeStore(db_path=str(tmp_path / "gate3.db"))
    _seed_signals(
        store,
        [("BUY", "SOL"), ("BUY", "AVAX"), ("SELL", "NEAR")],
    )
    result = confluence_gate.evaluate_confluence("LONG", store, hours=4.0)
    assert result.approved is True
    assert result.verdict == "APPROVE"


def test_verify_logs_decision_and_returns_bool(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFLUENCE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "CONFLUENCE_LONG_MIN_BULLISH_PCT", 40.0)
    store = TradeStore(db_path=str(tmp_path / "gate4.db"))
    _seed_signals(store, [("SELL", "SOL"), ("SHORT", "AVAX")])

    approved = confluence_gate.verify_btc_trade_safety("LONG", store=store)
    assert approved is False
    audit = store.read_radar_decisions_df()
    assert len(audit) == 1
    assert audit.iloc[0]["verdict"] == "VETO"
    assert store.count_radar_vetoes() == 1


def test_fail_open_on_store_error(monkeypatch):
    monkeypatch.setattr(config, "CONFLUENCE_GATE_ENABLED", True)

    class BrokenStore:
        def external_market_bias(self, hours=4.0):
            raise OSError("database is locked")

        def log_radar_decision(self, **kwargs):
            raise OSError("database is locked")

    assert confluence_gate.verify_btc_trade_safety("LONG", store=BrokenStore()) is True


def test_gate_readiness_matches_veto_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFLUENCE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "CONFLUENCE_LONG_MIN_BULLISH_PCT", 40.0)
    monkeypatch.setattr(config, "CONFLUENCE_SHORT_MAX_BULLISH_PCT", 60.0)
    store = TradeStore(db_path=str(tmp_path / "ready.db"))
    _seed_signals(store, [("SELL", "SOL"), ("SHORT", "AVAX"), ("BUY", "BTC")])

    snap = confluence_gate.gate_readiness_snapshot(store, hours=4.0)
    assert snap["long_allowed"] is False
    assert snap["short_allowed"] is True
    assert "risk-off" in snap["insight"].lower() or "blocked" in snap["insight"].lower()
