"""Tests for execution venue validation (Phase 3)."""

import pytest

import config


def test_execution_venue_defaults_to_testnet(monkeypatch):
    monkeypatch.setattr(config, "EXECUTION_VENUE", "TESTNET")
    assert config.execution_is_live() is False
    assert "TESTNET" in config.execution_banner_text()


def test_invalid_execution_venue_rejected(monkeypatch):
    monkeypatch.setattr(config, "EXECUTION_VENUE", "SANDBOX")
    errors = config.validate_execution_config()
    assert any("EXECUTION_VENUE" in e for e in errors)


def test_live_requires_credentials(monkeypatch):
    monkeypatch.setattr(config, "EXECUTION_VENUE", "LIVE")
    monkeypatch.setattr(config, "API_KEY", "your_binance_testnet_key")
    monkeypatch.setattr(config, "SECRET_KEY", "your_binance_testnet_secret")
    errors = config.validate_execution_config()
    assert any("LIVE" in e for e in errors)


def test_sanitize_for_log_redacts_secrets(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "super_secret_key_12345")
    monkeypatch.setattr(config, "SECRET_KEY", "super_secret_sec_67890")
    text = "Error with key super_secret_key_12345 in request"
    assert "super_secret_key_12345" not in config.sanitize_for_log(text)
    assert "[REDACTED]" in config.sanitize_for_log(text)


def test_secrets_leak_detected(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "real_key_abc123")
    monkeypatch.setattr(config, "SECRET_KEY", "placeholder")
    assert config.secrets_leak_detected("token real_key_abc123 leaked") is True
    assert config.secrets_leak_detected("no secrets here") is False
