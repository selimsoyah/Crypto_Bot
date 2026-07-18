"""Tests for the read-only Radio Tower listener."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import radio_tower
from trade_store import ExternalSignal, TradeStore


def _recent_ts(minutes_ago: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


SAMPLE_FEED = {
    "signals": [
        {
            "executed_at": _recent_ts(5),
            "symbol": "SOL",
            "side": "buy",
            "entry_price": 77.605,
            "quantity": 10.5,
            "agent_name": "ClaudeAdvancedBot",
            "content": "SOL breakout long",
        },
        {
            "executed_at": _recent_ts(6),
            "symbol": "NEAR",
            "side": "sell",
            "entry_price": 1.8875,
            "quantity": 5500.0,
            "agent_name": "cliptap",
            "content": "NEAR stop loss",
        },
    ]
}


def test_normalize_action_maps_api_sides():
    assert radio_tower.normalize_action("buy") == "BUY"
    assert radio_tower.normalize_action("short") == "SHORT"


def test_parse_feed_signal_extracts_fields():
    parsed = radio_tower.parse_feed_signal(SAMPLE_FEED["signals"][0])
    assert parsed is not None
    assert parsed.symbol == "SOL"
    assert parsed.action == "BUY"
    assert parsed.price == pytest.approx(77.605)
    assert parsed.agent_name == "ClaudeAdvancedBot"


def test_insert_external_signal_dedupes_timestamp_symbol(tmp_path):
    store = TradeStore(db_path=str(tmp_path / "tower.db"))
    row = ExternalSignal(
        timestamp="2026-07-10T14:35:32Z",
        symbol="BTC",
        action="BUY",
        price=60000.0,
        quantity=0.1,
        agent_name="AgentA",
        content="test",
    )
    assert store.insert_external_signal_if_new(row) is True
    assert store.insert_external_signal_if_new(row) is False
    assert store.count_external_signals() == 1


def test_ingest_feed_items_inserts_new_rows(tmp_path):
    store = TradeStore(db_path=str(tmp_path / "tower2.db"))
    inserted = radio_tower.ingest_feed_items(store, SAMPLE_FEED["signals"])
    assert inserted == 2
    assert store.count_external_signals() == 2
    assert store.top_external_signal_symbol() in {"SOL", "NEAR"}


def test_external_market_bias_bullish(tmp_path):
    store = TradeStore(db_path=str(tmp_path / "tower3.db"))
    for idx, (sym, action) in enumerate((("BTC", "BUY"), ("ETH", "BUY"), ("SOL", "SELL"))):
        store.insert_external_signal_if_new(
            ExternalSignal(
                timestamp=_recent_ts(10 - idx),
                symbol=sym,
                action=action,
                price=1.0,
                quantity=1.0,
                agent_name="A",
                content="",
            )
        )
    bias = store.external_market_bias(hours=24.0)
    assert bias["label"] == "BULLISH"


@patch("radio_tower.fetch_operation_feed", return_value=SAMPLE_FEED["signals"])
def test_radio_tower_poll_once(mock_fetch, tmp_path):
    store = TradeStore(db_path=str(tmp_path / "tower4.db"))
    listener = radio_tower.RadioTowerListener(store, poll_seconds=60, retry_seconds=1)
    count = listener.poll_once()
    assert count == 2
    mock_fetch.assert_called_once()
