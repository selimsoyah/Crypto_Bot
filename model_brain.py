"""
model_brain.py
==============
Multi-class XGBoost model training, chronological evaluation, and a directional
event-driven backtest for the BTC/USDT futures strategy.

The model is a 3-class classifier (0=CASH, 1=SHORT, 2=LONG) trained with
``objective='multi:softprob'``. The backtester opens **long or short** trades
depending on the per-class probabilities, allowing the strategy to profit in
both rising and falling markets.

Run directly to train on cached historical data, persist the model artifact to
``config.MODEL_PATH`` and print an out-of-sample metrics report:

    python model_brain.py
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Final, Optional

import numpy as np
import pandas as pd

import config
from feature_factory import (
    TARGET_COLUMN,
    build_feature_matrix,
    compute_live_features,
    feature_columns_for,
    use_feature_variant,
)

logger = config.configure_logging(__name__)

TRAIN_FRACTION: Final[float] = 0.80
HOURS_PER_YEAR: Final[float] = 8760.0


@dataclass
class TradeSim:
    """One simulated round-trip trade from the event-driven backtester."""

    direction: str  # LONG | SHORT
    gross_return: float
    net_return: float
    outcome: str  # TP | SL | TIMEOUT
    bars_held: int
    entry_idx: int


@dataclass
class BacktestResult:
    """Rich backtest output including costs, risk metrics, and per-trade log."""

    strategy_net_profit_pct: float
    buy_hold_net_profit_pct: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    n_trades: int
    n_long: int
    n_short: int
    long_win_rate: float
    short_win_rate: float
    overall_win_rate: float
    equity_curve: np.ndarray
    trades: list[TradeSim] = field(default_factory=list)
    total_cost_pct: float = 0.0


@dataclass
class BacktestMetrics:
    """Container for out-of-sample evaluation results."""

    oos_accuracy: float
    long_precision: float
    short_precision: float
    strategy_net_profit_pct: float
    buy_hold_net_profit_pct: float
    max_drawdown_pct: float
    n_trades: int
    n_long: int
    n_short: int
    n_test_rows: int
    long_threshold: float = config.LONG_PROBABILITY_THRESHOLD
    short_threshold: float = config.SHORT_PROBABILITY_THRESHOLD

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Threshold persistence (per direction)                                       #
# --------------------------------------------------------------------------- #
def save_thresholds(
    long_threshold: float,
    short_threshold: float,
    path: str = config.THRESHOLD_PATH,
) -> None:
    """Persist the data-driven per-direction thresholds to a sidecar JSON file."""
    try:
        with open(path, "w") as fh:
            json.dump(
                {
                    "long_threshold": float(long_threshold),
                    "short_threshold": float(short_threshold),
                },
                fh,
                indent=2,
            )
        logger.info(
            "Saved thresholds (long=%.3f, short=%.3f) to %s",
            long_threshold,
            short_threshold,
            path,
        )
    except Exception as exc:  # pragma: no cover - disk issues
        logger.warning("Could not persist thresholds to %s (%s).", path, exc)


def load_thresholds(path: str = config.THRESHOLD_PATH) -> tuple[float, float]:
    """Load per-direction thresholds, falling back to the config defaults."""
    long_thr = config.LONG_PROBABILITY_THRESHOLD
    short_thr = config.SHORT_PROBABILITY_THRESHOLD
    if os.path.exists(path):
        try:
            with open(path) as fh:
                data = json.load(fh)
            long_thr = float(data.get("long_threshold", long_thr))
            short_thr = float(data.get("short_threshold", short_thr))
        except Exception as exc:  # pragma: no cover - corrupt file
            logger.warning("Could not read thresholds from %s (%s).", path, exc)
    return long_thr, short_thr


# --------------------------------------------------------------------------- #
# Trend regime helpers                                                        #
# --------------------------------------------------------------------------- #
def _trend_array(feature_df: pd.DataFrame, n: int) -> np.ndarray:
    """Return the EMA200 trend column as an array (zeros if unavailable)."""
    if "price_vs_ema200" in feature_df.columns:
        return feature_df["price_vs_ema200"].to_numpy(dtype=np.float64)
    return np.zeros(n, dtype=np.float64)


def decide_direction(
    prob_long: float,
    prob_short: float,
    trend: float,
    long_threshold: float,
    short_threshold: float,
    use_trend_filter: Optional[bool] = None,
) -> str:
    """Return ``"LONG"``, ``"SHORT"`` or ``"CASH"`` for a single observation.

    Applies the per-direction probability thresholds and, when enabled, the
    EMA200 regime filter (longs only above EMA200, shorts only below). When both
    directions qualify, the higher-probability side wins.

    ``use_trend_filter`` overrides ``config.USE_TREND_FILTER`` when set (used by
    the Phase 1 trend-filter A/B backtest).
    """
    trend_on = config.USE_TREND_FILTER if use_trend_filter is None else use_trend_filter
    allow_long = prob_long > long_threshold and (not trend_on or trend > 0)
    allow_short = prob_short > short_threshold and (not trend_on or trend < 0)
    if allow_long and allow_short:
        return "LONG" if prob_long >= prob_short else "SHORT"
    if allow_long:
        return "LONG"
    if allow_short:
        return "SHORT"
    return "CASH"


# --------------------------------------------------------------------------- #
# Model factory                                                               #
# --------------------------------------------------------------------------- #
def _build_model():
    """Instantiate the pre-configured multi-class XGBoost classifier.

    Uses ``objective='multi:softprob'`` for 3-class probability output. The
    number of classes (3) is inferred from the label set by the scikit-learn
    wrapper. Class imbalance is handled via per-sample weights at ``fit`` time.
    """
    from xgboost import XGBClassifier

    return XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_lambda=1.0,
        objective="multi:softprob",
        num_class=config.NUM_CLASSES,
        eval_metric="mlogloss",
        tree_method="hist",
        n_jobs=-1,
        random_state=42,
    )


def _fit_balanced(model, x: pd.DataFrame, y: pd.Series):
    """Fit ``model`` with balanced per-sample weights to offset class imbalance."""
    from sklearn.utils.class_weight import compute_sample_weight

    sample_weight = compute_sample_weight(class_weight="balanced", y=y)
    model.fit(x, y, sample_weight=sample_weight)
    return model


def chronological_split(
    matrix: pd.DataFrame,
    train_fraction: float = TRAIN_FRACTION,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split ``matrix`` by time into train/test without shuffling (no leakage)."""
    split_idx = int(len(matrix) * train_fraction)
    train = matrix.iloc[:split_idx].reset_index(drop=True)
    test = matrix.iloc[split_idx:].reset_index(drop=True)
    return train, test


