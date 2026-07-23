"""Typed rows shared across the DB, ingest, and web layers.

Timestamps are UTC epoch seconds (int) everywhere internally; display-time
conversion to the configured timezone happens only in the web layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
class Food:
    """A reusable item in the food library. Values are per single unit/serving.
    Editing a food never changes history — logging copies values into a
    MealItem snapshot."""

    name: str
    description: str = ""
    carbs_g: float | None = None
    calories: float | None = None
    tags: str = ""
    id: int | None = None


@dataclass
class MealItem:
    """One line on a logged meal's plate: a snapshot of a food plus a count.
    ``food_id`` is kept only as soft provenance — it is never used for display
    or analysis, so deleting/editing the source food leaves history intact."""

    name: str
    count: float = 1
    carbs_g: float | None = None
    calories: float | None = None
    description: str = ""
    tags: str = ""
    food_id: int | None = None
    id: int | None = None


@dataclass
class Meal:
    """A logged meal: a header on the timeline plus a plate of snapshot items."""

    ts_utc: int
    name: str = ""
    note: str = ""
    items: list[MealItem] = field(default_factory=list)
    id: int | None = None

    @property
    def total_carbs(self) -> float | None:
        vals = [i.carbs_g * i.count for i in self.items if i.carbs_g is not None]
        return round(sum(vals), 1) if vals else None

    @property
    def total_calories(self) -> float | None:
        vals = [i.calories * i.count for i in self.items if i.calories is not None]
        return round(sum(vals)) if vals else None

    @property
    def label(self) -> str:
        """Human summary: the explicit name if set, else the item list."""
        if self.name.strip():
            return self.name.strip()
        if self.items:
            return ", ".join(f"{i.count:g}× {i.name}" for i in self.items)
        return "(meal)"


@dataclass
class MealTemplateItem:
    """One line of a saved meal. Prefer the live food (by ``food_id``) at load
    time; the stored name/carbs/calories are a fallback for ad-hoc items or
    foods that were later deleted."""

    name: str
    count: float = 1
    carbs_g: float | None = None
    calories: float | None = None
    food_id: int | None = None
    id: int | None = None


@dataclass
class MealTemplate:
    """A reusable named plate for fast logging (Update / Save as new). Not linked
    to history — logging copies resolved values into Meal snapshots."""

    name: str
    items: list[MealTemplateItem] = field(default_factory=list)
    id: int | None = None
