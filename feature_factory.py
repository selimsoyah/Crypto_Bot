"""
feature_factory.py
===================
Quantitative feature engineering for the BTC/USDT model.

Transforms a clean OHLCV DataFrame into a feature matrix containing:
    * Trend  : EMA(20), EMA(50), EMA(200)
    * Momentum: RSI(14), MACD (line / signal / histogram)
    * Volatility: ATR(14), Bollinger Band width
    * Structure: rolling 24h local support & resistance lines

It also constructs a **multi-class** directional target using a forward-looking
triple-barrier rule over a ``config.FORWARD_WINDOW`` horizon. The barrier
levels are ``config.TAKE_PROFIT_PCT`` and ``config.STOP_LOSS_PCT`` — the SAME
constants the live bot uses for its TP/SL brackets, so training labels and
live execution can never drift apart:
    * Label 2 (LONG) : +TAKE_PROFIT_PCT reached before -STOP_LOSS_PCT.
    * Label 1 (SHORT): -TAKE_PROFIT_PCT reached before +STOP_LOSS_PCT.
    * Label 0 (CASH) : neither boundary triggered (choppy / sideways).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Final, Iterator

import numpy as np
import pandas as pd

import config

logger = config.configure_logging(__name__)

# Baseline feature set (v2.0.x production default — variant F0).
BASELINE_FEATURE_COLUMNS: Final[list[str]] = [
    "ema_20",
    "ema_50",
    "ema_200",
    "ema_20_50_spread",
    "price_vs_ema200",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_hist",
    "atr_14",
    "atr_pct",
    "bb_width",
    "support_24",
    "resistance_24",
    "dist_to_support",
    "dist_to_resistance",
    "return_1h",
    "return_24h",
    "volume_change",
]

# Phase 3 experimental groups (see feature_sweep.py).
REGIME_FEATURE_COLUMNS: Final[list[str]] = [
    "bb_width_pct",
    "atr_pct_z",
]
MTF_FEATURE_COLUMNS: Final[list[str]] = [
    "rsi_1h",
    "macd_hist_1h",
    "ema_spread_1h",
]
TIME_FEATURE_COLUMNS: Final[list[str]] = [
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
]
FLOW_FEATURE_COLUMNS: Final[list[str]] = [
    "volume_z",
    "close_in_bar",
]
PHASE3_EXTRA_COLUMNS: Final[list[str]] = (
    REGIME_FEATURE_COLUMNS
    + MTF_FEATURE_COLUMNS
    + TIME_FEATURE_COLUMNS
    + FLOW_FEATURE_COLUMNS
)

FEATURE_VARIANTS: Final[dict[str, str]] = {
    "F0": "Baseline (19 TA features)",
    "F1": "Regime — vol percentile / ATR z-score",
    "F2": "Multi-timeframe — 1h RSI / MACD / EMA spread on 15m rows",
    "F3": "Time — cyclical hour & day-of-week",
    "F4": "Flow — volume z-score & close position in bar",
    "F5": "All Phase 3 groups combined",
}

# Active column list for train/inference; patched by use_feature_variant() during sweeps.
FEATURE_COLUMNS: list[str] = list(BASELINE_FEATURE_COLUMNS)

TARGET_COLUMN: Final[str] = "target"

REGIME_ROLL_BARS: Final[int] = 96  # ~24h on 15m bars


# --------------------------------------------------------------------------- #
# Indicator primitives (raw formulas, no external dependency required)        #
# --------------------------------------------------------------------------- #
def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.fillna(50.0)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False).mean()


def _macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = _ema(series, fast)
    ema_slow = _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _bollinger_width(series: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.Series:
    mid = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    # Width normalized by the mid band so it is comparable across price regimes.
    return (upper - lower) / mid.replace(0.0, np.nan)


def feature_columns_for(variant: str = "F0") -> list[str]:
    """Return the ordered feature column list for a Phase 3 variant id."""
    v = variant.upper()
    base = list(BASELINE_FEATURE_COLUMNS)
    if v == "F0":
        return base
    if v == "F1":
        return base + list(REGIME_FEATURE_COLUMNS)
    if v == "F2":
        return base + list(MTF_FEATURE_COLUMNS)
    if v == "F3":
        return base + list(TIME_FEATURE_COLUMNS)
    if v == "F4":
        return base + list(FLOW_FEATURE_COLUMNS)
    if v == "F5":
        return base + list(PHASE3_EXTRA_COLUMNS)
    raise ValueError(
        f"Unknown feature variant {variant!r}. "
        f"Choose from: {', '.join(FEATURE_VARIANTS)}"
    )


@contextmanager
def use_feature_variant(variant: str) -> Iterator[list[str]]:
    """Temporarily set :data:`FEATURE_COLUMNS` for sweeps / experiments."""
    global FEATURE_COLUMNS
    cols = feature_columns_for(variant)
    previous = list(FEATURE_COLUMNS)
    FEATURE_COLUMNS = cols
    try:
        yield cols
    finally:
        FEATURE_COLUMNS = previous


def _rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=window // 2).mean()
    std = series.rolling(window, min_periods=window // 2).std()
    return (series - mean) / std.replace(0.0, np.nan)


def _rolling_percentile_rank(series: pd.Series, window: int) -> pd.Series:
    def _last_rank(values: np.ndarray) -> float:
        if len(values) == 0:
            return np.nan
        ordered = np.sort(values)
        return float(np.searchsorted(ordered, values[-1], side="right")) / len(values)

    return series.rolling(window, min_periods=window // 2).apply(_last_rank, raw=True)


def _add_regime_features(out: pd.DataFrame) -> pd.DataFrame:
    window = REGIME_ROLL_BARS
    out["bb_width_pct"] = _rolling_percentile_rank(out["bb_width"], window)
    out["atr_pct_z"] = _rolling_zscore(out["atr_pct"], window)
    return out


def _add_time_features(out: pd.DataFrame) -> pd.DataFrame:
    if "Timestamp" not in out.columns:
        for col in TIME_FEATURE_COLUMNS:
            out[col] = np.nan
        return out
    ts = pd.to_datetime(out["Timestamp"], utc=True)
    hour = ts.dt.hour + ts.dt.minute / 60.0
    dow = ts.dt.dayofweek.astype(float)
    out["hour_sin"] = np.sin(2.0 * np.pi * hour / 24.0)
    out["hour_cos"] = np.cos(2.0 * np.pi * hour / 24.0)
    out["dow_sin"] = np.sin(2.0 * np.pi * dow / 7.0)
    out["dow_cos"] = np.cos(2.0 * np.pi * dow / 7.0)
    return out


def _add_flow_features(out: pd.DataFrame) -> pd.DataFrame:
    window = REGIME_ROLL_BARS
    volume = out["Volume"]
    out["volume_z"] = _rolling_zscore(volume, window)
    bar_range = (out["High"] - out["Low"]).replace(0.0, np.nan)
    out["close_in_bar"] = (out["Close"] - out["Low"]) / bar_range
    return out


def _add_mtf_features(out: pd.DataFrame) -> pd.DataFrame:
    for col in MTF_FEATURE_COLUMNS:
        out[col] = np.nan
    if "Timestamp" not in out.columns:
        return out

    ts = pd.to_datetime(out["Timestamp"], utc=True)
    hourly = (
        out.assign(_ts=ts)
        .set_index("_ts")
        .resample("1h")
        .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        .dropna(subset=["Close"])
    )
    if hourly.empty:
        return out

    hourly["rsi_1h"] = _rsi(hourly["Close"])
    _, _, hist_1h = _macd(hourly["Close"])
    hourly["macd_hist_1h"] = hist_1h
    ema20 = _ema(hourly["Close"], 20)
    ema50 = _ema(hourly["Close"], 50)
    hourly["ema_spread_1h"] = (ema20 - ema50) / hourly["Close"]

    aligned = hourly[MTF_FEATURE_COLUMNS].reindex(ts, method="ffill")
    for col in MTF_FEATURE_COLUMNS:
        out[col] = aligned[col].to_numpy()
    return out


def _phase3_groups_for_variant(variant: str) -> set[str]:
    v = variant.upper()
    if v == "F0":
        return set()
    if v == "F1":
        return {"regime"}
    if v == "F2":
        return {"mtf"}
    if v == "F3":
        return {"time"}
    if v == "F4":
        return {"flow"}
    if v == "F5":
        return {"regime", "mtf", "time", "flow"}
    raise ValueError(f"Unknown feature variant {variant!r}")


# --------------------------------------------------------------------------- #
# Feature assembly                                                            #
# --------------------------------------------------------------------------- #
def add_technical_indicators(
    df: pd.DataFrame,
    *,
    feature_variant: str = "F0",
) -> pd.DataFrame:
    """Append all technical indicator and structural columns to ``df``.

    The input must contain ``Open, High, Low, Close, Volume`` columns. A copy
    is returned; the original frame is not mutated.
    """
    required = {"Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {sorted(missing)}")

    out = df.copy()
    close = out["Close"]
    high = out["High"]
    low = out["Low"]
    volume = out["Volume"]

    # Trend ---------------------------------------------------------------- #
    out["ema_20"] = _ema(close, 20)
    out["ema_50"] = _ema(close, 50)
    out["ema_200"] = _ema(close, 200)
    out["ema_20_50_spread"] = (out["ema_20"] - out["ema_50"]) / close
    out["price_vs_ema200"] = (close - out["ema_200"]) / out["ema_200"]

    # Momentum ------------------------------------------------------------- #
    out["rsi_14"] = _rsi(close, 14)
    macd_line, signal_line, hist = _macd(close)
    out["macd"] = macd_line
    out["macd_signal"] = signal_line
    out["macd_hist"] = hist

    # Volatility ----------------------------------------------------------- #
    out["atr_14"] = _atr(high, low, close, 14)
    out["atr_pct"] = out["atr_14"] / close
    out["bb_width"] = _bollinger_width(close, 20, 2.0)

    # Local structure (rolling lookback support / resistance) ---------------- #
    lookback = config.STRUCTURE_LOOKBACK
    out["support_24"] = low.rolling(window=lookback).min()
    out["resistance_24"] = high.rolling(window=lookback).max()
    out["dist_to_support"] = (close - out["support_24"]) / close
    out["dist_to_resistance"] = (out["resistance_24"] - close) / close

    # Returns / volume dynamics ------------------------------------------- #
    out["return_1h"] = close.pct_change(1)
    out["return_24h"] = close.pct_change(config.RETURN_LONG_LOOKBACK)
    out["volume_change"] = volume.pct_change(1).replace([np.inf, -np.inf], np.nan)

    groups = _phase3_groups_for_variant(feature_variant)
    if "regime" in groups:
        out = _add_regime_features(out)
    if "mtf" in groups:
        out = _add_mtf_features(out)
    if "time" in groups:
        out = _add_time_features(out)
    if "flow" in groups:
        out = _add_flow_features(out)

    return out


def build_target(
    df: pd.DataFrame,
    window: int | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
) -> pd.Series:
    """Build the 3-class directional triple-barrier target.

    For every candle ``i`` we inspect the forward ``window`` candles and decide
    which directional trade would have resolved profitably *first*:

    * ``LABEL_LONG`` (2): the high reaches ``entry * (1 + take_profit)`` strictly
      before the low breaches ``entry * (1 - stop_loss)``.
    * ``LABEL_SHORT`` (1): the low reaches ``entry * (1 - take_profit)`` strictly
      before the high breaches ``entry * (1 + stop_loss)``.
    * ``LABEL_CASH`` (0): neither directional setup triggers within the window.

    ``take_profit`` and ``stop_loss`` default to ``config.TAKE_PROFIT_PCT`` and
    ``config.STOP_LOSS_PCT`` at **call** time — the exact values the live bot
    trades with. Do not pass literals here in production code; the single source
    of truth is ``config.py``.

    The two directional conditions are mutually exclusive by construction (a
    clean +TP-before-SL path in one direction cannot simultaneously be a clean
    +TP-before-SL path in the other), so each candle maps to exactly one label.

    The final ``window`` rows have no complete forward horizon and are returned
    as ``NaN`` so they can be dropped by the caller.
    """
    window = config.FORWARD_WINDOW if window is None else window
    take_profit = config.TAKE_PROFIT_PCT if take_profit is None else take_profit
    stop_loss = config.STOP_LOSS_PCT if stop_loss is None else stop_loss
    close = df["Close"].to_numpy(dtype=np.float64)
    high = df["High"].to_numpy(dtype=np.float64)
    low = df["Low"].to_numpy(dtype=np.float64)
    n = len(close)

    target = np.full(n, np.nan, dtype=np.float64)

    for i in range(n - window):
        entry = close[i]
        long_tp = entry * (1.0 + take_profit)
        long_sl = entry * (1.0 - stop_loss)
        short_tp = entry * (1.0 - take_profit)
        short_sl = entry * (1.0 + stop_loss)

        label = config.LABEL_CASH
        long_dead = False   # long stop touched before long take-profit
        short_dead = False  # short stop touched before short take-profit

        # Walk the forward window in chronological order. Within a single candle
        # we apply "death precedence" (a stop on the same bar invalidates that
        # direction's win), which is the conservative choice.
        for j in range(i + 1, i + 1 + window):
            hi = high[j]
            lo = low[j]

            if lo <= long_sl:
                long_dead = True
            if hi >= short_sl:
                short_dead = True

            if not long_dead and hi >= long_tp:
                label = config.LABEL_LONG
                break
            if not short_dead and lo <= short_tp:
                label = config.LABEL_SHORT
                break
            if long_dead and short_dead:
                label = config.LABEL_CASH
                break
        target[i] = label

    return pd.Series(target, index=df.index, name=TARGET_COLUMN)


def build_feature_matrix(
    df: pd.DataFrame,
    *,
    forward_window: int | None = None,
    take_profit_pct: float | None = None,
    stop_loss_pct: float | None = None,
    feature_variant: str = "F0",
) -> pd.DataFrame:
    """Return a model-ready frame: indicators + target with NaNs dropped.

    The returned frame retains ``Timestamp`` and ``Close`` (for backtesting)
    alongside the active feature columns and :data:`TARGET_COLUMN`.

    Optional label overrides are used by ``label_sweep.py``; ``feature_variant``
    selects Phase 3 feature groups (``feature_sweep.py``).
    """
    cols = feature_columns_for(feature_variant)
    enriched = add_technical_indicators(df, feature_variant=feature_variant)
    enriched[TARGET_COLUMN] = build_target(
        enriched,
        window=forward_window,
        take_profit=take_profit_pct,
        stop_loss=stop_loss_pct,
    )

    keep_cols: list[str] = []
    if "Timestamp" in enriched.columns:
        keep_cols.append("Timestamp")
    # Keep OHLC alongside features so the backtester can use true intraday
    # barriers; feature columns alone are used for model training/inference.
    keep_cols += ["High", "Low", "Close"] + cols + [TARGET_COLUMN]

    matrix = enriched[keep_cols].copy()
    matrix = matrix.replace([np.inf, -np.inf], np.nan).dropna().reset_index(drop=True)

    if TARGET_COLUMN in matrix.columns:
        matrix[TARGET_COLUMN] = matrix[TARGET_COLUMN].astype(int)

    if not matrix.empty:
        dist = matrix[TARGET_COLUMN].value_counts(normalize=True).to_dict()
        logger.info(
            "Built feature matrix: %d rows x %d features (%s) "
            "(LONG: %.1f%% | SHORT: %.1f%% | CASH: %.1f%%).",
            len(matrix),
            len(cols),
            feature_variant,
            100.0 * dist.get(config.LABEL_LONG, 0.0),
            100.0 * dist.get(config.LABEL_SHORT, 0.0),
            100.0 * dist.get(config.LABEL_CASH, 0.0),
        )
    return matrix


def compute_live_features(
    df: pd.DataFrame,
    *,
    feature_variant: str | None = None,
) -> pd.DataFrame:
    """Compute features for live inference (no target, no row dropping).

    Returns the indicator frame; the caller typically uses the last valid row
    as the inference vector. ``feature_variant`` defaults to
    ``config.FEATURE_VARIANT``.
    """
    variant = feature_variant if feature_variant is not None else config.FEATURE_VARIANT
    enriched = add_technical_indicators(df, feature_variant=variant)
    enriched = enriched.replace([np.inf, -np.inf], np.nan)
    return enriched


if __name__ == "__main__":
    import data_pipeline

    raw = data_pipeline.load_historical_data()
    fm = build_feature_matrix(raw)
    print(fm.tail())
    print(f"\nFeature matrix shape: {fm.shape}")
    print("Target distribution (0=CASH, 1=SHORT, 2=LONG):")
    print(fm[TARGET_COLUMN].value_counts(normalize=True).sort_index())
