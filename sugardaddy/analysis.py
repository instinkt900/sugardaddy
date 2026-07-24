"""Retrospective analysis over the stored timeline.

Deliberately simple and explainable (no modelling here): time-in-range, average
glucose + estimated GMI, high/low counts, and the 2-hour glucose response after
each logged meal. This is the clean base a later predictive layer can build on.

Everything here is a pure function of the rows passed in — no I/O, no clock, no
config lookups — so it is trivially testable and equally usable by the web layer
and the `report` command. Functions that need calendar/clock context (per-day,
per-hour) take an explicit ``tzinfo`` rather than reading a global.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

from sugardaddy.constants import mgdl_to_mmol, to_display
from sugardaddy.models import GlucoseReading, InsulinDose, Meal

# A meal's "starting" glucose is the nearest reading within this window (seconds).
_MEAL_MATCH_WINDOW = 20 * 60
_POST_MEAL_WINDOW = 2 * 60 * 60

# Two sub-range readings are part of the same episode if no more than this many
# seconds apart — bridges the odd dropped CGM sample without merging separate dips.
_EPISODE_GAP = 20 * 60


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
                "description": meal.label,
                "carbs_g": meal.total_carbs,
                "start_display": to_display(start.value_mgdl, units),
                "peak_display": to_display(peak.value_mgdl, units),
                "peak_delta_display": _delta_display(peak.value_mgdl - start.value_mgdl, units),
                "end_display": to_display(end.value_mgdl, units),
                "minutes_to_peak": round((peak.ts_utc - meal.ts_utc) / 60),
            }
        )
    out.sort(key=lambda d: d["ts_utc"], reverse=True)  # most recent meal first
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


# --------------------------------------------------------------------------
# Extra retrospective views used by the `report` command. All pure, all
# JSON-serialisable outputs (display values pre-converted to the chosen unit).
# --------------------------------------------------------------------------


def variability(readings: list[GlucoseReading], units: str) -> dict:
    """Spread of the readings: mean, (population) SD, and coefficient of
    variation. CV = SD / mean and is unit-independent; <=36% is the common
    "stable" threshold."""
    n = len(readings)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "cv_percent": None, "units": units}
    vals = [r.value_mgdl for r in readings]
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals) if n > 1 else 0.0
    return {
        "n": n,
        "mean": to_display(mean, units),
        "sd": _delta_display(sd, units),
        "cv_percent": round(100 * sd / mean, 1) if mean else None,
        "units": units,
    }


def _bucket_stats(
    readings: list[GlucoseReading],
    target_low_mgdl: float,
    target_high_mgdl: float,
    units: str,
) -> dict:
    """Shared per-group rollup (used by day and hour breakdowns)."""
    n = len(readings)
    vals = [r.value_mgdl for r in readings]
    below = sum(1 for v in vals if v < target_low_mgdl)
    above = sum(1 for v in vals if v > target_high_mgdl)
    in_range = n - below - above
    mean = sum(vals) / n
    return {
        "n": n,
        "avg": to_display(mean, units),
        "min": to_display(min(vals), units),
        "max": to_display(max(vals), units),
        "tir_percent": round(100 * in_range / n, 1),
        "below_percent": round(100 * below / n, 1),
        "above_percent": round(100 * above / n, 1),
        "cv_percent": round(100 * statistics.pstdev(vals) / mean, 1) if n > 1 and mean else 0.0,
    }


def daily_breakdown(
    readings: list[GlucoseReading],
    target_low_mgdl: float,
    target_high_mgdl: float,
    units: str,
    tz: tzinfo,
) -> list[dict]:
    """One rollup per local calendar day (in ``tz``), oldest first."""
    by_day: dict[str, list[GlucoseReading]] = {}
    for r in readings:
        day = datetime.fromtimestamp(r.ts_utc, tz).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(r)
    out = []
    for day in sorted(by_day):
        row = {"day": day}
        row.update(_bucket_stats(by_day[day], target_low_mgdl, target_high_mgdl, units))
        out.append(row)
    return out


def hourly_profile(
    readings: list[GlucoseReading],
    target_low_mgdl: float,
    target_high_mgdl: float,
    units: str,
    tz: tzinfo,
) -> list[dict]:
    """Average glucose by hour-of-day (0-23) in ``tz``, pooled across all days.
    Surfaces time-of-day patterns like a dawn rise or a post-lunch peak."""
    by_hour: dict[int, list[GlucoseReading]] = {}
    for r in readings:
        hour = datetime.fromtimestamp(r.ts_utc, tz).hour
        by_hour.setdefault(hour, []).append(r)
    out = []
    for hour in sorted(by_hour):
        row = {"hour": hour}
        row.update(_bucket_stats(by_hour[hour], target_low_mgdl, target_high_mgdl, units))
        out.append(row)
    return out


def low_episodes(
    readings: list[GlucoseReading],
    target_low_mgdl: float,
    units: str,
) -> list[dict]:
    """Collapse runs of below-range readings into discrete episodes so 60 low
    samples over one long dip read as a single event, not 60. Consecutive lows
    within ``_EPISODE_GAP`` belong to the same episode."""
    lows = sorted((r for r in readings if r.value_mgdl < target_low_mgdl), key=lambda r: r.ts_utc)
    episodes: list[dict] = []
    current: list[GlucoseReading] = []

    def _flush() -> None:
        if not current:
            return
        nadir = min(current, key=lambda r: r.value_mgdl)
        episodes.append(
            {
                "start_utc": current[0].ts_utc,
                "end_utc": current[-1].ts_utc,
                "duration_min": round((current[-1].ts_utc - current[0].ts_utc) / 60),
                "nadir": to_display(nadir.value_mgdl, units),
                "nadir_utc": nadir.ts_utc,
                "reading_count": len(current),
            }
        )

    for r in lows:
        if current and r.ts_utc - current[-1].ts_utc > _EPISODE_GAP:
            _flush()
            current = []
        current.append(r)
    _flush()
    return episodes


def insulin_summary(doses: list[InsulinDose]) -> dict:
    """Totals and per-kind counts/units. The ratio of corrections to meal
    boluses is a behavioural signal (chasing highs vs. covering carbs)."""
    by_kind: dict[str, dict] = {}
    for d in doses:
        k = by_kind.setdefault(d.kind or "bolus", {"count": 0, "units": 0.0})
        k["count"] += 1
        k["units"] += d.units
    for k in by_kind.values():
        k["units"] = round(k["units"], 1)
    return {
        "count": len(doses),
        "total_units": round(sum(d.units for d in doses), 1),
        "by_kind": by_kind,
    }


def carb_coverage(meals: list[Meal]) -> dict:
    """How many logged meals actually carry a carb count — the gate on any
    carb-ratio analysis. Reported so improving logging discipline is measurable."""
    total = len(meals)
    with_carbs = sum(1 for m in meals if m.total_carbs is not None)
    return {
        "total": total,
        "with_carbs": with_carbs,
        "percent": round(100 * with_carbs / total, 1) if total else 0.0,
    }


def _fromtimestamp_utc(ts: int) -> datetime:
    """Small shared helper kept here so report/tests need not re-import timezone."""
    return datetime.fromtimestamp(ts, timezone.utc)
