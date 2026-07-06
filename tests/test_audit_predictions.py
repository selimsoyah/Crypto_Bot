"""Tests for audit_predictions ground-truth labeling."""

import numpy as np
import pandas as pd
import pytest

import config
from audit_predictions import (
    AUDIT_FORWARD_WINDOW,
    AUDIT_LONG_SL_PCT,
    AUDIT_LONG_TP_PCT,
    AUDIT_SHORT_SL_PCT,
    AUDIT_SHORT_TP_PCT,
    build_audit_ground_truth,
)


def _frame(closes, highs, lows):
    n = len(closes)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": np.ones(n),
        }
    )


def test_audit_long_label_triggers_on_tp_before_sl():
    n = AUDIT_FORWARD_WINDOW + 3
    closes = np.full(n, 100.0)
    highs = closes.copy()
    lows = closes.copy()
    highs[1] = 100.0 * (1.0 + AUDIT_LONG_TP_PCT) + 0.01
    labels = build_audit_ground_truth(_frame(closes, highs, lows))
    assert labels.iloc[0] == config.LABEL_LONG


def test_audit_short_label_triggers_on_tp_before_sl():
    n = AUDIT_FORWARD_WINDOW + 3
    closes = np.full(n, 100.0)
    highs = closes.copy()
    lows = closes.copy()
    lows[1] = 100.0 * (1.0 - AUDIT_SHORT_TP_PCT) - 0.01
    labels = build_audit_ground_truth(_frame(closes, highs, lows))
    assert labels.iloc[0] == config.LABEL_SHORT


def test_audit_long_sl_precludes_long_win_but_may_stay_cash():
    n = AUDIT_FORWARD_WINDOW + 3
    closes = np.full(n, 100.0)
    highs = closes.copy()
    lows = closes.copy()
    lows[1] = 100.0 * (1.0 - AUDIT_LONG_SL_PCT) - 0.01
    highs[2] = 100.0 * (1.0 + AUDIT_LONG_TP_PCT) + 0.01
    labels = build_audit_ground_truth(_frame(closes, highs, lows))
    assert labels.iloc[0] == config.LABEL_CASH


def test_audit_cash_when_both_directions_stopped_out():
    n = AUDIT_FORWARD_WINDOW + 3
    closes = np.full(n, 100.0)
    highs = closes.copy()
    lows = closes.copy()
    highs[1] = 100.0 * (1.0 + AUDIT_SHORT_SL_PCT) + 0.01
    lows[1] = 100.0 * (1.0 - AUDIT_LONG_SL_PCT) - 0.01
    labels = build_audit_ground_truth(_frame(closes, highs, lows))
    assert labels.iloc[0] == config.LABEL_CASH


def test_audit_tail_rows_are_nan():
    n = AUDIT_FORWARD_WINDOW + 5
    closes = np.linspace(100, 101, n)
    frame = _frame(closes, closes + 0.1, closes - 0.1)
    labels = build_audit_ground_truth(frame)
    assert labels.iloc[-AUDIT_FORWARD_WINDOW:].isna().all()
