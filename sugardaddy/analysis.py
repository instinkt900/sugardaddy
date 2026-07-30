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

from sugardaddy.bolus import bolus_reference, describe
from sugardaddy.constants import mgdl_to_mmol, to_display
from sugardaddy.iob import DEFAULT_DIA_MINUTES, DEFAULT_PEAK_MINUTES, active_iob, is_rapid
from sugardaddy.models import GlucoseReading, InsulinDose, Meal

# A meal's "starting" glucose is the nearest reading within this window (seconds).
# The same window defines a dose "co-timed" with the meal (its meal bolus).
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
    doses: list[InsulinDose] | None = None,
    *,
    dia_minutes: float = DEFAULT_DIA_MINUTES,
    peak_minutes: float = DEFAULT_PEAK_MINUTES,
    isf_mgdl: float | None = None,
    icr: float | None = None,
    target_mgdl: float | None = None,
) -> list[dict]:
    """For each meal, the glucose at meal time and the peak/end over the next 2h,
    plus insulin context so the response is readable: the rapid-acting dose
    co-timed with the meal (``bolus_units``) and the active insulin already
    working from *earlier* doses when the meal started (``iob_start_units``).

    The two are disjoint by construction — ``iob_start_units`` counts only doses
    before the co-timed window, so it excludes the meal's own bolus (a +4 spike
    with 8 u on board reads very differently from one with none, and a flat
    response may just be an earlier dose still coming down).

    When ``isf_mgdl``/``icr``/``target_mgdl`` are supplied, each row also carries
    the EXPERIMENTAL bolus reference for that meal (``ref_*``) so the dose that
    was actually given can be read beside a calculated one. Unset → the fields are
    absent entirely and nothing about this function changes."""
    if not readings:
        return []
    ordered = sorted(readings, key=lambda r: r.ts_utc)
    doses = doses or []
    out: list[dict] = []

    for meal in meals:
        start = _nearest(ordered, meal.ts_utc, _MEAL_MATCH_WINDOW)
        window = [r for r in ordered if meal.ts_utc <= r.ts_utc <= meal.ts_utc + _POST_MEAL_WINDOW]
        if start is None or not window:
            continue
        peak = max(window, key=lambda r: r.value_mgdl)
        end = window[-1]
        # Meal bolus = rapid-acting doses co-timed with the meal (± match window).
        bolus_units = sum(
            d.units
            for d in doses
            if is_rapid(d) and abs(d.ts_utc - meal.ts_utc) <= _MEAL_MATCH_WINDOW
        )
        # IOB at start = insulin from strictly-earlier doses still active now,
        # i.e. the depot excluding this meal's own (co-timed) bolus.
        prior = [d for d in doses if d.ts_utc < meal.ts_utc - _MEAL_MATCH_WINDOW]
        iob_start = active_iob(
            prior, meal.ts_utc, dia_minutes=dia_minutes, peak_minutes=peak_minutes
        )
        row = {
            "meal_id": meal.id,
            "ts_utc": meal.ts_utc,
            "description": meal.label,
            "carbs_g": meal.total_carbs,
            "start_display": to_display(start.value_mgdl, units),
            "peak_display": to_display(peak.value_mgdl, units),
            "peak_delta_display": _delta_display(peak.value_mgdl - start.value_mgdl, units),
            "end_display": to_display(end.value_mgdl, units),
            "minutes_to_peak": round((peak.ts_utc - meal.ts_utc) / 60),
            "bolus_units": round(bolus_units, 1),
            "iob_start_units": round(iob_start, 1),
        }
        # Gate on ISF alone, matching bolus_backtest and the config docs: one
        # switch turns the experimental reference on everywhere or nowhere.
        if isf_mgdl is not None:
            ref = bolus_reference(
                bg_mgdl=start.value_mgdl,
                target_mgdl=target_mgdl,
                isf_mgdl_per_unit=isf_mgdl,
                icr_g_per_unit=icr,
                # Partial plate → unknown, not a smaller meal (see Meal.carbs_complete).
                carbs_g=meal.total_carbs if meal.carbs_complete else None,
                iob_units=iob_start,
            )
            row["ref"] = ref.as_dict()
            row["ref_note"] = describe(ref)
            # Signed gap: positive means the user gave MORE than the reference.
            row["ref_delta_units"] = (
                None if not ref.available else round(bolus_units - ref.suggested_units, 1)
            )
        out.append(row)
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


