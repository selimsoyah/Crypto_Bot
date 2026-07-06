"""Phase 1 backtest cost and risk-metric sanity checks."""

import numpy as np
import pandas as pd
import pytest

import config
import model_brain


def _mini_test_df(n: int = 200) -> pd.DataFrame:
    close = np.linspace(60_000, 61_000, n)
    return pd.DataFrame(
        {
            "Close": close,
            "High": close * 1.002,
            "Low": close * 0.998,
            "price_vs_ema200": np.linspace(-0.01, 0.01, n),
        }
    )


def test_round_trip_cost_is_positive():
    cost = model_brain._round_trip_cost_pct()
    assert cost > 0
    expected = 2 * (config.BACKTEST_TAKER_FEE + config.BACKTEST_SLIPPAGE_BPS / 10_000)
    assert cost == pytest.approx(expected)


def test_net_pnl_worse_than_gross_with_costs():
    n = 300
    proba = np.zeros((n, 3))
    proba[:, config.LABEL_LONG] = 0.99  # always long signal
    df = _mini_test_df(n)

    gross = model_brain._run_backtest(
        df, proba, 0.5, 0.99, config.TAKE_PROFIT_PCT, config.STOP_LOSS_PCT,
        config.FORWARD_WINDOW, False, 0.0, 0.0,
    )
    net = model_brain._run_backtest(
        df, proba, 0.5, 0.99, config.TAKE_PROFIT_PCT, config.STOP_LOSS_PCT,
        config.FORWARD_WINDOW, False,
        config.BACKTEST_TAKER_FEE, config.BACKTEST_SLIPPAGE_BPS,
    )
    if gross.n_trades > 0 and net.n_trades > 0:
        assert net.strategy_net_profit_pct <= gross.strategy_net_profit_pct


def test_trend_filter_reduces_long_signals_in_downtrend():
    n = 50
    proba = np.zeros((n, 3))
    proba[:, config.LABEL_LONG] = 0.99
    df = _mini_test_df(n)
    df["price_vs_ema200"] = -0.05  # below EMA200

    off = model_brain._run_backtest(
        df, proba, 0.5, 0.99, config.TAKE_PROFIT_PCT, config.STOP_LOSS_PCT,
        24, False, 0.0, 0.0,
    )
    on = model_brain._run_backtest(
        df, proba, 0.5, 0.99, config.TAKE_PROFIT_PCT, config.STOP_LOSS_PCT,
        24, True, 0.0, 0.0,
    )
    assert on.n_trades <= off.n_trades