def _max_drawdown(equity_curve: np.ndarray) -> float:
    """Return the maximum drawdown of an equity curve as a positive percent."""
    if equity_curve.size == 0:
        return 0.0
    running_max = np.maximum.accumulate(equity_curve)
    drawdowns = (equity_curve - running_max) / running_max
    return float(abs(drawdowns.min()) * 100.0)


# --------------------------------------------------------------------------- #
# Directional backtest                                                        #
# --------------------------------------------------------------------------- #
def _round_trip_cost_pct(
    taker_fee: float = config.BACKTEST_TAKER_FEE,
    slippage_bps: float = config.BACKTEST_SLIPPAGE_BPS,
) -> float:
    """Total friction for one round trip (entry + exit): fees + slippage."""
    slippage_pct = slippage_bps / 10_000.0
    return 2.0 * (taker_fee + slippage_pct)


def _sharpe_sortino(
    trade_returns: np.ndarray,
    bars_per_trade: np.ndarray,
    risk_free: float = config.BACKTEST_RISK_FREE_RATE,
) -> tuple[float, float]:
    """Annualised Sharpe and Sortino from per-trade net returns."""
    if trade_returns.size < 2:
        return 0.0, 0.0
    avg_bars = float(np.mean(bars_per_trade)) if bars_per_trade.size else 24.0
    trades_per_year = HOURS_PER_YEAR / max(avg_bars, 1.0)
    excess = trade_returns - risk_free / trades_per_year
    std = float(np.std(excess, ddof=1))
    if std <= 1e-12:
        return 0.0, 0.0
    sharpe = float(np.mean(excess) / std * np.sqrt(trades_per_year))
    downside = excess[excess < 0]
    if downside.size == 0:
        sortino = sharpe
    else:
        down_std = float(np.std(downside, ddof=1))
        sortino = float(np.mean(excess) / down_std * np.sqrt(trades_per_year)) if down_std > 1e-12 else 0.0
    return sharpe, sortino