def daily_intake(meals: list[Meal], doses: list[InsulinDose], tz: tzinfo) -> list[dict]:
    """Carbs, calories and mealtime insulin per local calendar day, oldest first.

    The insulin total is bolus+correction only (`iob.is_rapid` decides, so "what
    counts as rapid" stays defined in one place). Basal is a separate depot on its
    own schedule, and a single 36 u basal folded into this column would swamp the
    mealtime doses the row exists to sit beside.

    Carbs and calories are summed from the meals that carry them, and each column
    says whether *every* meal that day contributed one. A plate logged without
    calories makes the day's total an understatement rather than a fact, and a
    table that can't admit that invites arithmetic it doesn't support.

    A day appears only if something was logged on it: no meals and no rapid doses
    means no row, rather than a run of zeroes implying a day of fasting.
    """
    days: dict[str, dict] = {}

    def row_for(ts: int) -> dict:
        day = datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")
        return days.setdefault(
            day,
            {
                "day": day,
                "carbs_g": 0.0,
                "calories": 0.0,
                "insulin_units": 0.0,
                "meal_count": 0,
                "dose_count": 0,
                "carbs_complete": True,
                "calories_complete": True,
            },
        )

    for m in meals:
        row = row_for(m.ts_utc)
        row["meal_count"] += 1
        # Partial figures still count toward the total — what was logged is real,
        # and dropping the whole meal would put the number further from the truth,
        # not closer. The flag is what stops it being read as complete. Same
        # treatment the post-meal reference gives an incomplete plate.
        if m.total_carbs is None or not m.carbs_complete:
            row["carbs_complete"] = False
        if m.total_carbs is not None:
            row["carbs_g"] += m.total_carbs
        # There is no Meal.calories_complete to lean on, but the rule is the same.
        if m.total_calories is None or not all(i.calories is not None for i in m.items):
            row["calories_complete"] = False
        if m.total_calories is not None:
            row["calories"] += m.total_calories

    for d in doses:
        if not is_rapid(d):
            continue
        row = row_for(d.ts_utc)
        row["dose_count"] += 1
        row["insulin_units"] += d.units

    out = []
    for day in sorted(days):
        row = days[day]
        row["carbs_g"] = round(row["carbs_g"], 1)
        row["calories"] = round(row["calories"])
        row["insulin_units"] = round(row["insulin_units"], 1)
        out.append(row)
    return out


def basal_status(
    last_basal_ts: int | None,
    now: int,
    *,
    interval_hours: float = 24.0,
    leniency_hours: float = 1.0,
) -> dict:
    """Whether a basal dose has gone unlogged for longer than it should have.

    Basal is the one dose that is easy to forget and invisible in the glucose
    trace until hours later, so it is worth a reminder. Note what this measures:
    a gap in the *log*. It reports that no dose has been recorded — never that one
    should now be taken.

    ``interval_hours`` is the expected gap between basal doses and
    ``leniency_hours`` is grace on top of it, kept separate so 24 h stays the
    honest threshold while the nag holds off until 25.

    ``last_basal_ts`` of None means no basal has ever been logged, which is
    deliberately *not* overdue: a fresh install — or someone who isn't on basal at
    all — must not be nagged about a dose there is no evidence they ever take.
    """
    if last_basal_ts is None:
        return {
            "last_ts": None,
            "hours_since": None,
            "due_at": None,
            "due": False,
            "overdue_hours": 0.0,
        }
    due_at = last_basal_ts + round((interval_hours + leniency_hours) * 3600)
    return {
        "last_ts": last_basal_ts,
        "hours_since": round((now - last_basal_ts) / 3600, 1),
        "due_at": due_at,
        "due": now >= due_at,
        "overdue_hours": round(max(0, now - due_at) / 3600, 1),
    }


def carb_coverage(meals: list[Meal]) -> dict:
    """How many logged meals actually carry a carb count — the gate on any
    carb-ratio analysis. Reported so improving logging discipline is measurable.

    ``partial`` counts plates where only *some* items were carbed: they show a
    total and so count as covered, but the figure understates the meal, which
    matters to anything doing arithmetic on it."""
    total = len(meals)
    with_carbs = sum(1 for m in meals if m.total_carbs is not None)
    partial = sum(1 for m in meals if m.total_carbs is not None and not m.carbs_complete)
    return {
        "total": total,
        "with_carbs": with_carbs,
        "partial": partial,
        "percent": round(100 * with_carbs / total, 1) if total else 0.0,
    }


