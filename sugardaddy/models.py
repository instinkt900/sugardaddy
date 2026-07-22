"""Typed rows shared across the DB, ingest, and web layers.

Timestamps are UTC epoch seconds (int) everywhere internally; display-time
conversion to the configured timezone happens only in the web layer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GlucoseReading:
    ts_utc: int
    value_mgdl: float
    trend: int | None = None
    source: str = "librelinkup"
    id: int | None = None


@dataclass
class InsulinDose:
    ts_utc: int
    units: float
    kind: str = "bolus"  # bolus | basal | correction
    note: str = ""
    id: int | None = None


@dataclass
class Meal:
    ts_utc: int
    carbs_g: float | None = None
    description: str = ""
    tags: str = ""
    note: str = ""
    id: int | None = None
