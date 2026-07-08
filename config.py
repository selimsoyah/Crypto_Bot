"""
config.py
=========
Global settings, credentials, and shared constants for the BTC/USDT ML
trading bot.

This build targets the Binance **USDⓈ-M Futures Testnet**
(https://testnet.binancefuture.com) and a **multi-class directional** strategy
(LONG / SHORT / CASH), enabling the model to profit in both rising and falling
markets.

Credentials are resolved with the following priority:
    1. Environment variables (recommended for production / CI).
    2. Values inside a local ``.env`` file (loaded via python-dotenv).
    3. The hard-coded fallback defaults defined below.

Never commit real secrets. The defaults below are intentionally placeholders
for the Binance Futures Testnet.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final
from profiles import (
    PROFILE_DARVAS_BOX,
    PROFILE_XGBOOST_ML,
    SUPPORTED_PROFILES,
    build_profile_catalog,
    normalize_profile_name,
)

# --------------------------------------------------------------------------- #
# Optional .env loading                                                        #
# --------------------------------------------------------------------------- #
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    # python-dotenv is optional; if it is missing we silently fall back to the
    # real OS environment and the hard-coded defaults.
    pass


# --------------------------------------------------------------------------- #
# Project paths                                                               #
# --------------------------------------------------------------------------- #
BASE_DIR: Final[Path] = Path(__file__).resolve().parent


def _env(key: str, default: str) -> str:
    """Return the environment variable ``key`` or ``default`` if unset/empty."""
    value = os.getenv(key, "").strip()
    return value if value else default


# --------------------------------------------------------------------------- #
# Binance Testnet credentials & endpoints                                     #
# --------------------------------------------------------------------------- #
API_KEY: Final[str] = _env("API_KEY", "your_binance_testnet_key")
SECRET_KEY: Final[str] = _env("SECRET_KEY", "your_binance_testnet_secret")

# USDⓈ-M Futures Testnet gateway. All *order execution* is routed here.
FUTURES_TESTNET_URL: Final[str] = _env(
    "FUTURES_TESTNET_URL", "https://testnet.binancefuture.com"
)
# Legacy spot testnet URL retained for reference / fallback only.
TESTNET_URL: Final[str] = _env("TESTNET_URL", "https://testnet.binance.vision")

# Public mainnet Futures REST endpoint. The Testnet only exposes a short kline
# history, which is far too little to train a robust model. We therefore pull
# *market data* (historical + live signal candles) from the mainnet public
# futures endpoints (no authentication required for klines) while still routing
# all *order execution* to the Futures Testnet. Set USE_MAINNET_DATA=False to
# force the Testnet for data as well (not recommended).
MAINNET_URL: Final[str] = _env("MAINNET_URL", "https://api.binance.com")
MAINNET_FUTURES_URL: Final[str] = _env("MAINNET_FUTURES_URL", "https://fapi.binance.com")
USE_MAINNET_DATA: Final[bool] = _env("USE_MAINNET_DATA", "true").lower() in (
    "1",
    "true",
    "yes",
)

# --------------------------------------------------------------------------- #
# Futures execution parameters                                                #
# --------------------------------------------------------------------------- #
# Conservative institutional leverage (3x on testnet).
LEVERAGE: Final[int] = int(_env("LEVERAGE", "3"))
# Margin mode for the symbol: "ISOLATED" (recommended) or "CROSSED".
MARGIN_TYPE: Final[str] = _env("MARGIN_TYPE", "ISOLATED").upper()
# Quote asset that funds the futures wallet.
MARGIN_ASSET: Final[str] = "USDT"

# --------------------------------------------------------------------------- #
# Artifact / log file locations (kept relative to the project root)           #
# --------------------------------------------------------------------------- #
# SQLite database — the single source of truth for status logs and the
# ground-truth trades ledger (WAL mode; safe for bot-writer + dashboard-reader).
DB_FILE: Final[str] = str(BASE_DIR / _env("DB_FILE", "bot_status_log.db"))
# Legacy CSV path, now used only as the target of TradeStore.export_status_csv.
LOG_FILE: Final[str] = str(BASE_DIR / _env("LOG_FILE", "bot_status_log.csv"))
MODEL_PATH: Final[str] = str(BASE_DIR / _env("MODEL_PATH", "xgboost_trading_model.json"))
# Per-session CSV dossiers written on shutdown (metrics + full status log + trades).
SESSION_EXPORT_DIR: Final[str] = str(
    BASE_DIR / _env("SESSION_EXPORT_DIR", "session_exports")
)
# Sidecar JSON storing the data-driven optimal decision thresholds chosen during
# training (one per direction). The live bot reads this so it trades at the
# validated thresholds instead of a hard-coded guess.
THRESHOLD_PATH: Final[str] = str(BASE_DIR / "decision_threshold.json")

# --------------------------------------------------------------------------- #
# Trading profile — SWING (legacy 1h) vs COMPOUND (Path B active compounding) #
# --------------------------------------------------------------------------- #
TRADING_PROFILE: Final[str] = _env("TRADING_PROFILE", "COMPOUND").upper()
# Phase 3 feature variant (F0=baseline … F5=all extras). See feature_sweep.py.
FEATURE_VARIANT: Final[str] = _env("FEATURE_VARIANT", "F0").upper()

# Strategy runtime profile: xgboost_ml (legacy) or darvas_box (new breakout mode).
_ACTIVE_PROFILE_RAW = _env("ACTIVE_PROFILE", PROFILE_DARVAS_BOX)
ACTIVE_PROFILE: Final[str] = normalize_profile_name(_ACTIVE_PROFILE_RAW)
if ACTIVE_PROFILE not in SUPPORTED_PROFILES:
    ACTIVE_PROFILE = PROFILE_DARVAS_BOX

PROFILE_SETTINGS: Final[dict] = build_profile_catalog()
ACTIVE_PROFILE_SETTINGS: Final[dict] = PROFILE_SETTINGS[ACTIVE_PROFILE]


def is_compound_profile() -> bool:
    """Return ``True`` when Path B active-compounding settings are active."""
    return TRADING_PROFILE == "COMPOUND"


def is_swing_profile() -> bool:
    return TRADING_PROFILE == "SWING"


def is_xgboost_ml_profile() -> bool:
    return ACTIVE_PROFILE == PROFILE_XGBOOST_ML


def is_darvas_box_profile() -> bool:
    return ACTIVE_PROFILE == PROFILE_DARVAS_BOX


# --------------------------------------------------------------------------- #
# Market / strategy parameters (profile-aware defaults)                       #
# --------------------------------------------------------------------------- #
SYMBOL: Final[str] = "BTCUSDT"

BOX_LOOKBACK_CANDLES: Final[int] = int(
    _env(
        "BOX_LOOKBACK_CANDLES",
        str(ACTIVE_PROFILE_SETTINGS.get("box_lookback_candles", 20)),
    )
)
BOX_CONFIRMATION_CANDLES: Final[int] = int(
    _env(
        "BOX_CONFIRMATION_CANDLES",
        str(ACTIVE_PROFILE_SETTINGS.get("box_confirmation_candles", 3)),
    )
)
BOX_RISK_REWARD_RATIO: Final[float] = float(
    _env(
        "BOX_RISK_REWARD_RATIO",
        str(ACTIVE_PROFILE_SETTINGS.get("risk_to_reward_ratio", 2.0)),
    )
)
BOX_VOLUME_FILTER_MULTIPLIER: Final[float] = float(
    _env(
        "BOX_VOLUME_FILTER_MULTIPLIER",
        str(ACTIVE_PROFILE_SETTINGS.get("volume_filter_multiplier", 1.2)),
    )
)
BOX_STOP_BUFFER_PCT: Final[float] = float(
    _env("BOX_STOP_BUFFER_PCT", str(ACTIVE_PROFILE_SETTINGS.get("stop_buffer_pct", 0.0005)))
)

if is_darvas_box_profile():
    INTERVAL: Final[str] = _env("INTERVAL", "15m")
    HISTORY_YEARS: Final[int] = int(_env("HISTORY_YEARS", "2"))
    FORWARD_WINDOW: Final[int] = int(_env("FORWARD_WINDOW", "16"))
    STRUCTURE_LOOKBACK: Final[int] = int(_env("STRUCTURE_LOOKBACK", "96"))
    RETURN_LONG_LOOKBACK: Final[int] = int(_env("RETURN_LONG_LOOKBACK", "96"))
    TAKE_PROFIT_PCT: Final[float] = float(_env("TAKE_PROFIT_PCT", "0.008"))
    STOP_LOSS_PCT: Final[float] = float(_env("STOP_LOSS_PCT", "0.005"))
    HISTORICAL_PARQUET: Final[str] = str(
        BASE_DIR / _env("HISTORICAL_PARQUET", "historical_btc_15m.parquet")
    )
elif is_compound_profile():
    # 15m bars — more setups per day for compounding-style activity.
    INTERVAL: Final[str] = _env("INTERVAL", "15m")
    HISTORY_YEARS: Final[int] = int(_env("HISTORY_YEARS", "2"))
    # Label horizon: 16 × 15m = 4 hours forward (triple-barrier scalp labels).
    FORWARD_WINDOW: Final[int] = int(_env("FORWARD_WINDOW", "16"))
    # Structure features: 96 × 15m = 24 hours lookback.
    STRUCTURE_LOOKBACK: Final[int] = int(_env("STRUCTURE_LOOKBACK", "96"))
    RETURN_LONG_LOOKBACK: Final[int] = int(_env("RETURN_LONG_LOOKBACK", "96"))
    # Scalp brackets — single source of truth for labels, backtest, and live execution.
    # LONG: +0.80% TP / −0.50% SL (1.6:1 R:R) · symmetric triple-barrier for shorts.
    TAKE_PROFIT_PCT: Final[float] = float(_env("TAKE_PROFIT_PCT", "0.008"))
    STOP_LOSS_PCT: Final[float] = float(_env("STOP_LOSS_PCT", "0.005"))
    HISTORICAL_PARQUET: Final[str] = str(
        BASE_DIR / _env("HISTORICAL_PARQUET", "historical_btc_15m.parquet")
    )
else:
    INTERVAL: Final[str] = _env("INTERVAL", "1h")
    HISTORY_YEARS: Final[int] = int(_env("HISTORY_YEARS", "4"))
    FORWARD_WINDOW: Final[int] = int(_env("FORWARD_WINDOW", "24"))
    STRUCTURE_LOOKBACK: Final[int] = int(_env("STRUCTURE_LOOKBACK", "24"))
    RETURN_LONG_LOOKBACK: Final[int] = int(_env("RETURN_LONG_LOOKBACK", "24"))
    TAKE_PROFIT_PCT: Final[float] = float(_env("TAKE_PROFIT_PCT", "0.012"))
    STOP_LOSS_PCT: Final[float] = float(_env("STOP_LOSS_PCT", "0.006"))
    HISTORICAL_PARQUET: Final[str] = str(
        BASE_DIR / _env("HISTORICAL_PARQUET", "historical_btc.parquet")
    )

# --------------------------------------------------------------------------- #
# Multi-class directional labels                                              #
# --------------------------------------------------------------------------- #
# The model is a 3-class classifier. These integer codes are shared across the
# feature factory, model brain, bot loop, and dashboard.
LABEL_CASH: Final[int] = 0   # choppy / sideways -> stay flat
LABEL_SHORT: Final[int] = 1  # downward setup    -> open short
LABEL_LONG: Final[int] = 2   # upward setup      -> open long
NUM_CLASSES: Final[int] = 3
# Sentinel: probabilities are in [0, 1], so 1.01 disables a direction at runtime.
THRESHOLD_DISABLED: Final[float] = 1.01
DIRECTION_NAMES: Final[dict[int, str]] = {
    LABEL_CASH: "CASH",
    LABEL_SHORT: "SHORT",
    LABEL_LONG: "LONG",
}

# Legacy EMA200 regime filter (longs only above EMA200, shorts only below).
USE_TREND_FILTER: Final[bool] = _env("USE_TREND_FILTER", "false").lower() in (
    "1",
    "true",
    "yes",
)
# Primary momentum gate on 15m bars: block longs below EMA50, shorts above EMA50.
_EMA50_GATE_DEFAULT = "true" if is_compound_profile() else "false"
USE_EMA50_TREND_GATE: Final[bool] = _env("USE_EMA50_TREND_GATE", _EMA50_GATE_DEFAULT).lower() in (
    "1",
    "true",
    "yes",
)
# Programmatic short inversion when long probability is very low (bearish conviction).
ENABLE_LONG_INVERSION: Final[bool] = _env("ENABLE_LONG_INVERSION", "true").lower() in (
    "1",
    "true",
    "yes",
)
LONG_INVERSION_THRESHOLD: Final[float] = float(
    _env("LONG_INVERSION_THRESHOLD", "0.40")
)

# FALLBACK live execution thresholds (per direction). The live bot and the
# dashboard both resolve thresholds through ``model_brain.load_thresholds()``:
# the data-driven tuned values in ``decision_threshold.json`` take priority and
# these constants are only used when that sidecar file is missing.
# NOTE: on a 3-class problem the random baseline is ~0.33, so 0.34 is barely
# above chance — see threshold_sweep.py for the measured precision/recall
# trade-off at each level before choosing a live value.
if is_compound_profile():
    BUY_PROBABILITY_THRESHOLD: Final[float] = float(_env("LONG_THRESHOLD", "0.38"))
    LONG_PROBABILITY_THRESHOLD: Final[float] = float(_env("LONG_THRESHOLD", "0.38"))
    SHORT_PROBABILITY_THRESHOLD: Final[float] = float(_env("SHORT_THRESHOLD", "0.38"))
    THRESHOLD_SEARCH_MIN: Final[float] = float(_env("THRESHOLD_SEARCH_MIN", "0.34"))
    THRESHOLD_SEARCH_MAX: Final[float] = float(_env("THRESHOLD_SEARCH_MAX", "0.85"))
    THRESHOLD_SEARCH_STEP: Final[float] = float(_env("THRESHOLD_SEARCH_STEP", "0.01"))
    MIN_VALIDATION_TRADES: Final[int] = int(_env("MIN_VALIDATION_TRADES", "5"))
    COMPOUND_MAX_THRESHOLD: Final[float] = float(_env("COMPOUND_MAX_THRESHOLD", "0.85"))
    # Per-direction fallbacks when no organic positive-PnL threshold qualifies.
    LONG_FALLBACK_THRESHOLD: Final[float] = float(_env("LONG_FALLBACK_THRESHOLD", "0.78"))
    SHORT_FALLBACK_THRESHOLD: Final[float] = float(_env("SHORT_FALLBACK_THRESHOLD", "0.60"))
    # Back-compat alias for long-side fallback.
    FALLBACK_THRESHOLD: Final[float] = LONG_FALLBACK_THRESHOLD
    CASH_ALLOCATION_PCT: Final[float] = float(_env("CASH_ALLOCATION_PCT", "0.12"))
    # Exchange minimum notional — sizing clamp only when % allocation is below this.
    EXCHANGE_MIN_NOTIONAL_USDT: Final[float] = float(
        _env("EXCHANGE_MIN_NOTIONAL_USDT", "50.0")
    )
    MIN_ORDER_USDT_FLOOR: Final[float] = float(_env("MIN_ORDER_USDT_FLOOR", "60.0"))
    LOOP_SLEEP_SECONDS: Final[int] = int(_env("LOOP_SLEEP_SECONDS", "5"))
    LIVE_CANDLE_LOOKBACK: Final[int] = int(_env("LIVE_CANDLE_LOOKBACK", "500"))
else:
    BUY_PROBABILITY_THRESHOLD: Final[float] = 0.34
    LONG_PROBABILITY_THRESHOLD: Final[float] = 0.34
    SHORT_PROBABILITY_THRESHOLD: Final[float] = 0.34
    THRESHOLD_SEARCH_MIN: Final[float] = 0.40
    THRESHOLD_SEARCH_MAX: Final[float] = 0.85
    THRESHOLD_SEARCH_STEP: Final[float] = 0.025
    MIN_VALIDATION_TRADES: Final[int] = 10
    COMPOUND_MAX_THRESHOLD: Final[float] = 0.85
    LONG_FALLBACK_THRESHOLD: Final[float] = 0.78
    SHORT_FALLBACK_THRESHOLD: Final[float] = 0.60
    FALLBACK_THRESHOLD: Final[float] = LONG_FALLBACK_THRESHOLD
    CASH_ALLOCATION_PCT: Final[float] = float(_env("CASH_ALLOCATION_PCT", "0.25"))
    EXCHANGE_MIN_NOTIONAL_USDT: Final[float] = float(
        _env("EXCHANGE_MIN_NOTIONAL_USDT", "50.0")
    )
    MIN_ORDER_USDT_FLOOR: Final[float] = 60.0
    LOOP_SLEEP_SECONDS: Final[int] = int(_env("LOOP_SLEEP_SECONDS", "7"))
    LIVE_CANDLE_LOOKBACK: Final[int] = 350

# Path B — compounding behaviour (active when TRADING_PROFILE=COMPOUND).
USE_ATR_BRACKETS: Final[bool] = _env("USE_ATR_BRACKETS", "true").lower() in (
    "1", "true", "yes",
)
ATR_BRACKET_TP_MULT: Final[float] = float(_env("ATR_BRACKET_TP_MULT", "1.2"))
ATR_BRACKET_SL_MULT: Final[float] = float(_env("ATR_BRACKET_SL_MULT", "0.6"))
TRAILING_STOP_ENABLED: Final[bool] = _env("TRAILING_STOP_ENABLED", "true").lower() in (
    "1", "true", "yes",
)
TRAILING_STOP_ACTIVATION_PCT: Final[float] = float(
    _env("TRAILING_STOP_ACTIVATION_PCT", "0.004")
)
TRAILING_STOP_DISTANCE_PCT: Final[float] = float(
    _env("TRAILING_STOP_DISTANCE_PCT", "0.002")
)
REENTRY_COOLDOWN_SECONDS: Final[int] = int(
    _env("REENTRY_COOLDOWN_SECONDS", "60" if is_compound_profile() else "0")
)
# After a stop-loss exit, block all new entries for N complete bars (4 × 15m = 1h).
POST_SL_COOLDOWN_BARS: Final[int] = int(_env("POST_SL_COOLDOWN_BARS", "4"))
# Emergency flatten uses market orders (not post-only) for circuit-breaker exits.
CIRCUIT_BREAKER_USE_MARKET_FLATTEN: Final[bool] = _env(
    "CIRCUIT_BREAKER_USE_MARKET_FLATTEN", "true"
).lower() in ("1", "true", "yes")
COMPOUND_WIN_STREAK_BOOST: Final[float] = float(_env("COMPOUND_WIN_STREAK_BOOST", "1.05"))
COMPOUND_LOSS_STREAK_CUT: Final[float] = float(_env("COMPOUND_LOSS_STREAK_CUT", "0.90"))
COMPOUND_MAX_SIZE_MULT: Final[float] = float(_env("COMPOUND_MAX_SIZE_MULT", "1.25"))
COMPOUND_MIN_SIZE_MULT: Final[float] = float(_env("COMPOUND_MIN_SIZE_MULT", "0.50"))
COMPOUND_STREAK_CAP: Final[int] = int(_env("COMPOUND_STREAK_CAP", "3"))
RISK_MAX_WEEKLY_LOSS_PCT: Final[float] = float(_env("RISK_MAX_WEEKLY_LOSS_PCT", "0.05"))

# --------------------------------------------------------------------------- #
# Backtest / walk-forward rigor (Phase 1)                                     #
# --------------------------------------------------------------------------- #
# Binance USDⓈ-M standard taker fee (0.04% per side, VIP0 baseline).
BACKTEST_TAKER_FEE: Final[float] = float(_env("BACKTEST_TAKER_FEE", "0.0004"))
# Estimated slippage per side in basis points (BTCUSDT at ~$3–4k notional).
BACKTEST_SLIPPAGE_BPS: Final[float] = float(_env("BACKTEST_SLIPPAGE_BPS", "3.0"))
# Walk-forward: minimum 5 expanding-window folds over the feature matrix.
WALK_FORWARD_FOLDS: Final[int] = int(_env("WALK_FORWARD_FOLDS", "5"))
WALK_FORWARD_MIN_TRAIN_FRAC: Final[float] = float(_env("WALK_FORWARD_MIN_TRAIN_FRAC", "0.50"))
BACKTEST_REPORT_PATH: Final[str] = str(BASE_DIR / "backtest_report.md")
# Risk-free rate for Sharpe/Sortino annualisation (USDT stablecoin ~0).
BACKTEST_RISK_FREE_RATE: Final[float] = float(_env("BACKTEST_RISK_FREE_RATE", "0.0"))

# --------------------------------------------------------------------------- #
# Risk engine (Phase 2) — hard limits between signal and execution            #
# --------------------------------------------------------------------------- #
# Session circuit breaker: halt if realized + unrealized PnL breaches this
# fraction of the session-starting equity (e.g. -3%).
RISK_MAX_DAILY_LOSS_PCT: Final[float] = float(_env("RISK_MAX_DAILY_LOSS_PCT", "0.03"))
# Pause new entries after this many consecutive losing closes; requires manual
# resume via dashboard / risk_engine.confirm_manual_resume().
RISK_MAX_CONSECUTIVE_LOSSES: Final[int] = int(_env("RISK_MAX_CONSECUTIVE_LOSSES", "4"))
# ATR% baseline for vol-scaling; higher ATR shrinks margin.
_RISK_ATR_DEFAULT = "0.004" if is_compound_profile() else "0.008"
RISK_ATR_BASELINE_PCT: Final[float] = float(_env("RISK_ATR_BASELINE_PCT", _RISK_ATR_DEFAULT))
# Minimum vol scale factor (never size below 25% of the base allocation).
RISK_VOL_SCALE_FLOOR: Final[float] = float(_env("RISK_VOL_SCALE_FLOOR", "0.25"))
# Reject orders if price deviates more than this from the last good tick.
RISK_ORDER_PRICE_DEVIATION_PCT: Final[float] = float(
    _env("RISK_ORDER_PRICE_DEVIATION_PCT", "0.015")
)
KILL_SWITCH_FILE: Final[str] = str(BASE_DIR / _env("KILL_SWITCH_FILE", ".bot_kill_switch"))
RISK_MANUAL_RESUME_FILE: Final[str] = str(
    BASE_DIR / _env("RISK_MANUAL_RESUME_FILE", ".bot_risk_manual_resume_required")
)
# Recommendation for initial live period — NOT applied automatically; see Phase 5.
RISK_RECOMMENDED_LIVE_LEVERAGE: Final[int] = int(_env("RISK_RECOMMENDED_LIVE_LEVERAGE", "2"))

# --------------------------------------------------------------------------- #
# Live-readiness / execution venue (Phase 3)                                  #
# --------------------------------------------------------------------------- #
# Where orders are routed: TESTNET (default, safe) or LIVE (mainnet futures).
# LIVE requires explicit operator confirmation in the dashboard before boot.
EXECUTION_VENUE: Final[str] = _env("EXECUTION_VENUE", "TESTNET").upper()

# Exchange API retry — exponential backoff on transient failures.
EXCHANGE_RETRY_ATTEMPTS: Final[int] = int(_env("EXCHANGE_RETRY_ATTEMPTS", "4"))
EXCHANGE_RETRY_BASE_DELAY: Final[float] = float(_env("EXCHANGE_RETRY_BASE_DELAY", "0.5"))
# Consecutive API failures before the bot marks connection as degraded.
EXCHANGE_DEGRADED_THRESHOLD: Final[int] = int(_env("EXCHANGE_DEGRADED_THRESHOLD", "3"))
# Failures before attempting a client reconnect mid-session.
EXCHANGE_RECONNECT_THRESHOLD: Final[int] = int(_env("EXCHANGE_RECONNECT_THRESHOLD", "5"))

# --------------------------------------------------------------------------- #
# Live execution — audit-parity maker fills (COMPOUND / F2 audit alignment)   #
# --------------------------------------------------------------------------- #
# When true: fixed audit brackets (+0.8% / −0.5%), no ATR scaling, no
# trailing stop, and FORWARD_WINDOW bar timeout exits (matches audit_predictions).
_EXEC_AUDIT_DEFAULT = "true" if is_compound_profile() else "false"
EXECUTION_AUDIT_PARITY: Final[bool] = _env(
    "EXECUTION_AUDIT_PARITY", _EXEC_AUDIT_DEFAULT
).lower() in ("1", "true", "yes")
# Route all entries/exits as post-only GTX limit orders (maker / zero slippage).
_POST_ONLY_DEFAULT = "true" if is_compound_profile() else "false"
USE_POST_ONLY_MAKER: Final[bool] = _env(
    "USE_POST_ONLY_MAKER", _POST_ONLY_DEFAULT
).lower() in ("1", "true", "yes")
# Pre-entry spread gate: (ask − bid) / bid must be ≤ this fraction (0.02%).
MAX_ENTRY_SPREAD_PCT: Final[float] = float(_env("MAX_ENTRY_SPREAD_PCT", "0.0002"))
# How long to wait for a resting post-only limit to fill before aborting.
LIMIT_ORDER_FILL_TIMEOUT_SEC: Final[float] = float(
    _env("LIMIT_ORDER_FILL_TIMEOUT_SEC", "30")
)
LIMIT_ORDER_POLL_INTERVAL_SEC: Final[float] = float(
    _env("LIMIT_ORDER_POLL_INTERVAL_SEC", "0.5")
)
# Binance USDⓈ-M VIP0 maker fee baseline (for backtest cost modelling).
BACKTEST_MAKER_FEE: Final[float] = float(_env("BACKTEST_MAKER_FEE", "0.0002"))

# Optional external alerting (Telegram). Falls back to logging when unset.
TELEGRAM_BOT_TOKEN: Final[str] = _env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: Final[str] = _env("TELEGRAM_CHAT_ID", "")
# Minimum seconds between duplicate alert keys (rate limiting).
ALERT_RATE_LIMIT_SECONDS: Final[int] = int(_env("ALERT_RATE_LIMIT_SECONDS", "60"))

# --------------------------------------------------------------------------- #
# Logging configuration                                                       #
# --------------------------------------------------------------------------- #
LOG_LEVEL: Final[int] = logging.INFO

# Legacy CSV export schema (the live store is SQLite; see trade_store.py).
# ``Event`` is a short machine category used by the dashboard for colour-coding
# (e.g. BUY_LONG, SHORT_ORDER, STOP_LOSS, FILL_SUCCESS, CASH, WAIT, WARNING),
# while ``Reason`` is a full human-readable sentence explaining the decision.
LOG_COLUMNS: Final[list[str]] = [
    "Timestamp",
    "Current_Price",
    "Prob_Long",
    "Prob_Short",
    "Prob_Cash",
    "Direction",
    "Current_Balance",
    "Open_Position",
    "Realized_PNL",
    "Unrealized_PNL",
    "Action",
    "Event",
    "Reason",
]


def configure_logging(name: str | None = None) -> logging.Logger:
    """Return a module logger configured with a consistent format.

    Parameters
    ----------
    name:
        Optional logger name. Defaults to the root project logger.
    """
    logger = logging.getLogger(name if name else "crypto_bot")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(LOG_LEVEL)
        logger.propagate = False
    return logger


def credentials_present() -> bool:
    """Return ``True`` when non-placeholder API credentials are configured."""
    return (
        API_KEY not in ("", "your_binance_testnet_key")
        and SECRET_KEY not in ("", "your_binance_testnet_secret")
    )


def execution_is_live() -> bool:
    """Return ``True`` when order execution targets mainnet futures."""
    return EXECUTION_VENUE == "LIVE"


def execution_banner_text() -> str:
    """Human-readable venue banner for logs and dashboard."""
    exec_label = "LIVE MAINNET" if execution_is_live() else "TESTNET"
    data_label = "Mainnet" if USE_MAINNET_DATA else "Testnet"
    profile = f"{TRADING_PROFILE}/{ACTIVE_PROFILE}"
    return (
        f"EXECUTION: {exec_label} | MARKET DATA: {data_label} | "
        f"{SYMBOL} {INTERVAL} {LEVERAGE}x | {profile}"
    )


def profile_summary() -> str:
    """One-line description of the active trading profile."""
    if is_darvas_box_profile():
        return (
            f"Darvas Box mode — {INTERVAL} bars, lookback {BOX_LOOKBACK_CANDLES}, "
            f"confirm {BOX_CONFIRMATION_CANDLES}, RR {BOX_RISK_REWARD_RATIO:.2f}, "
            f"vol x{BOX_VOLUME_FILTER_MULTIPLIER:.2f}"
        )
    if is_compound_profile():
        return (
            f"Compound mode — {INTERVAL} bars, TP/SL {TAKE_PROFIT_PCT:.2%}/"
            f"{STOP_LOSS_PCT:.2%}, {CASH_ALLOCATION_PCT:.0%} alloc, "
            f"trail {'on' if TRAILING_STOP_ENABLED else 'off'}"
        )
    return f"Swing mode — {INTERVAL} bars, selective thresholds"


def validate_execution_config() -> list[str]:
    """Return config errors that must block engine startup."""
    errors: list[str] = []
    if EXECUTION_VENUE not in ("TESTNET", "LIVE"):
        errors.append(
            f"EXECUTION_VENUE must be TESTNET or LIVE (got {EXECUTION_VENUE!r})."
        )
    if ACTIVE_PROFILE not in SUPPORTED_PROFILES:
        errors.append(
            f"ACTIVE_PROFILE must be one of {SUPPORTED_PROFILES} (got {ACTIVE_PROFILE!r})."
        )
    if execution_is_live() and not credentials_present():
        errors.append(
            "EXECUTION_VENUE=LIVE but API credentials are missing or placeholders."
        )
    return errors


_PLACEHOLDER_SECRETS = frozenset(
    {"", "your_binance_testnet_key", "your_binance_testnet_secret"}
)


def sanitize_for_log(text: str) -> str:
    """Redact configured API secrets from log/alert strings."""
    out = text
    for secret in (API_KEY, SECRET_KEY):
        if secret and secret not in _PLACEHOLDER_SECRETS and len(secret) > 6:
            out = out.replace(secret, "[REDACTED]")
    return out


def secrets_leak_detected(text: str) -> bool:
    """Return ``True`` if ``text`` appears to contain a configured secret."""
    for secret in (API_KEY, SECRET_KEY):
        if secret and secret not in _PLACEHOLDER_SECRETS and secret in text:
            return True
    return False