def bolus_backtest(
    readings: list[GlucoseReading],
    doses: list[InsulinDose],
    meals: list[Meal],
    units: str,
    *,
    isf_mgdl: float | None = None,
    icr: float | None = None,
    target_mgdl: float | None = None,
    dia_minutes: float = DEFAULT_DIA_MINUTES,
    peak_minutes: float = DEFAULT_PEAK_MINUTES,
    history_doses: list[InsulinDose] | None = None,
) -> dict:
    """EXPERIMENTAL: replay each rapid-acting dose against the calculated reference.

    For every bolus/correction actually given, reconstruct what the formula would
    have said *at that moment* — glucose then, carbs from any co-timed meal, and
    the IOB carried in from strictly earlier doses — and report the gap. The
    output is the "how well does it match what I decided?" view: it grades the
    **calculator** against the user's judgement, not the other way round.

    Gated on a configured ISF: with none, this returns ``available: False`` and a
    reason rather than a number. The agreement stats are split by whether carbs
    were logged, because a dose whose carb half is missing is not a fair test of
    the formula — it is a test of the logging.

    ``doses`` are the ones scored; pass ``history_doses`` (reaching a DIA further
    back) so a dose near the window start still sees the IOB carried into it."""
    if isf_mgdl is None:
        return {"available": False, "reason": "no ISF configured", "events": [], "agreement": None}

    ordered = sorted(readings, key=lambda r: r.ts_utc)
    rapid = sorted((d for d in doses if is_rapid(d)), key=lambda d: d.ts_utc)
    history = history_doses if history_doses is not None else doses
    events = []

    for dose in rapid:
        bg = _nearest(ordered, dose.ts_utc, _MEAL_MATCH_WINDOW)
        # Carbs credited to this dose: any co-timed meal (same window that pairs a
        # meal with its bolus elsewhere). No meal at all → a pure correction, which
        # is carbs=0 rather than "unknown"; a meal logged *without* carbs is the
        # genuinely unknown case and blocks the carb half.
        near = [m for m in meals if abs(m.ts_utc - dose.ts_utc) <= _MEAL_MATCH_WINDOW]
        if not near:
            carbs, carbs_known = 0.0, True
        else:
            # Every co-timed plate must be *fully* carbed: a meal with some items
            # logged and some not would otherwise pass as a smaller, confident meal.
            carbs_known = all(m.carbs_complete for m in near)
            carbs = sum(m.total_carbs or 0.0 for m in near) if carbs_known else None

        prior = [d for d in history if d.ts_utc < dose.ts_utc]
        iob = active_iob(prior, dose.ts_utc, dia_minutes=dia_minutes, peak_minutes=peak_minutes)
        ref = bolus_reference(
            bg_mgdl=None if bg is None else bg.value_mgdl,
            target_mgdl=target_mgdl,
            isf_mgdl_per_unit=isf_mgdl,
            icr_g_per_unit=icr,
            carbs_g=carbs,
            iob_units=iob,
        )
        events.append(
            {
                "ts_utc": dose.ts_utc,
                "kind": dose.kind or "bolus",
                "actual_units": dose.units,
                "glucose_display": None if bg is None else to_display(bg.value_mgdl, units),
                "carbs_g": carbs,
                "carbs_known": carbs_known,
                "iob_units": round(iob, 2),
                "ref": ref.as_dict(),
                "ref_note": describe(ref),
                # Positive = the user gave more than the reference.
                "delta_units": (
                    None if not ref.available else round(dose.units - ref.suggested_units, 1)
                ),
            }
        )

    events.sort(key=lambda e: e["ts_utc"], reverse=True)
    return {
        "available": True,
        "reason": "",
        "events": events,
        "agreement": _agreement(events),
    }


def _agreement(events: list[dict]) -> dict:
    """How closely the reference tracked the doses actually given. ``full_inputs``
    counts only events where every input was present — the honest denominator, and
    usually far smaller than the event count while carb logging is patchy."""
    scored = [e for e in events if e["delta_units"] is not None]
    full = [e for e in scored if e["carbs_known"] and not e["ref"]["missing"]]
    base = full or scored
    if not base:
        return {"n": 0, "n_full_inputs": 0, "mean_abs_delta": None,
                "mean_signed_delta": None, "within_1u_percent": None}
    deltas = [e["delta_units"] for e in base]
    return {
        "n": len(base),
        "n_full_inputs": len(full),
        "mean_abs_delta": round(sum(abs(d) for d in deltas) / len(deltas), 2),
        # Signed mean shows systematic bias: negative = the formula asks for more
        # than the user gives, which usually means ISF/ICR need revisiting.
        "mean_signed_delta": round(sum(deltas) / len(deltas), 2),
        "within_1u_percent": round(100 * sum(1 for d in deltas if abs(d) <= 1) / len(deltas), 1),
    }


def _fromtimestamp_utc(ts: int) -> datetime:
    """Small shared helper kept here so report/tests need not re-import timezone."""
    return datetime.fromtimestamp(ts, timezone.utc)