def backtest_directional(
    test: pd.DataFrame,
    proba: np.ndarray,
    long_threshold: float,
    short_threshold: float,
    take_profit: float = config.TAKE_PROFIT_PCT,
    stop_loss: float = config.STOP_LOSS_PCT,
    window: int = config.FORWARD_WINDOW,
    use_trend_filter: Optional[bool] = None,
    taker_fee: float = config.BACKTEST_TAKER_FEE,
    slippage_bps: float = config.BACKTEST_SLIPPAGE_BPS,
    rich: bool = False,
) -> tuple | BacktestResult:
    """Event-driven, dual-direction triple-barrier backtest with optional costs.

    When ``rich=False`` (default), returns the legacy 5-tuple for backward
    compatibility. When ``rich=True``, returns a :class:`BacktestResult`.
    """
    result = _run_backtest(
        test, proba, long_threshold, short_threshold,
        take_profit, stop_loss, window, use_trend_filter, taker_fee, slippage_bps,
    )
    if rich:
        return result
    return (
        result.strategy_net_profit_pct,
        result.buy_hold_net_profit_pct,
        result.n_long,
        result.n_short,
        result.equity_curve,
    )


def _run_backtest(
    test: pd.DataFrame,
    proba: np.ndarray,
    long_threshold: float,
    short_threshold: float,
    take_profit: float,
    stop_loss: float,
    window: int,
    use_trend_filter: Optional[bool],
    taker_fee: float,
    slippage_bps: float,
) -> BacktestResult:
    close = test["Close"].to_numpy(dtype=np.float64)
    high = test["High"].to_numpy(dtype=np.float64) if "High" in test else close
    low = test["Low"].to_numpy(dtype=np.float64) if "Low" in test else close
    n = len(close)
    trend = _trend_array(test, n)

    p_long = proba[:, config.LABEL_LONG]
    p_short = proba[:, config.LABEL_SHORT]
    cost = _round_trip_cost_pct(taker_fee, slippage_bps)

    equity = 1.0
    equity_curve: list[float] = [1.0]
    trades: list[TradeSim] = []
    long_wins = long_total = 0
    short_wins = short_total = 0
    i = 0

    while i < n:
        direction = decide_direction(
            p_long[i], p_short[i], trend[i], long_threshold, short_threshold,
            use_trend_filter=use_trend_filter,
        )
        if direction == "CASH":
            i += 1
            continue

        entry = close[i]
        trade_return = 0.0
        exit_offset = window
        outcome = "TIMEOUT"

        if direction == "LONG":
            tp_level = entry * (1.0 + take_profit)
            sl_level = entry * (1.0 - stop_loss)
            for j in range(i + 1, min(i + 1 + window, n)):
                if high[j] >= tp_level:
                    trade_return = take_profit
                    exit_offset = j - i
                    outcome = "TP"
                    break
                if low[j] <= sl_level:
                    trade_return = -stop_loss
                    exit_offset = j - i
                    outcome = "SL"
                    break
            else:
                last = min(i + window, n - 1)
                trade_return = (close[last] - entry) / entry
                exit_offset = last - i
            long_total += 1
            if trade_return > 0:
                long_wins += 1
        else:
            tp_level = entry * (1.0 - take_profit)
            sl_level = entry * (1.0 + stop_loss)
            for j in range(i + 1, min(i + 1 + window, n)):
                if low[j] <= tp_level:
                    trade_return = take_profit
                    exit_offset = j - i
                    outcome = "TP"
                    break
                if high[j] >= sl_level:
                    trade_return = -stop_loss
                    exit_offset = j - i
                    outcome = "SL"
                    break
            else:
                last = min(i + window, n - 1)
                trade_return = (entry - close[last]) / entry
                exit_offset = last - i
            short_total += 1
            if trade_return > 0:
                short_wins += 1

        net_return = trade_return - cost
        trades.append(
            TradeSim(
                direction=direction,
                gross_return=trade_return,
                net_return=net_return,
                outcome=outcome,
                bars_held=exit_offset,
                entry_idx=i,
            )
        )
        equity *= (1.0 + net_return)
        equity_curve.append(equity)
        i += max(exit_offset, 1)

    strategy_net_pct = (equity - 1.0) * 100.0
    buy_hold_pct = ((close[-1] - close[0]) / close[0]) * 100.0 if n > 1 else 0.0
    curve = np.array(equity_curve, dtype=np.float64)
    trade_returns = np.array([t.net_return for t in trades], dtype=np.float64)
    bars_held = np.array([t.bars_held for t in trades], dtype=np.float64)
    sharpe, sortino = _sharpe_sortino(trade_returns, bars_held)
    n_trades = len(trades)

    return BacktestResult(
        strategy_net_profit_pct=strategy_net_pct,
        buy_hold_net_profit_pct=buy_hold_pct,
        max_drawdown_pct=_max_drawdown(curve),
        sharpe=sharpe,
        sortino=sortino,
        n_trades=n_trades,
        n_long=long_total,
        n_short=short_total,
        long_win_rate=(100.0 * long_wins / long_total) if long_total else 0.0,
        short_win_rate=(100.0 * short_wins / short_total) if short_total else 0.0,
        overall_win_rate=(
            100.0 * (long_wins + short_wins) / n_trades if n_trades else 0.0
        ),
        equity_curve=curve,
        trades=trades,
        total_cost_pct=cost * n_trades * 100.0,
    )


