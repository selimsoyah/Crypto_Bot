"""Tests for post-only execution helpers and audit-parity gates."""

from unittest.mock import MagicMock

import pytest

import config
import order_execution


def test_spread_gate_blocks_wide_book():
    book = order_execution.BookTicker(bid=100.0, ask=100.05)  # 0.05% spread
    result = order_execution.check_spread_gate(book, max_spread_pct=0.0002)
    assert not result.allowed
    assert "Spread too wide" in result.reason


def test_spread_gate_allows_tight_book():
    book = order_execution.BookTicker(bid=60_000.0, ask=60_001.0)
    result = order_execution.check_spread_gate(book, max_spread_pct=0.0002)
    assert result.allowed
    assert result.spread_pct < 0.0002


def test_maker_limit_price_passive_side():
    book = order_execution.BookTicker(bid=60_000.0, ask=60_010.0)
    assert order_execution.maker_limit_price("BUY", book) == 60_000.0
    assert order_execution.maker_limit_price("SELL", book) == 60_010.0


def test_bars_elapsed_counts_15m_interval():
    elapsed = order_execution.bars_elapsed(
        "2025-01-01 00:00:00+00:00",
        "2025-01-01 04:00:00+00:00",
        "15m",
    )
    assert elapsed == 16


def test_place_and_wait_post_only_immediate_fill(monkeypatch):
    client = MagicMock()
    client.futures_create_order.return_value = {
        "orderId": 99,
        "status": "FILLED",
        "avgPrice": "60000",
    }
    book = order_execution.BookTicker(bid=60_000.0, ask=60_001.0)
    monkeypatch.setattr(config, "USE_POST_ONLY_MAKER", True)

    result = order_execution.place_and_wait_post_only(
        client,
        symbol="BTCUSDT",
        side="BUY",
        quantity=0.01,
        reduce_only=False,
        book=book,
        tick_size=0.1,
        price_precision=2,
    )
    assert result.success
    assert result.fill_price == 60_000.0
    kwargs = client.futures_create_order.call_args.kwargs
    assert kwargs["type"] == "LIMIT"
    assert kwargs["timeInForce"] == "GTX"
    assert kwargs["price"] == 60_000.0


def test_place_and_wait_post_only_reject(monkeypatch):
    client = MagicMock()
    client.futures_create_order.side_effect = Exception("APIError -5022: Post Only")
    book = order_execution.BookTicker(bid=60_000.0, ask=60_001.0)
    monkeypatch.setattr(config, "USE_POST_ONLY_MAKER", True)

    result = order_execution.place_and_wait_post_only(
        client,
        symbol="BTCUSDT",
        side="BUY",
        quantity=0.01,
        reduce_only=False,
        book=book,
    )
    assert not result.success
    assert "Post-only order rejected" in result.reason
