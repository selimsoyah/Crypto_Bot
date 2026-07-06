"""
threshold_sweep.py
====================
Phase 0 audit tool: measure OOS precision / recall / F1 per class at several
probability thresholds so the operator can see the trade-off between signal
frequency and signal quality.

On a 3-class problem the random baseline is ~0.33 per class. A threshold of
0.34 is barely above chance — this script makes that explicit with numbers.

Usage::

    python threshold_sweep.py

Writes ``threshold_sweep_report.md`` in the project root.
"""

from __future__ import annotations

import os
from typing import Final

import numpy as np
from sklearn.metrics import classification_report, confusion_matrix

import config
import data_pipeline
import model_brain
from feature_factory import FEATURE_COLUMNS, TARGET_COLUMN, build_feature_matrix

SWEEP_LEVELS: Final[tuple[float, ...]] = (0.34, 0.40, 0.45, 0.50, 0.55)
REPORT_PATH: Final[str] = str(config.BASE_DIR / "threshold_sweep_report.md")


def _load_oos_predictions() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (y_true, prob_long, prob_short) on the chronological test split."""
    raw = data_pipeline.load_historical_data()
    matrix = build_feature_matrix(raw)
    _train, test = model_brain.chronological_split(matrix)

    if not os.path.exists(config.MODEL_PATH):
        raise FileNotFoundError(
            f"Model artifact missing at {config.MODEL_PATH}. "
            "Run `python model_brain.py` first."
        )

    model = model_brain.load_model()
    x_test = test[FEATURE_COLUMNS]
    y_true = test[TARGET_COLUMN].to_numpy(dtype=int)
    proba = model.predict_proba(x_test)
    prob_long = proba[:, config.LABEL_LONG]
    prob_short = proba[:, config.LABEL_SHORT]
    return y_true, prob_long, prob_short


def _predict_directions(
    prob_long: np.ndarray,
    prob_short: np.ndarray,
    trend: np.ndarray,
    long_thr: float,
    short_thr: float,
) -> np.ndarray:
    """Apply ``model_brain.decide_direction`` row-wise."""
    preds = np.full(len(prob_long), config.LABEL_CASH, dtype=int)
    for i in range(len(prob_long)):
        direction = model_brain.decide_direction(
            float(prob_long[i]),
            float(prob_short[i]),
            float(trend[i]),
            long_thr,
            short_thr,
        )
        if direction == "LONG":
            preds[i] = config.LABEL_LONG
        elif direction == "SHORT":
            preds[i] = config.LABEL_SHORT
    return preds


def _signal_counts(preds: np.ndarray) -> dict[str, int]:
    return {
        "LONG": int((preds == config.LABEL_LONG).sum()),
        "SHORT": int((preds == config.LABEL_SHORT).sum()),
        "CASH": int((preds == config.LABEL_CASH).sum()),
    }


def run_sweep() -> str:
    y_true, prob_long, prob_short = _load_oos_predictions()
    trend = np.zeros(len(y_true))

    lines = [
        "# Threshold Sweep — OOS Class Metrics",
        "",
        f"**Model:** `{os.path.basename(config.MODEL_PATH)}`  ",
        f"**Test rows:** {len(y_true):,}  ",
        f"**Trend filter:** {'ON' if config.USE_TREND_FILTER else 'OFF'}  ",
        f"**Random baseline (3-class):** ~33.3% per class",
        "",
        "> **Audit note:** A threshold of **0.34** is only ~1 percentage point "
        "above the random baseline on a balanced 3-class problem. Higher "
        "thresholds typically reduce trade frequency but improve precision.",
        "",
        "---",
        "",
    ]

    for thr in SWEEP_LEVELS:
        preds = _predict_directions(prob_long, prob_short, trend, thr, thr)
        counts = _signal_counts(preds)
        trade_rows = counts["LONG"] + counts["SHORT"]

        report = classification_report(
            y_true,
            preds,
            labels=[config.LABEL_CASH, config.LABEL_SHORT, config.LABEL_LONG],
            target_names=["CASH", "SHORT", "LONG"],
            digits=3,
            zero_division=0,
        )
        cm = confusion_matrix(
            y_true,
            preds,
            labels=[config.LABEL_CASH, config.LABEL_SHORT, config.LABEL_LONG],
        )

        lines.extend(
            [
                f"## Threshold = {thr:.2f} (both LONG and SHORT)",
                "",
                f"- **Signals:** {counts['LONG']} LONG · {counts['SHORT']} SHORT · "
                f"{counts['CASH']} CASH",
                f"- **Directional bars (LONG+SHORT):** {trade_rows} "
                f"({100.0 * trade_rows / len(y_true):.1f}% of test set)",
                "",
                "### Classification report",
                "",
                "```",
                report.rstrip(),
                "```",
                "",
                "### Confusion matrix (rows=true, cols=predicted)",
                "",
                "| | CASH | SHORT | LONG |",
                "| :--- | ---: | ---: | ---: |",
            ]
        )
        for label, row in zip(["CASH", "SHORT", "LONG"], cm):
            lines.append(f"| **{label}** | {row[0]} | {row[1]} | {row[2]} |")
        lines.extend(["", "---", ""])

    if os.path.exists(config.THRESHOLD_PATH):
        long_t, short_t = model_brain.load_thresholds()
        lines.extend(
            [
                "## Current tuned sidecar (`decision_threshold.json`)",
                "",
                f"- LONG threshold: **{long_t:.3f}**",
                f"- SHORT threshold: **{short_t:.3f}**",
                "",
                "Live bot + dashboard resolve thresholds via "
                "`bot_loop.resolve_live_thresholds()`.",
                "",
            ]
        )

    markdown = "\n".join(lines)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    return markdown


if __name__ == "__main__":
    text = run_sweep()
    print(text)
    print(f"\nReport written to {REPORT_PATH}")
