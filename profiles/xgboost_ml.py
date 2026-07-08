from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class XGBoostMLProfileSettings:
    """Profile-level metadata for the legacy ML runtime."""

    name: str = "xgboost_ml"
    strategy: str = "xgboost_model_inference"
    interval: str = "15m"
    uses_model: bool = True

