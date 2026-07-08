from __future__ import annotations

import pandas as pd

import bot_loop
import config


def _darvas_breakout_frame(close: float) -> pd.DataFrame:
    prev = pd.date_range("2026-06-01 00:00", periods=96, freq="15min", tz="UTC")
    curr = pd.date_range("2026-06-02 00:00", periods=10, freq="15min", tz="UTC")
    ts = prev.append(curr)
    n = len(ts)
    frame = pd.DataFrame(
        {
            "Timestamp": ts,
            "Open": [100.0] * n,
            "High": [110.0] * n,
            "Low": [90.0] * n,
            "Close": [100.0] * n,
            "Volume": [100.0] * n,
        }
    )
    frame.loc[frame.index[-1], "Close"] = close
    frame.loc[frame.index[-1], "Volume"] = 180.0
    return frame


def test_darvas_profile_opens_long_on_breakout(bot, monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_PROFILE", "darvas_box")
    monkeypatch.setattr(config, "is_darvas_box_profile", lambda: True)
    monkeypatch.setattr(config, "is_xgboost_ml_profile", lambda: False)
    monkeypatch.setattr(config, "BOX_STOP_BUFFER_PCT", 0.0005)
    monkeypatch.setattr(config, "BOX_RISK_REWARD_RATIO", 2.0)

    candles = _darvas_breakout_frame(close=111.0)
    monkeypatch.setattr(bot_loop.data_pipeline, "fetch_latest_candles", lambda: candles)
    monkeypatch.setattr(
        bot_loop.model_brain,
        "predict_latest",
        lambda *_a, **_k: {
            "prob_long": 0.0,
            "prob_short": 0.0,
            "prob_cash": 1.0,
            "trend": 0.0,
        },
    )

    import feature_factory

    monkeypatch.setattr(
        feature_factory,
        "compute_live_features",
        lambda df: pd.DataFrame({"atr_pct": [config.RISK_ATR_BASELINE_PCT] * len(df)}),
    )

    bot._iteration()
    assert bot.state.position is not None
    assert bot.state.position.side == "LONG"
    assert bot._active_box is not None
    assert bot._active_box.valid is True
    assert bot._active_box.top == 110.0
    assert bot._active_box.bottom == 90.0
    assert bot._active_box.middle_line == 100.0
    assert bot._active_box.breakout == "LONG"
    # Box-height RR brackets are derived from previous-day boundaries, not the mocked ticker.
    assert bot.state.position.take_profit_price > bot._active_box.top
    assert bot.state.position.stop_loss_price < bot._active_box.bottom
