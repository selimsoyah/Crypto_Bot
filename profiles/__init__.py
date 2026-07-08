from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .darvas_box import DarvasBoxProfileSettings
from .xgboost_ml import XGBoostMLProfileSettings

PROFILE_XGBOOST_ML = "xgboost_ml"
PROFILE_DARVAS_BOX = "darvas_box"
SUPPORTED_PROFILES = (PROFILE_XGBOOST_ML, PROFILE_DARVAS_BOX)


def normalize_profile_name(raw: str) -> str:
    """Normalize user-provided profile labels to canonical names."""
    value = (raw or "").strip().lower()
    aliases = {
        "xgboost": PROFILE_XGBOOST_ML,
        "ml": PROFILE_XGBOOST_ML,
        "xgboost_ml": PROFILE_XGBOOST_ML,
        "darvas": PROFILE_DARVAS_BOX,
        "box": PROFILE_DARVAS_BOX,
        "darvas_box": PROFILE_DARVAS_BOX,
    }
    return aliases.get(value, value)


def build_profile_catalog() -> dict[str, dict[str, Any]]:
    """Return serializable per-profile settings payloads."""
    return {
        PROFILE_XGBOOST_ML: asdict(XGBoostMLProfileSettings()),
        PROFILE_DARVAS_BOX: asdict(DarvasBoxProfileSettings()),
    }

