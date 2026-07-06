"""Tests for alerting module (Phase 3)."""

from unittest.mock import patch

import alerting
import config


def test_send_alert_logs_without_telegram(monkeypatch, caplog):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    with caplog.at_level("INFO"):
        alerting.send_alert("INFO", "Test", "Hello world", key="test1")
    assert any("Hello world" in r.message for r in caplog.records)


def test_telegram_not_configured():
    assert alerting.telegram_configured() is False


def test_rate_limit_suppresses_duplicate(monkeypatch):
    monkeypatch.setattr(config, "ALERT_RATE_LIMIT_SECONDS", 300)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    calls = {"n": 0}

    def fake_telegram(_text):
        calls["n"] += 1
        return True

    with patch.object(alerting, "_send_telegram", fake_telegram):
        alerting.send_alert("INFO", "A", "msg", key="dup")
        alerting.send_alert("INFO", "A", "msg", key="dup")
    assert calls["n"] == 0  # telegram not configured anyway

    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "123")
    with patch.object(alerting, "_send_telegram", fake_telegram):
        alerting._last_sent.clear()
        alerting.send_alert("INFO", "B", "one", key="dup2")
        alerting.send_alert("INFO", "B", "two", key="dup2")
    assert calls["n"] == 1
