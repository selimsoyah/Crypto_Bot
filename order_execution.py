"""
order_execution.py
==================
Post-only (maker) limit order routing and pre-trade liquidity checks.

Binance USDⓈ-M futures use ``timeInForce='GTX'`` (Good Till Crossing) for
add-liquidity-only limits. Orders that would cross the spread are rejected by
the exchange — we treat that as a failed fill and log it explicitly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

import config
import exchange_client

logger = config.configure_logging(__name__)


@dataclass(frozen=True)
class BookTicker:
    bid: float
    ask: float

    @property
    def spread_pct(self) -> float:
        if self.bid <= 0:
            return float("inf")
        return (self.ask - self.bid) / self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class SpreadGateResult:
    allowed: bool
    spread_pct: float
    bid: float
    ask: float
    reason: str = ""


@dataclass(frozen=True)
class MakerOrderResult:
    success: bool
    fill_price: float
    order: Optional[dict[str, Any]] = None
    reason: str = ""


def interval_minutes(interval: str) -> int:
    """Parse ``15m`` / ``1h`` style interval strings into bar length in minutes."""
    value = interval.strip().lower()
    if value.endswith("m"):
        return int(value[:-1])
    if value.endswith("h"):
        return int(value[:-1]) * 60
    raise ValueError(f"Unsupported candle interval: {interval!r}")


def bars_elapsed(entry_candle_ts: str, current_candle_ts: str, interval: str) -> int:
    """Count completed 15m (etc.) bars between two candle timestamps."""
    import pandas as pd

    start = pd.Timestamp(entry_candle_ts)
    end = pd.Timestamp(current_candle_ts)
    if end < start:
        return 0
    minutes = interval_minutes(interval)
    return int((end - start).total_seconds() // (minutes * 60))


def fetch_book_ticker(client, symbol: str) -> BookTicker:
    """Return the best bid/ask from the futures book ticker stream."""
    raw = exchange_client.call_with_retry(
        client.futures_orderbook_ticker,
        symbol=symbol,
        label="futures_orderbook_ticker",
    )
    return BookTicker(bid=float(raw["bidPrice"]), ask=float(raw["askPrice"]))


def check_spread_gate(
    book: BookTicker,
    max_spread_pct: float = config.MAX_ENTRY_SPREAD_PCT,
) -> SpreadGateResult:
    """Block entries when bid-ask spread is wider than the audit tolerance."""
    spread = book.spread_pct
    if spread > max_spread_pct:
        return SpreadGateResult(
            allowed=False,
            spread_pct=spread,
            bid=book.bid,
            ask=book.ask,
            reason=(
                f"Spread too wide ({spread * 100:.4f}% > "
                f"{max_spread_pct * 100:.4f}% max; bid ${book.bid:,.2f}, "
                f"ask ${book.ask:,.2f})"
            ),
        )
    return SpreadGateResult(
        allowed=True,
        spread_pct=spread,
        bid=book.bid,
        ask=book.ask,
    )


def round_price(price: float, tick_size: float, precision: int) -> float:
    if tick_size > 0:
        stepped = int(price / tick_size) * tick_size
        return float(round(stepped, precision))
    return float(round(price, precision))


def maker_limit_price(side: str, book: BookTicker) -> float:
    """Pick a post-only price on the passive side of the book."""
    side = side.upper()
    if side == "BUY":
        return book.bid
    if side == "SELL":
        return book.ask
    raise ValueError(f"Unknown order side: {side!r}")


def _is_post_only_reject(exc: Exception) -> bool:
    msg = str(exc)
    needles = (
        "-5022",  # GTX would immediately match
        "Post Only",
        "post only",
        "would immediately match",
        "GTX",
    )
    return any(n in msg for n in needles)


def _extract_fill_price(order: dict[str, Any], fallback: float) -> float:
    try:
        avg = order.get("avgPrice")
        if avg is not None and float(avg) > 0:
            return float(avg)
        executed = order.get("executedQty")
        cum_quote = order.get("cumQuote")
        if executed and cum_quote and float(executed) > 0:
            return float(cum_quote) / float(executed)
        price = order.get("price")
        if price is not None and float(price) > 0:
            return float(price)
    except (TypeError, ValueError, KeyError):
        pass
    return fallback


def place_post_only_limit(
    client,
    *,
    symbol: str,
    side: str,
    quantity: float,
    price: float,
    reduce_only: bool,
) -> dict[str, Any]:
    """Submit a GTX post-only limit order."""
    params: dict[str, Any] = {
        "symbol": symbol,
        "side": side.upper(),
        "type": "LIMIT",
        "timeInForce": "GTX",
        "quantity": quantity,
        "price": price,
    }
    if reduce_only:
        params["reduceOnly"] = True
    return exchange_client.call_with_retry(
        client.futures_create_order,
        label="futures_create_order_post_only",
        **params,
    )


def wait_for_fill(
    client,
    *,
    symbol: str,
    order_id: int,
    timeout_sec: float,
    poll_interval_sec: float,
    fallback_price: float,
) -> MakerOrderResult:
    """Poll order status until filled, cancelled, or timed out."""
    deadline = time.monotonic() + timeout_sec
    last_status = "NEW"
    while time.monotonic() < deadline:
        order = exchange_client.call_with_retry(
            client.futures_get_order,
            symbol=symbol,
            orderId=order_id,
            label="futures_get_order",
        )
        last_status = str(order.get("status", ""))
        if last_status == "FILLED":
            return MakerOrderResult(
                success=True,
                fill_price=_extract_fill_price(order, fallback_price),
                order=order,
            )
        if last_status in {"CANCELED", "REJECTED", "EXPIRED"}:
            return MakerOrderResult(
                success=False,
                fill_price=fallback_price,
                order=order,
                reason=f"Post-only order {last_status.lower()} before fill",
            )
        time.sleep(poll_interval_sec)

    try:
        exchange_client.call_with_retry(
            client.futures_cancel_order,
            symbol=symbol,
            orderId=order_id,
            label="futures_cancel_order",
        )
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("Could not cancel timed-out order %s: %s", order_id, exc)

    return MakerOrderResult(
        success=False,
        fill_price=fallback_price,
        reason=(
            f"Post-only order failed to fill within {timeout_sec:.0f}s "
            f"(last status: {last_status})"
        ),
    )


def place_and_wait_post_only(
    client,
    *,
    symbol: str,
    side: str,
    quantity: float,
    reduce_only: bool,
    book: Optional[BookTicker] = None,
    tick_size: float = 0.1,
    price_precision: int = 2,
    fill_timeout_sec: Optional[float] = None,
    poll_interval_sec: Optional[float] = None,
) -> MakerOrderResult:
    """Place a maker limit at the passive book side and wait for a fill."""
    if not config.USE_POST_ONLY_MAKER:
        raise RuntimeError("place_and_wait_post_only called but USE_POST_ONLY_MAKER is off")

    book = book or fetch_book_ticker(client, symbol)
    limit_price = round_price(
        maker_limit_price(side, book),
        tick_size=tick_size,
        precision=price_precision,
    )
    timeout = fill_timeout_sec if fill_timeout_sec is not None else config.LIMIT_ORDER_FILL_TIMEOUT_SEC
    poll = poll_interval_sec if poll_interval_sec is not None else config.LIMIT_ORDER_POLL_INTERVAL_SEC

    try:
        placed = place_post_only_limit(
            client,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=limit_price,
            reduce_only=reduce_only,
        )
    except Exception as exc:
        if _is_post_only_reject(exc):
            return MakerOrderResult(
                success=False,
                fill_price=book.mid,
                reason=f"Post-only order rejected by exchange (would cross spread @ ${limit_price:,.2f})",
            )
        raise

    order_id = int(placed["orderId"])
    immediate = str(placed.get("status", ""))
    if immediate == "FILLED":
        return MakerOrderResult(
            success=True,
            fill_price=_extract_fill_price(placed, limit_price),
            order=placed,
        )
    return wait_for_fill(
        client,
        symbol=symbol,
        order_id=order_id,
        timeout_sec=timeout,
        poll_interval_sec=poll,
        fallback_price=limit_price,
    )


def fetch_open_position(client, symbol: str) -> dict[str, float]:
    """Return exchange position snapshot: side (LONG/SHORT/FLAT), qty, entry."""
    rows = exchange_client.call_with_retry(
        client.futures_position_information,
        symbol=symbol,
        label="futures_position_information",
    )
    if not rows:
        return {"side": "FLAT", "quantity": 0.0, "entry_price": 0.0}
    row = rows[0]
    amt = float(row.get("positionAmt", 0.0) or 0.0)
    if abs(amt) < 1e-12:
        return {"side": "FLAT", "quantity": 0.0, "entry_price": 0.0}
    side = "LONG" if amt > 0 else "SHORT"
    entry = float(row.get("entryPrice", 0.0) or 0.0)
    return {"side": side, "quantity": abs(amt), "entry_price": entry}


def flatten_position_market(
    client,
    *,
    symbol: str,
    quantity: float,
    position_side: str,
) -> dict[str, Any]:
    """Market-close an open futures position (reduce-only)."""
    if quantity <= 0:
        return {}
    close_side = "SELL" if position_side.upper() == "LONG" else "BUY"
    return exchange_client.call_with_retry(
        client.futures_create_order,
        symbol=symbol,
        side=close_side,
        type="MARKET",
        quantity=quantity,
        reduceOnly=True,
        label="futures_flatten_market",
    )
