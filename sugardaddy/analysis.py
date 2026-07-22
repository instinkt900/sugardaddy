"""Retrospective analysis over the stored timeline.

Deliberately simple and explainable (no modelling here): time-in-range, average
glucose + estimated GMI, high/low counts, and the 2-hour glucose response after
each logged meal. This is the clean base a later predictive layer can build on.
"""

from __future__ import annotations

from dataclasses import dataclass

from sugardaddy.constants import mgdl_to_mmol, to_display
from sugardaddy.models import GlucoseReading, Meal

# A meal's "starting" glucose is the nearest reading within this window (seconds).
_MEAL_MATCH_WINDOW = 20 * 60
_POST_MEAL_WINDOW = 2 * 60 * 60


@dataclass
class Summary:
    reading_count: int
    avg_mgdl: float | None
    avg_display: float | None
    gmi_percent: float | None
    tir_percent: float | None      # % in range
    below_percent: float | None
    above_percent: float | None
    low_count: int
    high_count: int
    units: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def summarize(
    readings: list[GlucoseReading],
    target_low_mgdl: float,
    target_high_mgdl: float,
    units: str,
) -> Summary:
    n = len(readings)
    if n == 0:
        return Summary(0, None, None, None, None, None, None, 0, 0, units)

    values = [r.value_mgdl for r in readings]
    below = [v for v in values if v < target_low_mgdl]
    above = [v for v in values if v > target_high_mgdl]
    in_range = n - len(below) - len(above)
    avg = sum(values) / n
    # ADA/GMI formula (Bergenstal et al.): GMI(%) = 3.31 + 0.02392 * mean mg/dL.
    gmi = 3.31 + 0.02392 * avg

    return Summary(
        reading_count=n,
        avg_mgdl=round(avg, 1),
        avg_display=to_display(avg, units),
        gmi_percent=round(gmi, 1),
        tir_percent=round(100 * in_range / n, 1),
        below_percent=round(100 * len(below) / n, 1),
        above_percent=round(100 * len(above) / n, 1),
        low_count=len(below),
        high_count=len(above),
        units=units,
    )


def post_meal_responses(
    readings: list[GlucoseReading],
    meals: list[Meal],
    units: str,
) -> list[dict]:
    """For each meal, the glucose at meal time and the peak/end over the next 2h."""
    if not readings:
        return []
    ordered = sorted(readings, key=lambda r: r.ts_utc)
    out: list[dict] = []

    for meal in meals:
        start = _nearest(ordered, meal.ts_utc, _MEAL_MATCH_WINDOW)
        window = [r for r in ordered if meal.ts_utc <= r.ts_utc <= meal.ts_utc + _POST_MEAL_WINDOW]
        if start is None or not window:
            continue
        peak = max(window, key=lambda r: r.value_mgdl)
        end = window[-1]
        out.append(
            {
                "meal_id": meal.id,
                "ts_utc": meal.ts_utc,
                "description": meal.description,
                "carbs_g": meal.carbs_g,
                "start_display": to_display(start.value_mgdl, units),
                "peak_display": to_display(peak.value_mgdl, units),
                "peak_delta_display": _delta_display(peak.value_mgdl - start.value_mgdl, units),
                "end_display": to_display(end.value_mgdl, units),
                "minutes_to_peak": round((peak.ts_utc - meal.ts_utc) / 60),
            }
        )
    return out


def _nearest(ordered: list[GlucoseReading], ts: int, window: int) -> GlucoseReading | None:
    best = None
    best_gap = window + 1
    for r in ordered:
        gap = abs(r.ts_utc - ts)
        if gap <= window and gap < best_gap:
            best, best_gap = r, gap
        if r.ts_utc > ts + window:
            break
    return best


def _delta_display(delta_mgdl: float, units: str) -> float:
    if units == "mmol/L":
        return round(mgdl_to_mmol(delta_mgdl), 1)
    return round(delta_mgdl)