def _threshold_grid() -> np.ndarray:
    return np.arange(
        config.THRESHOLD_SEARCH_MIN,
        config.THRESHOLD_SEARCH_MAX + 1e-9,
        config.THRESHOLD_SEARCH_STEP,
    )


def optimize_thresholds(
    valid_df: pd.DataFrame,
    proba_valid: np.ndarray,
) -> tuple[float, float, float, float]:
    """Choose per-direction thresholds maximising **positive** validation net profit.

    Only thresholds with validation PnL strictly greater than zero and at least
    ``config.MIN_VALIDATION_TRADES`` round trips are eligible. When no threshold
    qualifies for a direction, falls back to per-side defaults:
    :data:`config.LONG_FALLBACK_THRESHOLD` (0.78) and
    :data:`config.SHORT_FALLBACK_THRESHOLD` (0.60).
    """
    grid = _threshold_grid()
    if config.is_compound_profile():
        grid = grid[grid <= config.COMPOUND_MAX_THRESHOLD]
    isolate = config.THRESHOLD_DISABLED
    long_fallback = config.LONG_FALLBACK_THRESHOLD
    short_fallback = config.SHORT_FALLBACK_THRESHOLD
    min_trades = config.MIN_VALIDATION_TRADES

    def _candidate_score(profit: float, n_trades: int) -> float:
        if n_trades < min_trades or profit <= 0.0:
            return -float("inf")
        return profit

    best_long, best_long_profit, best_long_score, long_found = (
        long_fallback,
        0.0,
        -float("inf"),
        False,
    )
    for raw_t in grid:
        t = round(float(raw_t), 4)
        profit, _, n_l, _, _ = backtest_directional(
            valid_df, proba_valid, long_threshold=t, short_threshold=isolate
        )
        score = _candidate_score(profit, n_l)
        if score > best_long_score:
            best_long_score, best_long_profit, best_long, long_found = score, profit, t, True

    best_short, best_short_profit, best_short_score, short_found = (
        short_fallback,
        0.0,
        -float("inf"),
        False,
    )
    for raw_t in grid:
        t = round(float(raw_t), 4)
        profit, _, _, n_s, _ = backtest_directional(
            valid_df, proba_valid, long_threshold=isolate, short_threshold=t
        )
        score = _candidate_score(profit, n_s)
        if score > best_short_score:
            best_short_score, best_short_profit, best_short, short_found = score, profit, t, True

    if not long_found:
        logger.warning(
            "No long threshold with positive validation PnL and >=%d trades; "
            "using long fallback threshold %.2f (NO_EDGE_FALLBACK).",
            min_trades,
            long_fallback,
        )
        best_long = long_fallback
        best_long_profit = 0.0
    if not short_found:
        logger.warning(
            "No short threshold with positive validation PnL and >=%d trades; "
            "using short fallback threshold %.2f (NO_EDGE_FALLBACK).",
            min_trades,
            short_fallback,
        )
        best_short = short_fallback
        best_short_profit = 0.0

    return best_long, best_short, best_long_profit, best_short_profit


