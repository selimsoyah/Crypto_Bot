"""
backtest_runner.py
====================
Phase 1 reproducible evaluation pipeline.

Generates ``backtest_report.md`` with:
    * Walk-forward validation (expanding window, >=5 folds)
    * Per-fold classification metrics + confusion matrix
    * Cost-aware backtest (taker fee + slippage)
    * Sharpe / Sortino / max drawdown / win rate by direction
    * EMA200 trend-filter ON vs OFF comparison
    * Feature importance + optional SHAP summary

Usage::

    python backtest_runner.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.metrics import classification_report, confusion_matrix

import config
import data_pipeline
import model_brain
from feature_factory import FEATURE_COLUMNS, TARGET_COLUMN, build_feature_matrix

REPORT_PATH = config.BACKTEST_REPORT_PATH


@dataclass
class FoldResult:
    fold: int
    train_rows: int
    test_rows: int
    test_start: str
    test_end: str
    long_thr: float
    short_thr: float
    accuracy: float
    long_precision: float
    short_precision: float
    classification_text: str
    confusion: np.ndarray
    backtest: model_brain.BacktestResult


def walk_forward_splits(
    matrix: pd.DataFrame,
    n_folds: int = config.WALK_FORWARD_FOLDS,
    min_train_frac: float = config.WALK_FORWARD_MIN_TRAIN_FRAC,
) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """Expanding-window walk-forward: each fold adds more training data."""
    n = len(matrix)
    min_train = int(n * min_train_frac)
    oos_total = n - min_train
    if oos_total < n_folds:
        raise ValueError(
            f"Not enough OOS rows ({oos_total}) for {n_folds} folds. "
            "Reduce WALK_FORWARD_FOLDS or WALK_FORWARD_MIN_TRAIN_FRAC."
        )
    fold_size = oos_total // n_folds
    splits: list[tuple[pd.DataFrame, pd.DataFrame]] = []
    for k in range(n_folds):
        test_start = min_train + k * fold_size
        test_end = min_train + (k + 1) * fold_size if k < n_folds - 1 else n
        train = matrix.iloc[:test_start].reset_index(drop=True)
        test = matrix.iloc[test_start:test_end].reset_index(drop=True)
        splits.append((train, test))
    return splits


def _ts_range(df: pd.DataFrame) -> tuple[str, str]:
    if "Timestamp" not in df.columns or df.empty:
        return ("n/a", "n/a")
    return (str(df["Timestamp"].iloc[0]), str(df["Timestamp"].iloc[-1]))


def _predict_labels(
    test: pd.DataFrame,
    proba: np.ndarray,
    long_thr: float,
    short_thr: float,
    use_trend_filter: bool | None,
) -> np.ndarray:
    n = len(test)
    trend = model_brain._trend_array(test, n)
    p_long = proba[:, config.LABEL_LONG]
    p_short = proba[:, config.LABEL_SHORT]
    preds = np.full(n, config.LABEL_CASH, dtype=int)
    for i in range(n):
        d = model_brain.decide_direction(
            float(p_long[i]), float(p_short[i]), float(trend[i]),
            long_thr, short_thr, use_trend_filter=use_trend_filter,
        )
        if d == "LONG":
            preds[i] = config.LABEL_LONG
        elif d == "SHORT":
            preds[i] = config.LABEL_SHORT
    return preds


def run_fold(
    fold_idx: int,
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
    use_trend_filter: bool | None = None,
) -> FoldResult:
    """Train, tune thresholds, and evaluate one walk-forward fold."""
    val_split = int(len(train_df) * 0.85)
    sub_train = train_df.iloc[:val_split].reset_index(drop=True)
    valid = train_df.iloc[val_split:].reset_index(drop=True)

    tuning_model = model_brain._build_model()
    model_brain._fit_balanced(
        tuning_model, sub_train[FEATURE_COLUMNS], sub_train[TARGET_COLUMN]
    )
    proba_valid = tuning_model.predict_proba(valid[FEATURE_COLUMNS])
    long_thr, short_thr, _, _ = model_brain.optimize_thresholds(valid, proba_valid)

    model = model_brain._build_model()
    model_brain._fit_balanced(model, train_df[FEATURE_COLUMNS], train_df[TARGET_COLUMN])

    x_test = test_df[FEATURE_COLUMNS]
    y_test = test_df[TARGET_COLUMN].to_numpy()
    proba_test = model.predict_proba(x_test)
    pred_test = np.argmax(proba_test, axis=1)

    preds_signal = _predict_labels(test_df, proba_test, long_thr, short_thr, use_trend_filter)
    report = classification_report(
        y_test,
        preds_signal,
        labels=[config.LABEL_CASH, config.LABEL_SHORT, config.LABEL_LONG],
        target_names=["CASH", "SHORT", "LONG"],
        digits=3,
        zero_division=0,
    )
    cm = confusion_matrix(
        y_test,
        preds_signal,
        labels=[config.LABEL_CASH, config.LABEL_SHORT, config.LABEL_LONG],
    )

    long_prec, short_prec = model_brain._directional_precision(
        test_df, proba_test, y_test, long_thr, short_thr, use_trend_filter
    )
    from sklearn.metrics import accuracy_score

    test_bt = test_df.copy()
    if "High" not in test_bt.columns:
        test_bt["High"] = test_bt["Close"]
    if "Low" not in test_bt.columns:
        test_bt["Low"] = test_bt["Close"]

    bt: model_brain.BacktestResult = model_brain.backtest_directional(
        test_bt,
        proba_test,
        long_threshold=long_thr,
        short_threshold=short_thr,
        use_trend_filter=use_trend_filter,
        rich=True,
    )

    t0, t1 = _ts_range(test_df)
    return FoldResult(
        fold=fold_idx,
        train_rows=len(train_df),
        test_rows=len(test_df),
        test_start=t0,
        test_end=t1,
        long_thr=long_thr,
        short_thr=short_thr,
        accuracy=float(accuracy_score(y_test, pred_test)),
        long_precision=long_prec,
        short_precision=short_prec,
        classification_text=report,
        confusion=cm,
        backtest=bt,
    )


def _feature_importance_section(model, x_sample: pd.DataFrame) -> list[str]:
    lines = ["## Feature Importance (XGBoost gain)", ""]
    imp = model.feature_importances_
    order = np.argsort(imp)[::-1]
    lines.append("| Rank | Feature | Importance |")
    lines.append("| ---: | :--- | ---: |")
    for rank, idx in enumerate(order[:15], start=1):
        lines.append(f"| {rank} | {FEATURE_COLUMNS[idx]} | {imp[idx]:.4f} |")
    lines.append("")

    try:
        import shap

        explainer = shap.TreeExplainer(model)
        sample = x_sample.sample(n=min(500, len(x_sample)), random_state=42)
        shap_values = explainer.shap_values(sample)
        arr = np.asarray(shap_values)
        if arr.ndim == 3:
            # (n_samples, n_features, n_classes)
            mean_abs = np.mean(np.abs(arr), axis=(0, 2))
        elif isinstance(shap_values, list):
            stacked = np.stack([np.abs(sv).mean(axis=0) for sv in shap_values], axis=0)
            mean_abs = stacked.mean(axis=0)
        else:
            mean_abs = np.abs(arr).mean(axis=0)
        order_s = np.argsort(mean_abs)[::-1]
        lines.extend(["## SHAP Mean |Contribution| (top 15)", ""])
        lines.append("| Rank | Feature | Mean |SHAP| |")
        lines.append("| ---: | :--- | ---: |")
        for rank, idx in enumerate(order_s[:15], start=1):
            lines.append(f"| {rank} | {FEATURE_COLUMNS[idx]} | {mean_abs[idx]:.6f} |")
        lines.append("")
    except Exception as exc:
        lines.extend([
            "> SHAP unavailable (`pip install shap` for full attribution). "
            f"Reason: {exc}",
            "",
        ])
    return lines


def _fold_table_row(fr: FoldResult) -> str:
    bt = fr.backtest
    return (
        f"| {fr.fold} | {fr.train_rows:,} | {fr.test_rows:,} | "
        f"{fr.long_thr:.3f} / {fr.short_thr:.3f} | "
        f"{fr.accuracy:.3f} | {fr.long_precision:.3f} | {fr.short_precision:.3f} | "
        f"{bt.strategy_net_profit_pct:+.2f}% | {bt.buy_hold_net_profit_pct:+.2f}% | "
        f"{bt.max_drawdown_pct:.2f}% | {bt.sharpe:.2f} | {bt.sortino:.2f} | "
        f"{bt.overall_win_rate:.1f}% | {bt.n_long} / {bt.n_short} |"
    )


def _trend_comparison(
    train_df: pd.DataFrame,
    test_df: pd.DataFrame,
) -> list[str]:
    """Run trend-filter ON vs OFF on the final OOS window."""
    lines = [
        "## EMA200 Trend Filter — A/B Comparison (final fold test window)",
        "",
        "Same model/thresholds; only the regime gate changes.",
        "",
        "| Setting | Net PnL | Max DD | Sharpe | Sortino | Win% | LONG win% | SHORT win% | Trades |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, trend_on in [("Filter OFF", False), ("Filter ON", True)]:
        fr = run_fold(0, train_df, test_df, use_trend_filter=trend_on)
        bt = fr.backtest
        lines.append(
            f"| **{label}** | {bt.strategy_net_profit_pct:+.2f}% | "
            f"{bt.max_drawdown_pct:.2f}% | {bt.sharpe:.2f} | {bt.sortino:.2f} | "
            f"{bt.overall_win_rate:.1f}% | {bt.long_win_rate:.1f}% | "
            f"{bt.short_win_rate:.1f}% | {bt.n_trades} |"
        )
    lines.extend([
        "",
        "**Recommendation:** Compare the row with higher risk-adjusted return "
        "(Sharpe) and lower max drawdown. Do not enable live without reviewing "
        "all walk-forward folds above — one lucky window can mislead.",
        "",
    ])
    return lines


def generate_report(save_path: str = REPORT_PATH) -> str:
    raw = data_pipeline.load_historical_data()
    matrix = build_feature_matrix(raw)
    splits = walk_forward_splits(matrix)

    cost_pct = model_brain._round_trip_cost_pct() * 100.0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# BTC/USDT ML Futures Bot — Walk-Forward Backtest Report",
        "",
        f"**Generated:** {now}  ",
        f"**Data rows:** {len(matrix):,} ({config.HISTORY_YEARS}y × {config.INTERVAL})  ",
        f"**TP / SL:** +{config.TAKE_PROFIT_PCT*100:.1f}% / -{config.STOP_LOSS_PCT*100:.1f}%  ",
        f"**Costs modelled:** taker {config.BACKTEST_TAKER_FEE*100:.2f}%/side + "
        f"slippage {config.BACKTEST_SLIPPAGE_BPS:.1f} bps/side "
        f"→ **{cost_pct:.3f}% round-trip per trade**  ",
        f"**Walk-forward:** {config.WALK_FORWARD_FOLDS} expanding folds, "
        f"min train {config.WALK_FORWARD_MIN_TRAIN_FRAC:.0%}",
        "",
        "---",
        "",
        "## Per-Fold Summary (cost-adjusted backtest)",
        "",
        "| Fold | Train | Test | Thr L/S | Acc | L-Prec | S-Prec | "
        "Strat PnL | B&H | MaxDD | Sharpe | Sortino | Win% | L/S trades |",
        "| ---: | ---: | ---: | :--- | ---: | ---: | ---: | "
        "---: | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]

    fold_results: list[FoldResult] = []
    for i, (train_df, test_df) in enumerate(splits, start=1):
        fr = run_fold(i, train_df, test_df, use_trend_filter=False)
        fold_results.append(fr)
        lines.append(_fold_table_row(fr))

    # Aggregate stats across folds
    net_pnls = [fr.backtest.strategy_net_profit_pct for fr in fold_results]
    sharpes = [fr.backtest.sharpe for fr in fold_results]
    lines.extend([
        "",
        f"**Cross-fold net PnL:** mean {np.mean(net_pnls):+.2f}%, "
        f"std {np.std(net_pnls):.2f}%, min {np.min(net_pnls):+.2f}%, "
        f"max {np.max(net_pnls):+.2f}%  ",
        f"**Cross-fold Sharpe:** mean {np.mean(sharpes):.2f}, "
        f"std {np.std(sharpes):.2f}",
        "",
        "---",
        "",
    ])

    # Per-fold detail sections
    for fr in fold_results:
        lines.extend([
            f"### Fold {fr.fold} detail ({fr.test_start} → {fr.test_end})",
            "",
            f"- Train rows: {fr.train_rows:,} · Test rows: {fr.test_rows:,}",
            f"- Thresholds: LONG {fr.long_thr:.3f} · SHORT {fr.short_thr:.3f}",
            f"- Argmax accuracy: {fr.accuracy:.3f}",
            "",
            "**Classification report (signal-based predictions vs labels):**",
            "",
            "```",
            fr.classification_text.rstrip(),
            "```",
            "",
            "**Confusion matrix (rows=true, cols=predicted):**",
            "",
            "| | CASH | SHORT | LONG |",
            "| :--- | ---: | ---: | ---: |",
        ])
        for label, row in zip(["CASH", "SHORT", "LONG"], fr.confusion):
            lines.append(f"| **{label}** | {row[0]} | {row[1]} | {row[2]} |")
        bt = fr.backtest
        lines.extend([
            "",
            f"- Net strategy PnL: **{bt.strategy_net_profit_pct:+.2f}%** "
            f"(cost drag {bt.total_cost_pct:.2f}% across {bt.n_trades} trades)",
            f"- LONG win rate: {bt.long_win_rate:.1f}% · SHORT win rate: {bt.short_win_rate:.1f}%",
            "",
            "---",
            "",
        ])

    # Trend filter A/B on last fold
    last_train, last_test = splits[-1]
    lines.extend(_trend_comparison(last_train, last_test))

    # Feature importance on full-data model (last fold train+test context)
    final_model = model_brain._build_model()
    model_brain._fit_balanced(
        final_model, last_train[FEATURE_COLUMNS], last_train[TARGET_COLUMN]
    )
    lines.extend(_feature_importance_section(final_model, last_train[FEATURE_COLUMNS]))
    lines.extend([
        "---",
        "",
        "*Report generated by `backtest_runner.py` — Phase 1 model rigor audit.*",
        "",
    ])

    markdown = "\n".join(lines)
    with open(save_path, "w", encoding="utf-8") as fh:
        fh.write(markdown)
    return markdown


if __name__ == "__main__":
    text = generate_report()
    print(text[:4000])
    if len(text) > 4000:
        print(f"\n... [{len(text) - 4000} more chars] ...")
    print(f"\nFull report written to {REPORT_PATH}")
