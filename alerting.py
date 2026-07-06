"""
alerting.py
===========
Optional external notifications for critical bot events.

When ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are configured, alerts
are sent via the Telegram Bot API. Otherwise messages are logged only.
Duplicate alerts are rate-limited per ``key``.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import config

logger = config.configure_logging(__name__)

_lock = threading.Lock()
_last_sent: dict[str, float] = {}


def telegram_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _should_send(key: Optional[str]) -> bool:
    if not key:
        return True
    now = time.monotonic()
    with _lock:
        last = _last_sent.get(key, 0.0)
        if now - last < config.ALERT_RATE_LIMIT_SECONDS:
            return False
        _last_sent[key] = now
    return True


def _send_telegram(text: str) -> bool:
    if not telegram_configured():
        return False
    url = (
        f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    )
    payload = urllib.parse.urlencode(
        {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return bool(body.get("ok"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Telegram alert failed: %s", exc)
        return False


def send_alert(
    level: str,
    title: str,
    message: str,
    *,
    key: Optional[str] = None,
) -> None:
    """Send an alert to Telegram (if configured) and always log locally."""
    safe_msg = config.sanitize_for_log(message)
    if config.secrets_leak_detected(safe_msg):
        safe_msg = "[message redacted — contained secret-like content]"

    line = f"[{level.upper()}] {title}: {safe_msg}"
    if level.upper() in ("CRITICAL", "ERROR"):
        logger.error(line)
    elif level.upper() == "WARNING":
        logger.warning(line)
    else:
        logger.info(line)

    if not _should_send(key):
        return

    venue = "LIVE" if config.execution_is_live() else "TESTNET"
    telegram_text = f"🤖 Crypto Bot ({venue})\n{line}"
    if telegram_configured():
        _send_telegram(telegram_text)