# --------------------------------------------------------------------------- #
# Precision helpers                                                           #
# --------------------------------------------------------------------------- #
def _directional_precision(
    feature_df: pd.DataFrame,
    proba: np.ndarray,
    y_true: np.ndarray,
    long_threshold: float,
    short_threshold: float,
    use_trend_filter: Optional[bool] = None,
) -> tuple[float, float]:
    """Precision of long and short *signals* at the chosen thresholds."""
    n = len(y_true)
    trend = _trend_array(feature_df, n)
    p_long = proba[:, config.LABEL_LONG]
    p_short = proba[:, config.LABEL_SHORT]

    trend_on = config.USE_TREND_FILTER if use_trend_filter is None else use_trend_filter
    if trend_on:
        long_trend_ok = trend > 0
        short_trend_ok = trend < 0
    else:
        long_trend_ok = np.ones(n, dtype=bool)
        short_trend_ok = np.ones(n, dtype=bool)

    long_mask = (p_long > long_threshold) & long_trend_ok
    short_mask = (p_short > short_threshold) & short_trend_ok

    long_prec = (
        float(np.mean(y_true[long_mask] == config.LABEL_LONG))
        if long_mask.any()
        else 0.0
    )
    short_prec = (
        float(np.mean(y_true[short_mask] == config.LABEL_SHORT))
        if short_mask.any()
        else 0.0
    )
    return long_prec, short_prec


# --------------------------------------------------------------------------- #
# Training                                                                    #
# --------------------------------------------------------------------------- #
def train_and_evaluate(
    matrix: pd.DataFrame | None = None,
    save: bool = True,
) -> tuple[object, BacktestMetrics]:
    """Train the multi-class model, tune thresholds, evaluate, and persist.

    Returns the fitted model and a :class:`BacktestMetrics` instance.
    """
    if matrix is None:
        import data_pipeline

        raw = data_pipeline.load_historical_data()
        matrix = build_feature_matrix(raw, feature_variant=config.FEATURE_VARIANT)

    if matrix.empty:
        raise RuntimeError("Feature matrix is empty; cannot train the model.")

    with use_feature_variant(config.FEATURE_VARIANT) as cols:
        return _train_and_evaluate_matrix(matrix, save=save, feature_columns=cols)


