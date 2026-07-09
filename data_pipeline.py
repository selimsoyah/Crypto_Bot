"""
data_pipeline.py
================
Historical data acquisition and caching for BTC/USDT 1-hour candles.

The module pulls up to ``config.HISTORY_YEARS`` years of **USDⓈ-M Futures**
klines using ``python-binance`` and caches the parsed result into an optimized
parquet file so subsequent training runs never re-hit the rate-limited REST
endpoints.

Important: the Binance **Testnet** only retains a short kline history, which is
insufficient for training. Market data (historical candles and live signal
candles) is therefore pulled from the public **mainnet futures** REST API by
default (no authentication needed for klines), while order execution remains on
the Futures Testnet. This behaviour is controlled by ``config.USE_MAINNET_DATA``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Final

import pandas as pd

import config
import exchange_client

logger = config.configure_logging(__name__)

# Raw kline column order returned by the Binance REST API.
_RAW_KLINE_COLUMNS: Final[list[str]] = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "number_of_trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]

# Clean output schema requested by the project specification.
OUTPUT_COLUMNS: Final[list[str]] = [
    "Timestamp",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
]

def _build_client(use_mainnet: bool | None = None):
    """Return a Binance client for kline reads (delegates to exchange_client)."""
    return exchange_client.build_data_client(use_mainnet=use_mainnet)


def _fetch_klines_with_retry(client, label: str, **kwargs):
    return exchange_client.call_with_retry(
        client.futures_klines, label=label, **kwargs
    )


def _fetch_historical_klines_with_retry(client, symbol, interval, start_str):
    return exchange_client.call_with_retry(
        client.futures_historical_klines,
        symbol,
        interval,
        start_str,
        label="futures_historical_klines",
    )


def _parse_klines(raw: list[list]) -> pd.DataFrame:
    """Convert a raw Binance kline array into the clean OHLCV DataFrame."""
    if not raw:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    frame = pd.DataFrame(raw, columns=_RAW_KLINE_COLUMNS)

    numeric_cols = ["open", "high", "low", "close", "volume"]
    frame[numeric_cols] = frame[numeric_cols].apply(pd.to_numeric, errors="coerce")

    frame["Timestamp"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)

    clean = frame.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )[OUTPUT_COLUMNS]

    clean = clean.dropna().drop_duplicates(subset="Timestamp")
    clean = clean.sort_values("Timestamp").reset_index(drop=True)
    return clean


def fetch_historical_klines(
    symbol: str = config.SYMBOL,
    interval: str = config.INTERVAL,
    years: int = config.HISTORY_YEARS,
) -> pd.DataFrame:
    """Download ``years`` of historical klines from the Testnet.

    Parameters
    ----------
    symbol:
        Trading pair, e.g. ``"BTCUSDT"``.
    interval:
        Kline interval string, e.g. ``"1h"``.
    years:
        How many years of history to request.

    Returns
    -------
    pandas.DataFrame
        Clean OHLCV frame with columns from :data:`OUTPUT_COLUMNS`.
    """
    client = _build_client(use_mainnet=config.USE_MAINNET_DATA)
    source = "mainnet futures" if config.USE_MAINNET_DATA else "futures testnet"

    start_dt = datetime.now(timezone.utc) - timedelta(days=365 * years)
    start_str = start_dt.strftime("%d %b %Y %H:%M:%S")

    logger.info(
        "Fetching %s %s futures candles since %s from %s ...",
        symbol,
        interval,
        start_str,
        source,
    )

    # ``futures_historical_klines`` transparently paginates across the range.
    raw = _fetch_historical_klines_with_retry(client, symbol, interval, start_str)
    frame = _parse_klines(raw)

    logger.info("Downloaded %d candles (%s -> %s).",
                len(frame),
                frame["Timestamp"].min() if not frame.empty else "n/a",
                frame["Timestamp"].max() if not frame.empty else "n/a")
    return frame


def load_historical_data(
    force_refresh: bool = False,
    parquet_path: str = config.HISTORICAL_PARQUET,
) -> pd.DataFrame:
    """Return the historical dataset, using the parquet cache when available.

    Parameters
    ----------
    force_refresh:
        When ``True`` the cache is ignored and data is re-downloaded.
    parquet_path:
        Location of the local parquet cache file.
    """
    if not force_refresh and os.path.exists(parquet_path):
        try:
            cached = pd.read_parquet(parquet_path)
            if not cached.empty and set(OUTPUT_COLUMNS).issubset(cached.columns):
                logger.info("Loaded %d cached candles from %s.",
                            len(cached), parquet_path)
                return cached
            logger.warning("Cache at %s was malformed; re-downloading.", parquet_path)
        except Exception as exc:  # corrupt cache -> fall through to refresh
            logger.warning("Failed to read cache %s (%s); re-downloading.",
                           parquet_path, exc)

    frame = fetch_historical_klines()

    if frame.empty:
        raise RuntimeError(
            "No historical data returned from Binance. "
            "Verify network connectivity (and API credentials if using testnet)."
        )

    try:
        frame.to_parquet(parquet_path, index=False)
        logger.info("Cached %d candles to %s.", len(frame), parquet_path)
    except Exception as exc:  # pragma: no cover - disk/permission issues
        logger.warning("Could not write parquet cache %s (%s).", parquet_path, exc)

    return frame


def fetch_previous_utc_day_high_low(
    symbol: str = config.SYMBOL,
) -> tuple[float, float, str]:
    """Return (high, low, prev_day_iso) from the exchange UTC daily candle."""
    client = _build_client(use_mainnet=config.USE_MAINNET_DATA)
    raw = _fetch_klines_with_retry(
        client,
        label="daily_bounds",
        symbol=symbol,
        interval="1d",
        limit=5,
    )
    frame = _parse_klines(raw)
    if frame.empty:
        raise ValueError("No daily candles returned for previous-day box bounds.")

    prev_day = datetime.now(timezone.utc).date() - timedelta(days=1)
    day_start = pd.Timestamp(datetime(prev_day.year, prev_day.month, prev_day.day, tzinfo=timezone.utc))
    match = frame[frame["Timestamp"] == day_start]
    if match.empty:
        match = frame[frame["Timestamp"].dt.date == prev_day]
    if match.empty:
        raise ValueError(f"Exchange daily candle missing for previous UTC day {prev_day.isoformat()}.")

    row = match.iloc[-1]
    top = float(row["High"])
    bottom = float(row["Low"])
    if top <= bottom:
        raise ValueError("Exchange daily candle produced an invalid high/low range.")
    return top, bottom, prev_day.isoformat()


def fetch_latest_candles(
    symbol: str = config.SYMBOL,
    interval: str = config.INTERVAL,
    limit: int = config.LIVE_CANDLE_LOOKBACK,
) -> pd.DataFrame:
    """Fetch the most recent ``limit`` candles for live feature recomputation.

    Uses the same data source as training (mainnet futures by default) so the
    live feature distribution matches what the model was trained on. Order
    execution still happens on the Futures Testnet via ``bot_loop``.
    """
    client = _build_client(use_mainnet=config.USE_MAINNET_DATA)
    raw = _fetch_klines_with_retry(
        client, label="futures_klines", symbol=symbol, interval=interval, limit=limit
    )
    return _parse_klines(raw)


if __name__ == "__main__":
    df = load_historical_data(force_refresh=False)
    print(df.tail())
    print(f"\nTotal rows: {len(df):,}")
