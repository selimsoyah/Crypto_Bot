"""Shared pytest fixtures for the Crypto Bot test suite."""

import os
import sys

# Keep legacy tests stable — compound profile is the live default.
os.environ.setdefault("TRADING_PROFILE", "SWING")
os.environ.setdefault("ACTIVE_PROFILE", "xgboost_ml")
os.environ.setdefault("RADIO_TOWER_ENABLED", "false")
os.environ.setdefault("CONFLUENCE_GATE_ENABLED", "false")

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import bot_loop
import config
import order_execution
from bot_loop import TradingBot
from trade_store import TradeStore


@pytest.fixture()
def synthetic_candles():
    """Minimal OHLCV frame suitable for feature_factory / _iteration tests."""
    n = 250
    price = 60_000.0
    return pd.DataFrame(
        {
            "Open": [price] * n,
            "High": [price * 1.002] * n,
            "Low": [price * 0.998] * n,
            "Close": [price] * n,
            "Volume": [100.0] * n,
        }
    )


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    """TradingBot on a temp store with network/exchange calls mocked."""
    store = TradeStore(db_path=str(tmp_path / "test.db"))
    tb = TradingBot(store=store)
    tb.session_id = "testsession"
    monkeypatch.setattr(config, "USE_POST_ONLY_MAKER", True)
    monkeypatch.setattr(config, "ENABLE_LONG_INVERSION", False)
    monkeypatch.setattr(config, "USE_EMA50_TREND_GATE", False)
    monkeypatch.setattr(config, "POST_SL_COOLDOWN_BARS", 0)
    monkeypatch.setattr(bot_loop.time, "sleep", lambda *_: None)
    tb._last_test_price = 60_000.0

    def fake_maker_limit(side, quantity, reduce_only, book=None):
        return order_execution.MakerOrderResult(
            success=True,
            fill_price=tb._last_test_price,
            order={"avgPrice": str(tb._last_test_price)},
        )

    monkeypatch.setattr(tb, "_execute_maker_limit", fake_maker_limit)
    monkeypatch.setattr(
        tb,
        "_fetch_book",
        lambda: order_execution.BookTicker(
            bid=tb._last_test_price - 0.5,
            ask=tb._last_test_price + 0.5,
        ),
    )
    monkeypatch.setattr(tb, "_get_usdt_balance", lambda: 5_000.0)
    monkeypatch.setattr(tb, "_get_total_wallet_balance", lambda: 5_000.0)
    monkeypatch.setattr(tb, "_flatten_exchange_orphans", lambda *a, **k: False)
    tb.risk.begin_session(5_000.0)
    return tb


def drive_iteration(bot_obj, monkeypatch, price, prob_long, prob_short):
    """Run one real ``_iteration`` with market and model stubbed."""
    bot_obj._last_test_price = price
    candles = pd.DataFrame(
        {
            "Close": [price] * 5,
            "Timestamp": [pd.Timestamp("2025-06-01 12:00:00+00:00")] * 5,
        }
    )
    monkeypatch.setattr(
        bot_loop.data_pipeline, "fetch_latest_candles", lambda: candles
    )
    monkeypatch.setattr(
        bot_loop.model_brain,
        "predict_latest",
        lambda model, c: {
            "prob_long": prob_long,
            "prob_short": prob_short,
            "prob_cash": max(0.0, 1.0 - prob_long - prob_short),
            "trend": 0.0,
        },
    )

    def _fake_live_features(df):
        n = len(df)
        return pd.DataFrame({"atr_pct": [config.RISK_ATR_BASELINE_PCT] * n})

    import feature_factory

    monkeypatch.setattr(
        feature_factory, "compute_live_features", _fake_live_features
    )
    bot_obj._iteration()
