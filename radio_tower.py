"""
radio_tower.py
==============
READ-ONLY analytical listener for the public ai4trade.ai crypto operations feed.

This module polls the global AI agent fleet and persists signals to SQLite for
dashboard analysis. It **never** places orders or forwards signals to the local
execution engine — observation only.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

import config
from trade_store import ExternalSignal, TradeStore

logger = config.configure_logging(__name__)

_USER_AGENT = "CryptoBot-RadioTower/1.0 (read-only listener)"


def normalize_action(raw: str) -> str:
    """Map API ``side`` values to dashboard-friendly action tags."""
    mapping = {
        "buy": "BUY",
        "sell": "SELL",
        "short": "SHORT",
        "cover": "COVER",
        "long": "BUY",
    }
    return mapping.get(str(raw or "").strip().lower(), str(raw or "UNKNOWN").upper())


def parse_feed_signal(item: dict[str, Any]) -> Optional[ExternalSignal]:
    """Convert one ai4trade feed item into an :class:`ExternalSignal`."""
    if not isinstance(item, dict):
        return None

    timestamp = (
        item.get("executed_at")
        or item.get("created_at")
        or item.get("timestamp")
    )
    symbol = str(item.get("symbol") or "").strip().upper()
    if not timestamp or not symbol:
        return None

    side = item.get("side") or item.get("action") or ""
    action = normalize_action(str(side))

    price_raw = item.get("entry_price", item.get("price"))
    qty_raw = item.get("quantity", 0.0)
    try:
        price = float(price_raw) if price_raw is not None else 0.0
    except (TypeError, ValueError):
        price = 0.0
    try:
        quantity = float(qty_raw) if qty_raw is not None else 0.0
    except (TypeError, ValueError):
        quantity = 0.0

    agent_name = str(item.get("agent_name") or "Unknown Agent").strip() or "Unknown Agent"
    content = str(item.get("content") or "").strip()

    return ExternalSignal(
        timestamp=str(timestamp),
        symbol=symbol,
        action=action,
        price=price,
        quantity=quantity,
        agent_name=agent_name,
        content=content,
    )


def fetch_operation_feed(
    url: Optional[str] = None,
    *,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """GET the public crypto operations feed (raises on HTTP / network errors)."""
    feed_url = url or config.RADIO_TOWER_FEED_URL
    timeout = config.RADIO_TOWER_HTTP_TIMEOUT if timeout is None else timeout

    req = urllib.request.Request(feed_url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if status != 200:
            raise urllib.error.HTTPError(
                feed_url, status, f"Unexpected status {status}", resp.headers, None
            )
        payload = json.loads(resp.read().decode("utf-8"))

    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        signals = payload.get("signals")
        if isinstance(signals, list):
            return [row for row in signals if isinstance(row, dict)]
    return []


def ingest_feed_items(store: TradeStore, items: list[dict[str, Any]]) -> int:
    """Parse and insert new feed rows. Returns count of newly stored signals."""
    inserted = 0
    for item in items:
        parsed = parse_feed_signal(item)
        if parsed is None:
            continue
        if store.insert_external_signal_if_new(parsed):
            inserted += 1
    return inserted


class RadioTowerListener:
    """Background daemon thread that polls ai4trade.ai and logs to SQLite."""

    def __init__(
        self,
        store: TradeStore,
        *,
        feed_url: Optional[str] = None,
        poll_seconds: Optional[int] = None,
        retry_seconds: Optional[int] = None,
    ) -> None:
        self.store = store
        self.feed_url = feed_url or config.RADIO_TOWER_FEED_URL
        self.poll_seconds = poll_seconds or config.RADIO_TOWER_POLL_SECONDS
        self.retry_seconds = retry_seconds or config.RADIO_TOWER_RETRY_SECONDS
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_poll_at: Optional[float] = None
        self.last_inserted: int = 0
        self.last_error: str = ""
        self.total_polls: int = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poll_once(self) -> int:
        """Fetch the feed once and persist any new signals. Returns insert count."""
        items = fetch_operation_feed(self.feed_url)
        inserted = ingest_feed_items(self.store, items)
        self.last_poll_at = time.time()
        self.last_inserted = inserted
        self.last_error = ""
        self.total_polls += 1
        if inserted:
            logger.info("Radio Tower ingested %d new external signal(s).", inserted)
        else:
            logger.debug("Radio Tower poll complete — no new signals.")
        return inserted

    def _run(self) -> None:
        logger.info(
            "Radio Tower listener started (poll=%ds, feed=%s).",
            self.poll_seconds,
            self.feed_url,
        )
        while not self._stop_event.is_set():
            try:
                self.poll_once()
                if self._stop_event.wait(self.poll_seconds):
                    break
            except Exception as exc:
                safe = config.sanitize_for_log(str(exc))
                self.last_error = safe
                logger.warning(
                    "Radio Tower feed error: %s — retrying in %ds.",
                    safe,
                    self.retry_seconds,
                )
                if self._stop_event.wait(self.retry_seconds):
                    break
        logger.info("Radio Tower listener stopped.")

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="radio-tower",
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
