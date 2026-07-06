"""
exchange_client.py
==================
Shared Binance client factory and retry wrapper for all API calls.

Every authenticated futures call (bot, dashboard) and public kline fetch
(data pipeline) should route through :func:`call_with_retry` so transient
network failures backoff instead of silently returning empty/zero data.
"""

from __future__ import annotations

import random
import time
from typing import Callable, Optional, TypeVar

import config

logger = config.configure_logging(__name__)

T = TypeVar("T")


def build_execution_client():
    """Build a Binance client for order execution (venue from ``EXECUTION_VENUE``)."""
    from binance.client import Client

    if config.execution_is_live():
        client = Client(
            api_key=config.API_KEY,
            api_secret=config.SECRET_KEY,
            testnet=False,
        )
        client.FUTURES_URL = f"{config.MAINNET_FUTURES_URL}/fapi"
        logger.warning(
            "Execution client -> LIVE MAINNET (%s)", config.MAINNET_FUTURES_URL
        )
    else:
        client = Client(
            api_key=config.API_KEY,
            api_secret=config.SECRET_KEY,
            testnet=True,
        )
        client.FUTURES_URL = f"{config.FUTURES_TESTNET_URL}/fapi"
        logger.info(
            "Execution client -> TESTNET (%s)", config.FUTURES_TESTNET_URL
        )
    return client


def build_data_client(use_mainnet: Optional[bool] = None):
    """Build a client for kline/market-data reads (no order routing)."""
    from binance.client import Client

    if use_mainnet is None:
        use_mainnet = config.USE_MAINNET_DATA

    if use_mainnet:
        client = Client(api_key=config.API_KEY, api_secret=config.SECRET_KEY)
        client.FUTURES_URL = f"{config.MAINNET_FUTURES_URL}/fapi"
        return client

    client = Client(
        api_key=config.API_KEY,
        api_secret=config.SECRET_KEY,
        testnet=True,
    )
    client.FUTURES_URL = f"{config.FUTURES_TESTNET_URL}/fapi"
    return client


def call_with_retry(
    fn: Callable[..., T],
    *args,
    attempts: Optional[int] = None,
    base_delay: Optional[float] = None,
    label: str = "binance_api",
    **kwargs,
) -> T:
    """Call ``fn`` with exponential backoff on failure."""
    max_attempts = attempts if attempts is not None else config.EXCHANGE_RETRY_ATTEMPTS
    delay_base = base_delay if base_delay is not None else config.EXCHANGE_RETRY_BASE_DELAY
    last_exc: Optional[Exception] = None

    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts - 1:
                break
            wait = delay_base * (2**attempt) + random.uniform(0, 0.15)
            logger.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                label,
                attempt + 1,
                max_attempts,
                config.sanitize_for_log(str(exc)),
                wait,
            )
            time.sleep(wait)

    assert last_exc is not None
    raise last_exc
