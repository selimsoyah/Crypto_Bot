"""
label_sweep.py
================
Phase 1 retrain experiment: compare triple-barrier label configurations on the
current COMPOUND 15m feature set without overwriting the production model.

Runs variants L1–L4 (forward window and TP/SL grid), reporting per variant:
    * Label distribution (LONG / SHORT / CASH %)
    * OOS log loss vs train-frequency naive baseline
    * Walk-forward mean net PnL, Sharpe, and fold win rate (5 folds)

Usage::

    python label_sweep.py           # full sweep (walk-forward per variant)
    python label_sweep.py --quick   # labels + calibration only (faster)

Writes ``label_sweep_report.md`` in the project root.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Iterator

import numpy as np
import pandas as pd
from sklearn.metrics import log_loss

import config
import data_pipeline
import model_brain
from backtest_runner import run_fold, walk_forward_splits
from feature_factory import FEATURE_COLUMNS, TARGET_COLUMN, build_feature_matrix

REPORT_PATH: Final[str] = str(config.BASE_DIR / "label_sweep_report.md")


@dataclass(frozen=True)
class LabelVariant:
    variant_id: str
    name: str
    forward_window: int
    take_profit_pct: float
    stop_loss_pct: float


VARIANTS: Final[tuple[LabelVariant, ...]] = (
    LabelVariant("L1", "Current control", 16, 0.004, 0.0025),
    LabelVariant("L2", "Shorter horizon (12 bars)", 12, 0.004, 0.0025),
    LabelVariant("L3", "Longer horizon (24 bars)", 24, 0.004, 0.0025),
    LabelVariant("L4", "Wider brackets (+0.8% / -0.4%)", 16, 0.008, 0.004),
)


@dataclass
class VariantResult:
    variant: LabelVariant
    rows: int
    pct_long: float
    pct_short: float
    pct_cash: float
    model_log_loss: float
    baseline_log_loss: float
    beats_baseline: bool
    tuned_long_thr: float
    tuned_short_thr: float
    val_long_pnl: float
    val_short_pnl: float
    wf_mean_pnl: float | None
    wf_std_pnl: float | None
    wf_positive_folds: int | None
    wf_total_folds: int | None
    wf_mean_sharpe: float | None


@contextmanager
def _label_params(variant: LabelVariant) -> Iterator[None]:
    """Align walk-forward backtest barriers with the variant label settings."""
    saved = (
        config.FORWARD_WINDOW,
        config.TAKE_PROFIT_PCT,
        config.STOP_LOSS_PCT,
    )
    try:
        config.FORWARD_WINDOW = variant.forward_window
        config.TAKE_PROFIT_PCT = variant.take_profit_pct
        config.STOP_LOSS_PCT = variant.stop_loss_pct
        yield
    finally:
        config.FORWARD_WINDOW = saved[0]
        config.TAKE_PROFIT_PCT = saved[1]
        config.STOP_LOSS_PCT = saved[2]


def _label_distribution(matrix: pd.DataFrame) -> tuple[float, float, float]:
    dist = matrix[TARGET_COLUMN].value_counts(normalize=True)
    return (
        100.0 * dist.get(config.LABEL_LONG, 0.0),
        100.0 * dist.get(config.LABEL_SHORT, 0.0),
        100.0 * dist.get(config.LABEL_CASH, 0.0),
    )


def _brier_multiclass(y: np.ndarray, proba: np.ndarray) -> float:
    y_oh = np.zeros_like(proba)
    y_oh[np.arange(len(y)), y.astype(int)] = 1
    return float(np.mean(np.sum((proba - y_oh) ** 2, axis=1)))


def _calibration_metrics(
    matrix: pd.DataFrame,
) -> tuple[float, float, bool, float, float, float, float]:
    """Train with new optimizer rules; return log loss and tuned thresholds."""
    train_df, test_df = model_brain.chronological_split(matrix)
    val_split = int(len(train_df) * 0.85)
    sub_train = train_df.iloc[:val_split].reset_index(drop=True)
    valid = train_df.iloc[val_split:].reset_index(drop=True)

    tuning_model = model_brain._build_model()
    model_brain._fit_balanced(
        tuning_model, sub_train[FEATURE_COLUMNS], sub_train[TARGET_COLUMN]
    )
    proba_valid = tuning_model.predict_proba(valid[FEATURE_COLUMNS])
    long_thr, short_thr, long_vp, short_vp = model_brain.optimize_thresholds(
        valid, proba_valid
    )

    model = model_brain._build_model()
    model_brain._fit_balanced(model, train_df[FEATURE_COLUMNS], train_df[TARGET_COLUMN])
    x_test = test_df[FEATURE_COLUMNS]
    y_test = test_df[TARGET_COLUMN].to_numpy()
    proba = model.predict_proba(x_test)

    train_freq = train_df[TARGET_COLUMN].value_counts(normalize=True).sort_index()
    baseline = np.zeros_like(proba)
    for i in range(config.NUM_CLASSES):
        baseline[:, i] = train_freq.get(i, 1.0 / config.NUM_CLASSES)

    model_ll = log_loss(y_test, proba, labels=[0, 1, 2])
    base_ll = log_loss(y_test, baseline, labels=[0, 1, 2])
    return (
        model_ll,
        base_ll,
        model_ll < base_ll,
        long_thr,
        short_thr,
        long_vp,
        short_vp,
    )


def _walk_forward_summary(matrix: pd.DataFrame) -> tuple[float, float, int, int, float]:
    splits = walk_forward_splits(matrix)
    folds = [run_fold(i + 1, tr, te) for i, (tr, te) in enumerate(splits)]
    pnls = [f.backtest.strategy_net_profit_pct for f in folds]
    sharpes = [f.backtest.sharpe for f in folds]
    positive = sum(1 for p in pnls if p > 0)
    return (
        float(np.mean(pnls)),
        float(np.std(pnls)),
        positive,
        len(pnls),
        float(np.mean(sharpes)),
    )


def evaluate_variant(variant: LabelVariant, raw: pd.DataFrame, quick: bool) -> VariantResult:
    matrix = build_feature_matrix(
        raw,
        forward_window=variant.forward_window,
        take_profit_pct=variant.take_profit_pct,
        stop_loss_pct=variant.stop_loss_pct,
    )
    pct_long, pct_short, pct_cash = _label_distribution(matrix)
    (
        model_ll,
        base_ll,
        beats,
        long_thr,
        short_thr,
        long_vp,
        short_vp,
    ) = _calibration_metrics(matrix)

    wf_mean = wf_std = wf_mean_sharpe = None
    wf_pos = wf_total = None
    if not quick:
        with _label_params(variant):
            wf_mean, wf_std, wf_pos, wf_total, wf_mean_sharpe = _walk_forward_summary(matrix)

    return VariantResult(
        variant=variant,
        rows=len(matrix),
        pct_long=pct_long,
        pct_short=pct_short,
        pct_cash=pct_cash,
        model_log_loss=model_ll,
        baseline_log_loss=base_ll,
        beats_baseline=beats,
        tuned_long_thr=long_thr,
        tuned_short_thr=short_thr,
        val_long_pnl=long_vp,
        val_short_pnl=short_vp,
        wf_mean_pnl=wf_mean,
        wf_std_pnl=wf_std,
        wf_positive_folds=wf_pos,
        wf_total_folds=wf_total,
        wf_mean_sharpe=wf_mean_sharpe,
    )


def _format_thr(thr: float) -> str:
    if thr >= config.THRESHOLD_DISABLED - 1e-6:
        return "DISABLED"
    return f"{thr:.3f}"


def write_report(results: list[VariantResult], quick: bool) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# COMPOUND 15m Label Sweep Report",
        "",
        f"**Generated:** {now}  ",
        f"**Profile:** {config.TRADING_PROFILE} · **Interval:** {config.INTERVAL}  ",
        f"**Mode:** {'quick (calibration only)' if quick else 'full (walk-forward per variant)'}  ",
        "",
        "Compares triple-barrier label settings before committing to a retrain. "
        f"Threshold optimizer: min {config.MIN_VALIDATION_TRADES} val trades, "
        f"fallback L{config.LONG_FALLBACK_THRESHOLD}/S{config.SHORT_FALLBACK_THRESHOLD}.",
        "",
        "## Summary",
        "",
        "| ID | Horizon | TP / SL | LONG% | SHORT% | CASH% | Log loss | Beats baseline? | "
        "Thr L/S | Val PnL L/S | WF mean PnL | WF + folds |",
        "| :--- | ---: | :--- | ---: | ---: | ---: | ---: | :--- | :--- | :--- | ---: | :--- |",
    ]

    for r in results:
        v = r.variant
        wf_pnl = "—" if r.wf_mean_pnl is None else f"{r.wf_mean_pnl:+.2f}%"
        wf_folds = (
            "—"
            if r.wf_positive_folds is None
            else f"{r.wf_positive_folds}/{r.wf_total_folds}"
        )
        lines.append(
            f"| **{v.variant_id}** | {v.forward_window} bars | "
            f"+{v.take_profit_pct:.2%} / -{v.stop_loss_pct:.2%} | "
            f"{r.pct_long:.1f}% | {r.pct_short:.1f}% | {r.pct_cash:.1f}% | "
            f"{r.model_log_loss:.4f} | {'✅' if r.beats_baseline else '❌'} | "
            f"{_format_thr(r.tuned_long_thr)} / {_format_thr(r.tuned_short_thr)} | "
            f"{r.val_long_pnl:+.2f}% / {r.val_short_pnl:+.2f}% | {wf_pnl} | {wf_folds} |"
        )

    lines += [
        "",
        "## Variant notes",
        "",
    ]
    for r in results:
        v = r.variant
        lines.append(f"### {v.variant_id} — {v.name}")
        lines.append("")
        lines.append(f"- Rows after feature drop: **{r.rows:,}**")
        lines.append(
            f"- Calibration: model log loss **{r.model_log_loss:.4f}** vs baseline "
            f"**{r.baseline_log_loss:.4f}**"
        )
        if r.wf_mean_pnl is not None:
            lines.append(
                f"- Walk-forward: mean PnL **{r.wf_mean_pnl:+.2f}%** "
                f"(σ {r.wf_std_pnl:.2f}%), mean Sharpe **{r.wf_mean_sharpe:.2f}**, "
                f"positive folds **{r.wf_positive_folds}/{r.wf_total_folds}**"
            )
        lines.append("")

    best_baseline = [r for r in results if r.beats_baseline]
    if best_baseline:
        pick = min(best_baseline, key=lambda r: -(r.wf_mean_pnl or -999))
        lines.append(
            f"**Suggested next step:** Investigate **{pick.variant.variant_id}** "
            f"({pick.variant.name}) — beats log-loss baseline"
            + (
                f" with walk-forward mean {pick.wf_mean_pnl:+.2f}%."
                if pick.wf_mean_pnl is not None
                else "."
            )
        )
    else:
        lines.append(
            "**Suggested next step:** No variant beat the log-loss baseline — "
            "proceed to feature experiments (Phase 3) before retraining."
        )
    lines.append("")

    text = "\n".join(lines)
    with open(REPORT_PATH, "w") as fh:
        fh.write(text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="COMPOUND 15m label configuration sweep")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip walk-forward folds (labels + calibration only)",
    )
    args = parser.parse_args()

    raw = data_pipeline.load_historical_data()
    results = [evaluate_variant(v, raw, quick=args.quick) for v in VARIANTS]
    report = write_report(results, quick=args.quick)
    print(report)
    print(f"\nFull report written to {REPORT_PATH}")


if __name__ == "__main__":
    main()
