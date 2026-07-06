"""
audit_predictions.py
======================
Historical prediction audit for the F2 (1h multi-timeframe) model on the COMPOUND
15m scalper profile.

Loads ``historical_btc_15m.parquet`` only, trains or loads an F2 brain on the
chronological train slice (no holdout leakage), then scores the holdout segment
against objective-aligned asymmetric scalp labels:

    * LONG  — +TP% reached before −SL% within ``FORWARD_WINDOW`` bars
    * SHORT — −TP% reached before +SL% within ``FORWARD_WINDOW`` bars
    * CASH  — otherwise

Prints a Markdown audit table of the top high-confidence directional signals.

Usage::

    python audit_predictions.py
    python audit_predictions.py --retrain
    python audit_predictions.py --examples 15
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import timezone
from typing import Final

import numpy as np
import pandas as pd

import config
import model_brain
from feature_factory import (
    TARGET_COLUMN,
    add_technical_indicators,
    feature_columns_for,
    use_feature_variant,
)

# --------------------------------------------------------------------------- #
# COMPOUND scalper audit brackets (asymmetric — matches live scalp objectives)  #
# --------------------------------------------------------------------------- #
AUDIT_PARQUET: Final[str] = str(config.BASE_DIR / "historical_btc_15m.parquet")
F2_MODEL_PATH: Final[str] = str(config.BASE_DIR / "xgboost_trading_model_f2.json")
AUDIT_FORWARD_WINDOW: Final[int] = config.FORWARD_WINDOW
AUDIT_LONG_TP_PCT: Final[float] = config.TAKE_PROFIT_PCT
AUDIT_LONG_SL_PCT: Final[float] = config.STOP_LOSS_PCT
AUDIT_SHORT_TP_PCT: Final[float] = config.TAKE_PROFIT_PCT
AUDIT_SHORT_SL_PCT: Final[float] = config.STOP_LOSS_PCT
FEATURE_VARIANT: Final[str] = "F2"
MIN_EXAMPLE_GAP_BARS: Final[int] = 96  # ~24h between showcased signals


@dataclass(frozen=True)
class AuditRow:
    timestamp: str
    ground_truth: str
    predicted: str
    confidence_pct: float
    correct: bool


def load_15m_dataset() -> pd.DataFrame:
    """Load the 15-minute BTC history used by the COMPOUND profile."""
    if not os.path.exists(AUDIT_PARQUET):
        raise FileNotFoundError(
            f"15m dataset not found at {AUDIT_PARQUET}. "
            "Run `python data_pipeline.py` with TRADING_PROFILE=COMPOUND."
        )
    frame = pd.read_parquet(AUDIT_PARQUET)
    required = {"Timestamp", "Open", "High", "Low", "Close", "Volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Parquet missing columns: {sorted(missing)}")
    frame = frame.sort_values("Timestamp").reset_index(drop=True)
    return frame


def build_audit_ground_truth(df: pd.DataFrame) -> pd.Series:
    """Asymmetric triple-barrier labels for COMPOUND scalp verification.

    Uses project label codes: 0=CASH, 1=SHORT, 2=LONG.
    """
    close = df["Close"].to_numpy(dtype=np.float64)
    high = df["High"].to_numpy(dtype=np.float64)
    low = df["Low"].to_numpy(dtype=np.float64)
    n = len(close)
    window = AUDIT_FORWARD_WINDOW
    target = np.full(n, np.nan, dtype=np.float64)

    for i in range(n - window):
        entry = close[i]
        long_tp = entry * (1.0 + AUDIT_LONG_TP_PCT)
        long_sl = entry * (1.0 - AUDIT_LONG_SL_PCT)
        short_tp = entry * (1.0 - AUDIT_SHORT_TP_PCT)
        short_sl = entry * (1.0 + AUDIT_SHORT_SL_PCT)

        label = float(config.LABEL_CASH)
        long_dead = False
        short_dead = False

        for j in range(i + 1, i + 1 + window):
            hi = high[j]
            lo = low[j]

            if lo <= long_sl:
                long_dead = True
            if hi >= short_sl:
                short_dead = True

            if not long_dead and hi >= long_tp:
                label = float(config.LABEL_LONG)
                break
            if not short_dead and lo <= short_tp:
                label = float(config.LABEL_SHORT)
                break
            if long_dead and short_dead:
                break

        target[i] = label

    return pd.Series(target, index=df.index, name="audit_target")


def _chronological_holdout(
    frame: pd.DataFrame,
    train_fraction: float = model_brain.TRAIN_FRACTION,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    split_idx = int(len(frame) * train_fraction)
    train = frame.iloc[:split_idx].reset_index(drop=True)
    holdout = frame.iloc[split_idx:].reset_index(drop=True)
    return train, holdout


def _train_f2_model(train_matrix: pd.DataFrame):
    cols = feature_columns_for(FEATURE_VARIANT)
    model = model_brain._build_model()
    model_brain._fit_balanced(model, train_matrix[cols], train_matrix[TARGET_COLUMN])
    return model


def _load_or_train_f2(train_matrix: pd.DataFrame, retrain: bool):
    from xgboost import XGBClassifier

    cols = feature_columns_for(FEATURE_VARIANT)
    if not retrain and os.path.exists(F2_MODEL_PATH):
        model = XGBClassifier()
        model.load_model(F2_MODEL_PATH)
        expected = len(getattr(model, "feature_names_in_", cols))
        if expected != len(cols):
            raise ValueError(
                f"F2 artifact expects {expected} features but F2 variant has {len(cols)}. "
                "Re-run with --retrain."
            )
        return model

    with use_feature_variant(FEATURE_VARIANT):
        model = _train_f2_model(train_matrix)
    model.save_model(F2_MODEL_PATH)
    return model


def _predict_frame(model, matrix: pd.DataFrame) -> pd.DataFrame:
    cols = feature_columns_for(FEATURE_VARIANT)
    proba = model.predict_proba(matrix[cols])
    out = matrix.copy()
    out["prob_cash"] = proba[:, config.LABEL_CASH]
    out["prob_short"] = proba[:, config.LABEL_SHORT]
    out["prob_long"] = proba[:, config.LABEL_LONG]
    pred = np.argmax(proba, axis=1)
    out["predicted"] = pred
    out["signal_conf"] = np.select(
        [
            pred == config.LABEL_LONG,
            pred == config.LABEL_SHORT,
        ],
        [out["prob_long"], out["prob_short"]],
        default=0.0,
    )
    return out


def _label_name(code: int) -> str:
    return config.DIRECTION_NAMES.get(int(code), str(code))


def _format_ts(value) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    else:
        ts = ts.tz_convert(timezone.utc)
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def _select_distinct_examples(scored: pd.DataFrame, n: int) -> pd.DataFrame:
    """Pick top-N high-confidence directional signals spaced apart in time."""
    candidates = scored[scored["predicted"] != config.LABEL_CASH].copy()
    candidates = candidates.sort_values("signal_conf", ascending=False)
    if candidates.empty:
        return candidates

    chosen_idx: list[int] = []
    for idx, row in candidates.iterrows():
        if len(chosen_idx) >= n:
            break
        if not chosen_idx:
            chosen_idx.append(idx)
            continue
        last_pos = scored.index.get_loc(chosen_idx[-1])
        cur_pos = scored.index.get_loc(idx)
        if abs(cur_pos - last_pos) >= MIN_EXAMPLE_GAP_BARS:
            chosen_idx.append(idx)
    return scored.loc[chosen_idx]


def _build_audit_rows(examples: pd.DataFrame) -> list[AuditRow]:
    rows: list[AuditRow] = []
    for _, row in examples.iterrows():
        truth = int(row["audit_target"])
        pred = int(row["predicted"])
        rows.append(
            AuditRow(
                timestamp=_format_ts(row["Timestamp"]),
                ground_truth=_label_name(truth),
                predicted=_label_name(pred),
                confidence_pct=100.0 * float(row["signal_conf"]),
                correct=(truth == pred),
            )
        )
    return rows


def _print_markdown_table(rows: list[AuditRow]) -> None:
    print()
    print("| Timestamp (15m) | Ground Truth Outcome | Bot Predicted Direction | Model Confidence % | Prediction Correct? |")
    print("| :--- | :--- | :--- | ---: | :---: |")
    for row in rows:
        mark = "✅" if row.correct else "❌"
        print(
            f"| {row.timestamp} | {row.ground_truth} | {row.predicted} | "
            f"{row.confidence_pct:.1f}% | {mark} |"
        )
    print()


def _holdout_summary(scored: pd.DataFrame) -> dict[str, float | int | str]:
    directional = scored[scored["predicted"] != config.LABEL_CASH]
    n_dir = len(directional)
    accuracy = (
        float((directional["predicted"] == directional["audit_target"]).mean())
        if n_dir
        else 0.0
    )
    return {
        "holdout_rows": len(scored),
        "directional_signals": n_dir,
        "directional_accuracy": accuracy,
        "holdout_start": _format_ts(scored["Timestamp"].iloc[0]),
        "holdout_end": _format_ts(scored["Timestamp"].iloc[-1]),
    }


def run_audit(*, retrain: bool, n_examples: int) -> None:
    print("=" * 72)
    print("  F2 COMPOUND 15m — Historical Prediction Audit")
    print("=" * 72)

    raw = load_15m_dataset()
    train_raw, holdout_raw = _chronological_holdout(raw)

    print(f"\n**Dataset:** `{AUDIT_PARQUET}` ({len(raw):,} rows)")
    print(f"**Train / holdout split:** {len(train_raw):,} / {len(holdout_raw):,} "
          f"({model_brain.TRAIN_FRACTION:.0%} / {1 - model_brain.TRAIN_FRACTION:.0%})")
    print(
        f"**Audit brackets:** LONG +{AUDIT_LONG_TP_PCT:.2%}/−{AUDIT_LONG_SL_PCT:.2%} · "
        f"SHORT −{AUDIT_SHORT_TP_PCT:.2%}/+{AUDIT_SHORT_SL_PCT:.2%} · "
        f"{AUDIT_FORWARD_WINDOW}-bar horizon"
    )
    print(f"**Feature variant:** {FEATURE_VARIANT} ({len(feature_columns_for(FEATURE_VARIANT))} features)")

    from feature_factory import build_target

    train_enriched = add_technical_indicators(train_raw, feature_variant=FEATURE_VARIANT)
    train_enriched[TARGET_COLUMN] = build_target(train_enriched)
    cols = feature_columns_for(FEATURE_VARIANT)
    train_matrix = (
        train_enriched[["Timestamp", TARGET_COLUMN] + cols]
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
        .reset_index(drop=True)
    )
    train_matrix[TARGET_COLUMN] = train_matrix[TARGET_COLUMN].astype(int)

    holdout_enriched = add_technical_indicators(holdout_raw, feature_variant=FEATURE_VARIANT)
    holdout_enriched["audit_target"] = build_audit_ground_truth(holdout_enriched)
    holdout_matrix = (
        holdout_enriched[["Timestamp", "audit_target"] + cols]
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
        .reset_index(drop=True)
    )
    holdout_matrix["audit_target"] = holdout_matrix["audit_target"].astype(int)

    with use_feature_variant(FEATURE_VARIANT):
        model = _load_or_train_f2(train_matrix, retrain=retrain)

    scored = _predict_frame(model, holdout_matrix)
    summary = _holdout_summary(scored)

    print(f"\n**Holdout window:** {summary['holdout_start']} → {summary['holdout_end']}")
    print(f"**Scored rows:** {summary['holdout_rows']:,}")
    print(f"**Directional signals (non-CASH argmax):** {summary['directional_signals']:,}")
    print(f"**Directional accuracy vs audit labels:** {summary['directional_accuracy']:.1%}")

    examples = _select_distinct_examples(scored, n_examples)
    if examples.empty:
        print("\nNo directional high-confidence signals found on holdout.")
        return

    audit_rows = _build_audit_rows(examples)
    print(f"\n### Top {len(audit_rows)} distinct high-confidence examples "
          f"(≥{MIN_EXAMPLE_GAP_BARS} bars apart)")
    _print_markdown_table(audit_rows)

    correct = sum(1 for r in audit_rows if r.correct)
    print(f"Spot-check accuracy on showcased rows: {correct}/{len(audit_rows)} "
          f"({100.0 * correct / len(audit_rows):.0f}%)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit F2 model predictions on COMPOUND 15m holdout history",
    )
    parser.add_argument(
        "--retrain",
        action="store_true",
        help="Force retrain F2 model on train slice (saves to xgboost_trading_model_f2.json)",
    )
    parser.add_argument(
        "--examples",
        type=int,
        default=10,
        help="Number of distinct high-confidence examples to print (default: 10)",
    )
    args = parser.parse_args()
    run_audit(retrain=args.retrain, n_examples=max(1, args.examples))


if __name__ == "__main__":
    main()
