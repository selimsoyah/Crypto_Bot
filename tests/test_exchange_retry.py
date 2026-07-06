"""Tests for exchange API retry wrapper (Phase 3)."""

import pytest

import config
import exchange_client


def test_call_with_retry_succeeds_on_second_attempt(monkeypatch):
    monkeypatch.setattr(config, "EXCHANGE_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(config, "EXCHANGE_RETRY_BASE_DELAY", 0.01)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("timeout")
        return "ok"

    assert exchange_client.call_with_retry(flaky, label="test") == "ok"
    assert calls["n"] == 2


def test_call_with_retry_raises_after_exhausted(monkeypatch):
    monkeypatch.setattr(config, "EXCHANGE_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(config, "EXCHANGE_RETRY_BASE_DELAY", 0.01)

    def always_fail():
        raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="down"):
        exchange_client.call_with_retry(always_fail, label="test")


def test_build_execution_client_testnet(monkeypatch):
    monkeypatch.setattr(config, "EXECUTION_VENUE", "TESTNET")
    client = exchange_client.build_execution_client()
    assert "testnet" in client.FUTURES_URL


def test_build_execution_client_live(monkeypatch):
    monkeypatch.setattr(config, "EXECUTION_VENUE", "LIVE")
    client = exchange_client.build_execution_client()
    assert "testnet" not in client.FUTURES_URL.lower() or "fapi.binance.com" in client.FUTURES_URL