def _train_and_evaluate_matrix(
    matrix: pd.DataFrame,
    save: bool = True,
    *,
    feature_columns: list[str],
) -> tuple[object, BacktestMetrics]:
    """Core train/eval body using the active feature column list."""
    from sklearn.metrics import accuracy_score

    # Headline chronological 80/20 train/test split (no leakage).
    train_df, test_df = chronological_split(matrix)

    # Carve a validation tail out of training to choose thresholds. The final
    # model is refit on the entire 80% afterwards.
    val_split = int(len(train_df) * 0.85)
    sub_train_df = train_df.iloc[:val_split].reset_index(drop=True)
    valid_df = train_df.iloc[val_split:].reset_index(drop=True)
    logger.info(
        "Rows -> sub-train: %d | validation: %d | test: %d",
        len(sub_train_df),
        len(valid_df),
        len(test_df),
    )

    counts = train_df[TARGET_COLUMN].value_counts().to_dict()
    logger.info(
        "Train class counts -> LONG: %d | SHORT: %d | CASH: %d",
        counts.get(config.LABEL_LONG, 0),
        counts.get(config.LABEL_SHORT, 0),
        counts.get(config.LABEL_CASH, 0),
    )

    # 1) Fit on sub-train, choose thresholds on the validation slice.
    tuning_model = _build_model()
    _fit_balanced(tuning_model, sub_train_df[feature_columns], sub_train_df[TARGET_COLUMN])
    proba_valid = tuning_model.predict_proba(valid_df[feature_columns])
    long_thr, short_thr, long_vp, short_vp = optimize_thresholds(valid_df, proba_valid)
    logger.info(
        "Optimal thresholds -> long: %.3f (val %.2f%%) | short: %.3f (val %.2f%%).",
        long_thr,
        long_vp,
        short_thr,
        short_vp,
    )

    # 2) Refit the final model on the full 80% training data.
    model = _build_model()
    _fit_balanced(model, train_df[feature_columns], train_df[TARGET_COLUMN])

    x_test = test_df[feature_columns]
    y_test = test_df[TARGET_COLUMN].to_numpy()
    proba_test = model.predict_proba(x_test)
    pred_test = np.argmax(proba_test, axis=1)

    accuracy = float(accuracy_score(y_test, pred_test))
    long_prec, short_prec = _directional_precision(
        test_df, proba_test, y_test, long_thr, short_thr
    )

    test_for_bt = test_df.copy()
    if "High" not in test_for_bt.columns or "Low" not in test_for_bt.columns:
        test_for_bt["High"] = test_for_bt["Close"]
        test_for_bt["Low"] = test_for_bt["Close"]

    strat_pct, bh_pct, n_long, n_short, curve = backtest_directional(
        test_for_bt, proba_test, long_threshold=long_thr, short_threshold=short_thr
    )
    max_dd = _max_drawdown(curve)

    metrics = BacktestMetrics(
        oos_accuracy=round(accuracy, 4),
        long_precision=round(long_prec, 4),
        short_precision=round(short_prec, 4),
        strategy_net_profit_pct=round(strat_pct, 2),
        buy_hold_net_profit_pct=round(bh_pct, 2),
        max_drawdown_pct=round(max_dd, 2),
        n_trades=n_long + n_short,
        n_long=n_long,
        n_short=n_short,
        n_test_rows=len(test_df),
        long_threshold=round(long_thr, 4),
        short_threshold=round(short_thr, 4),
    )

    if save:
        model.save_model(config.MODEL_PATH)
        logger.info("Saved trained model to %s", config.MODEL_PATH)
        save_thresholds(long_thr, short_thr)

    return model, metrics


# --------------------------------------------------------------------------- #
# Inference                                                                   #
# --------------------------------------------------------------------------- #
def _model_feature_names(model) -> list[str]:
    """Return feature column names stored in a persisted XGBoost artifact."""
    booster = model.get_booster()
    names = booster.feature_names
    if names:
        return list(names)
    if hasattr(model, "feature_names_in_"):
        return list(model.feature_names_in_)
    return []


