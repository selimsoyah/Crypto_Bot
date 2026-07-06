"""
feature_sweep.py
==================
Phase 3 retrain experiment: compare feature-group variants on the COMPOUND 15m
label recipe (L1 control) without overwriting the production model.

Runs variants F0–F5, reporting per variant:
    * Feature count and group description
    * OOS log loss vs train-frequency naive baseline
    * Threshold optimizer outcome (min 5 val trades; long fallback 0.78, short 0.60)
    * Walk-forward mean net PnL, Sharpe, and fold win rate (5 folds)

Usage::

    python feature_sweep.py           # full sweep (walk-forward per variant)
    python feature_sweep.py --quick   # calibration only (~2 min)

Writes ``feature_sweep_report.md`` in the project root.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

import numpy as np
import pandas as pd
from sklearn.metrics import log_loss

import config
import data_pipeline
import model_brain
from backtest_runner import run_fold, walk_forward_splits
from feature_factory import (
    FEATURE_COLUMNS,
    FEATURE_VARIANTS,
    TARGET_COLUMN,
    build_feature_matrix,
    feature_columns_for,
    use_feature_variant,
)

REPORT_PATH: Final[str] = str(config.BASE_DIR / "feature_sweep_report.md")


@dataclass
class FeatureVariantResult:
    variant_id: str
    name: str
    n_features: int
    rows: int
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


def _calibration_metrics(
    matrix: pd.DataFrame,
    cols: list[str],
) -> tuple[float, float, bool, float, float, float, float]:
    train_df, test_df = model_brain.chronological_split(matrix)
    val_split = int(len(train_df) * 0.85)
    sub_train = train_df.iloc[:val_split].reset_index(drop=True)
    valid = train_df.iloc[val_split:].reset_index(drop=True)

    tuning_model = model_brain._build_model()
    model_brain._fit_balanced(tuning_model, sub_train[cols], sub_train[TARGET_COLUMN])
    proba_valid = tuning_model.predict_proba(valid[cols])
    long_thr, short_thr, long_vp, short_vp = model_brain.optimize_thresholds(
        valid, proba_valid
    )

    model = model_brain._build_model()
    model_brain._fit_balanced(model, train_df[cols], train_df[TARGET_COLUMN])
    y_test = test_df[TARGET_COLUMN].to_numpy()
    proba = model.predict_proba(test_df[cols])

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


def _walk_forward_summary(matrix: pd.DataFrame, cols: list[str]) -> tuple[float, float, int, int, float]:
    splits = walk_forward_splits(matrix)
    folds: list = []
    for i, (train, test) in enumerate(splits):
        for frame in (train, test):
            missing = [c for c in cols if c not in frame.columns]
            if missing:
                raise ValueError(f"Fold {i + 1} missing feature columns: {missing}")
        folds.append(run_fold(i + 1, train, test))
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


def evaluate_variant(variant_id: str, raw: pd.DataFrame, quick: bool) -> FeatureVariantResult:
    cols = feature_columns_for(variant_id)
    matrix = build_feature_matrix(raw, feature_variant=variant_id)

    with use_feature_variant(variant_id):
        (
            model_ll,
            base_ll,
            beats,
            long_thr,
            short_thr,
            long_vp,
            short_vp,
        ) = _calibration_metrics(matrix, cols)

        wf_mean = wf_std = wf_mean_sharpe = None
        wf_pos = wf_total = None
        if not quick:
            wf_mean, wf_std, wf_pos, wf_total, wf_mean_sharpe = _walk_forward_summary(
                matrix, cols
            )

    return FeatureVariantResult(
        variant_id=variant_id,
        name=FEATURE_VARIANTS[variant_id],
        n_features=len(cols),
        rows=len(matrix),
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


def write_report(results: list[FeatureVariantResult], quick: bool) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# COMPOUND 15m Feature Sweep Report (Phase 3)",
        "",
        f"**Generated:** {now}  ",
        f"**Profile:** {config.TRADING_PROFILE} · **Interval:** {config.INTERVAL}  ",
        f"**Labels:** L1 control (+0.4% / -0.25%, 16-bar horizon)  ",
        f"**Mode:** {'quick (calibration only)' if quick else 'full (walk-forward per variant)'}  ",
        "",
        "Compares Phase 3 feature groups before promoting a variant to production. "
        "Deploy gate: beat log-loss baseline **and** walk-forward mean PnL > -5% with ≥2/5 positive folds.",
        "",
        "## Summary",
        "",
        "| ID | Features | # | Log loss | Beats baseline? | Thr L/S | Val PnL L/S | WF mean PnL | WF + folds |",
        "| :--- | :--- | ---: | ---: | :--- | :--- | :--- | ---: | :--- |",
    ]

    for r in results:
        wf_pnl = "—" if r.wf_mean_pnl is None else f"{r.wf_mean_pnl:+.2f}%"
        wf_folds = (
            "—"
            if r.wf_positive_folds is None
            else f"{r.wf_positive_folds}/{r.wf_total_folds}"
        )
        lines.append(
            f"| **{r.variant_id}** | {r.name} | {r.n_features} | {r.model_log_loss:.4f} | "
            f"{'✅' if r.beats_baseline else '❌'} | "
            f"{_format_thr(r.tuned_long_thr)} / {_format_thr(r.tuned_short_thr)} | "
            f"{r.val_long_pnl:+.2f}% / {r.val_short_pnl:+.2f}% | {wf_pnl} | {wf_folds} |"
        )

    lines += ["", "## Variant notes", ""]
    for r in results:
        lines.append(f"### {r.variant_id} — {r.name}")
        lines.append("")
        lines.append(f"- Feature count: **{r.n_features}** · rows: **{r.rows:,}**")
        lines.append(
            f"- Calibration: model log loss **{r.model_log_loss:.4f}** vs baseline "
            f"**{r.baseline_log_loss:.4f}** (Δ {r.baseline_log_loss - r.model_log_loss:+.4f})"
        )
        if r.wf_mean_pnl is not None:
            lines.append(
                f"- Walk-forward: mean PnL **{r.wf_mean_pnl:+.2f}%** "
                f"(σ {r.wf_std_pnl:.2f}%), mean Sharpe **{r.wf_mean_sharpe:.2f}**, "
                f"positive folds **{r.wf_positive_folds}/{r.wf_total_folds}**"
            )
        lines.append("")

    winners = [
        r
        for r in results
        if r.beats_baseline
        and r.wf_mean_pnl is not None
        and r.wf_mean_pnl > -5.0
        and (r.wf_positive_folds or 0) >= 2
    ]
    if winners:
        pick = max(winners, key=lambda r: r.wf_mean_pnl or -999.0)
        lines.append(
            f"**Suggested next step:** Promote **{pick.variant_id}** — set "
            f"`FEATURE_VARIANT={pick.variant_id}` in `.env`, then `python model_brain.py`."
        )
    else:
        baseline_beaters = [r for r in results if r.beats_baseline]
        if baseline_beaters:
            pick = min(baseline_beaters, key=lambda r: r.model_log_loss)
            lines.append(
                f"**Partial signal:** **{pick.variant_id}** beats log-loss baseline "
                f"but fails walk-forward deploy gates — try combined tuning or Phase 4."
            )
        else:
            lines.append(
                "**Suggested next step:** No variant beats the log-loss baseline — "
                "proceed to Phase 4 (model/training experiments)."
            )
    lines.append("")

    text = "\n".join(lines)
    with open(REPORT_PATH, "w") as fh:
        fh.write(text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="COMPOUND 15m Phase 3 feature sweep")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Skip walk-forward folds (calibration only)",
    )
    args = parser.parse_args()

    # Ensure production column list restored after sweep.
    previous = list(FEATURE_COLUMNS)
    try:
        raw = data_pipeline.load_historical_data()
        results = [
            evaluate_variant(vid, raw, quick=args.quick) for vid in FEATURE_VARIANTS
        ]
        report = write_report(results, quick=args.quick)
        print(report)
        print(f"\nFull report written to {REPORT_PATH}")
    finally:
        FEATURE_COLUMNS.clear()
        FEATURE_COLUMNS.extend(previous)


if __name__ == "__main__":
    main()
