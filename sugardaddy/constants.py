"""Shared constants and small unit helpers.

Glucose is stored canonically in **mg/dL** (the unit the LibreLinkUp API reports
natively) and converted to the display unit at the edges. Australia uses mmol/L.
"""

from __future__ import annotations

DB_NAME = "sugardaddy.db"

# 1 mmol/L of glucose == 18.0182 mg/dL.
MGDL_PER_MMOL = 18.0182

# Sensible default target range (mmol/L) — the standard "time in range" band.
DEFAULT_TARGET_LOW_MMOL = 3.9
DEFAULT_TARGET_HIGH_MMOL = 10.0

# Insulin dose kinds we accept.
INSULIN_KINDS = ("bolus", "basal", "correction")

# LibreLinkUp reports a trend as an integer 1..5; map to a human arrow.
TREND_ARROWS = {
    1: "↓↓",  # falling quickly
    2: "↓",         # falling
    3: "→",         # steady
    4: "↑",         # rising
    5: "↑↑",  # rising quickly
}


def mgdl_to_mmol(mgdl: float) -> float:
    return mgdl / MGDL_PER_MMOL


def mmol_to_mgdl(mmol: float) -> float:
    return mmol * MGDL_PER_MMOL


def to_display(mgdl: float, units: str) -> float:
    """Convert a stored mg/dL value to the configured display unit, rounded the
    way each unit is conventionally shown (mmol/L to 1 dp, mg/dL to integer)."""
    if units == "mmol/L":
        return round(mgdl_to_mmol(mgdl), 1)
    return round(mgdl)


def trend_arrow(trend: int | None) -> str:
    if trend is None:
        return ""
    return TREND_ARROWS.get(int(trend), "")