def validate_model_feature_variant(
    model,
    variant: str | None = None,
    *,
    model_path: str | None = None,
) -> None:
    """Raise if the loaded model's columns do not match ``FEATURE_VARIANT``."""
    variant = (variant or config.FEATURE_VARIANT).upper()
    expected = feature_columns_for(variant)
    actual = _model_feature_names(model)
    if not actual:
        return
    if actual == expected:
        return
    path = model_path or config.MODEL_PATH
    missing = [c for c in expected if c not in actual]
    extra = [c for c in actual if c not in expected]
    detail = []
    if missing:
        detail.append(f"missing in model: {missing}")
    if extra:
        detail.append(f"extra in model: {extra}")
    raise ValueError(
        f"Model artifact {path!r} has {len(actual)} features but "
        f"FEATURE_VARIANT={variant} expects {len(expected)}. "
        f"{'; '.join(detail)}. "
        f"Run `python model_brain.py --retrain` after setting FEATURE_VARIANT in `.env`."
    )


def load_model():
    """Load the persisted XGBoost model from :data:`config.MODEL_PATH`."""
    from xgboost import XGBClassifier

    if not os.path.exists(config.MODEL_PATH):
        raise FileNotFoundError(
            f"Model artifact not found at {config.MODEL_PATH}. "
            "Run `python model_brain.py` to train it first."
        )
    model = XGBClassifier()
    model.load_model(config.MODEL_PATH)
    validate_model_feature_variant(model)
    return model


def predict_latest(model, candles: pd.DataFrame) -> dict[str, float]:
    """Return the latest multi-class probabilities and trend for live use.

    ``candles`` is a raw OHLCV frame (e.g. from
    ``data_pipeline.fetch_latest_candles``). The returned dict contains
    ``prob_long``, ``prob_short``, ``prob_cash`` and ``trend``
    (the ``price_vs_ema200`` value of the latest candle).
    """
    enriched = compute_live_features(candles, feature_variant=config.FEATURE_VARIANT)
    cols = feature_columns_for(config.FEATURE_VARIANT)
    feature_rows = enriched[cols].dropna()
    if feature_rows.empty:
        raise ValueError("Not enough candles to compute a complete feature row.")
    latest = feature_rows.iloc[[-1]]
    proba = model.predict_proba(latest)[0]
    trend = float(latest["price_vs_ema200"].iloc[0]) if "price_vs_ema200" in latest else 0.0
    return {
        "prob_long": float(proba[config.LABEL_LONG]),
        "prob_short": float(proba[config.LABEL_SHORT]),
        "prob_cash": float(proba[config.LABEL_CASH]),
        "trend": trend,
    }


def _print_report(metrics: BacktestMetrics) -> None:
    report = {
        "Long Threshold": f"{metrics.long_threshold:.3f}",
        "Short Threshold": f"{metrics.short_threshold:.3f}",
        "Out-of-Sample Accuracy": f"{metrics.oos_accuracy:.2%}",
        "Long Precision": f"{metrics.long_precision:.2%}",
        "Short Precision": f"{metrics.short_precision:.2%}",
        "Strategy Net Profit": f"{metrics.strategy_net_profit_pct:.2f}%",
        "Buy & Hold Benchmark": f"{metrics.buy_hold_net_profit_pct:.2f}%",
        "Max Drawdown": f"{metrics.max_drawdown_pct:.2f}%",
        "Total Trades": metrics.n_trades,
        "Long / Short Trades": f"{metrics.n_long} / {metrics.n_short}",
        "Test Rows": metrics.n_test_rows,
    }
    print("\n" + "=" * 54)
    print("  OUT-OF-SAMPLE PERFORMANCE REPORT (FUTURES, MULTI-CLASS)")
    print("=" * 54)
    for key, value in report.items():
        print(f"  {key:.<34}{value}")
    print("=" * 54 + "\n")
    print("Raw metrics dict:")
    print(json.dumps(metrics.as_dict(), indent=2))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Train the XGBoost model and save artifacts.")
    parser.add_argument(
        "--retrain",
        action="store_true",
        help="Train from scratch on cached data and overwrite model + thresholds.",
    )
    args = parser.parse_args()
    if args.retrain:
        logger.info(
            "Retrain requested — fitting %s variant on %s data.",
            config.FEATURE_VARIANT,
            config.HISTORICAL_PARQUET,
        )
    _, m = train_and_evaluate(save=True)
    _print_report(m)
