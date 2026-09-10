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
from datetime import datetime, timedelta, timezone, tzinfo

from sugardaddy.bolus import bolus_reference, describe
from sugardaddy.constants import mgdl_to_mmol, to_display
from sugardaddy.iob import DEFAULT_DIA_MINUTES, DEFAULT_PEAK_MINUTES, active_iob, is_rapid
from sugardaddy.models import GlucoseReading, InsulinDose, Meal, Note

# A meal's "starting" glucose is the nearest reading within this window (seconds).
# The same window defines a dose "co-timed" with the meal (its meal bolus).
_MEAL_MATCH_WINDOW = 20 * 60
_POST_MEAL_WINDOW = 2 * 60 * 60

# Wider than the co-timing window above, and for a different question. That one
# asks "which dose covered this plate" and must not claim a dose that belongs to
# something else. This one asks "how did the two land relative to each other",
# where a dose 30 minutes after the first bite is exactly the habit worth seeing
# rather than a pairing to discard.
_BOLUS_PAIR_WINDOW = 45 * 60

# Named parts of the day, used wherever meals or doses are grouped by when they
# happen. Half-open [start, end) on the local hour; "night" wraps midnight.
_DAY_BANDS = (
    ("morning", 5, 11),
    ("midday", 11, 16),
    ("evening", 16, 21),
    ("night", 21, 5),
)

# Two sub-range readings are part of the same episode if no more than this many
# seconds apart — bridges the odd dropped CGM sample without merging separate dips.
_EPISODE_GAP = 20 * 60

# Floor on the robust spread used for outlier rejection, in mg/dL. Readings are
# whole mg/dL and frequently sit flat for several minutes, which drives MAD to
# zero; without a floor, every 1 mg/dL neighbour of a plateau looks like an
# infinite-sigma outlier and the filter eats the signal it was meant to clean.
_MIN_SPREAD_MGDL = 2.0


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

    A meal logged in the last few minutes has **no** post-meal readings yet, and
    dropping it until the sensor catches up made freshly logged plates vanish from
    the table for several minutes. Such a meal is emitted as a ``pending`` row
    instead: its insulin context (bolus, IOB, reference) is complete and useful
    immediately, and the glucose columns are ``None`` until readings arrive. A
    meal with no readings that is *older* than the data is a genuine sensor gap
    and is still skipped. Callers doing arithmetic over responses must filter
    ``pending`` out (``meal_response_groups`` does).

    When ``isf_mgdl``/``icr``/``target_mgdl`` are supplied, each row also carries
    the EXPERIMENTAL bolus reference for that meal (``ref_*``) so the dose that
    was actually given can be read beside a calculated one. Unset → the fields are
    absent entirely and nothing about this function changes.

    The reference covers whatever carbs *were* logged, even on a plate where some
    items have none: ``ref_carbs_partial`` says so, and the figure is then a floor
    rather than an answer. Blanking it out instead only hid the arithmetic — the
    correction half and the known carbs are both real, and a meal with 60 g logged
    and one uncounted side is far better served by a marked figure than by a
    dash."""
    if not readings:
        return []
    ordered = sorted(readings, key=lambda r: r.ts_utc)
    last_ts = ordered[-1].ts_utc
    doses = doses or []
    out: list[dict] = []

    for meal in meals:
        start = _nearest(ordered, meal.ts_utc, _MEAL_MATCH_WINDOW)
        window = [r for r in ordered if meal.ts_utc <= r.ts_utc <= meal.ts_utc + _POST_MEAL_WINDOW]
        # Nothing after the plate yet, but the data simply hasn't got there —
        # the meal is still unfolding, not unreadable.
        pending = not window and meal.ts_utc + _POST_MEAL_WINDOW > last_ts
        if not pending and (start is None or not window):
            continue
        peak = max(window, key=lambda r: r.value_mgdl) if window else None
        end = window[-1] if window else None
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
        # Nearest rapid dose on a wider window than the co-timing one, signed:
        # negative = insulin before the plate (a pre-bolus), positive = chasing it
        # afterwards. None means nothing rapid landed anywhere near the meal.
        near = [d for d in doses if is_rapid(d) and abs(d.ts_utc - meal.ts_utc) <= _BOLUS_PAIR_WINDOW]
        lag_min = (
            round((min(near, key=lambda d: abs(d.ts_utc - meal.ts_utc)).ts_utc - meal.ts_utc) / 60)
            if near
            else None
        )
        row = {
            "meal_id": meal.id,
            "ts_utc": meal.ts_utc,
            "description": meal.label,
            "meal_type": meal.meal_type or "",
            # The plate itself, not just its label — so a response can be read
            # against what was actually on it and foods can be grouped across meals.
            "items": [
                {"name": i.name, "count": i.count, "carbs_g": i.carbs_g} for i in meal.items
            ],
            "carbs_g": meal.total_carbs,
            "carbs_complete": meal.carbs_complete,
            "bolus_lag_min": lag_min,
            # True while the response is still arriving: the glucose columns below
            # are None and mean "not yet", never "flat".
            "pending": peak is None,
            "start_display": None if start is None else to_display(start.value_mgdl, units),
            "peak_display": None if peak is None else to_display(peak.value_mgdl, units),
            "peak_delta_display": (
                None if peak is None or start is None
                else _delta_display(peak.value_mgdl - start.value_mgdl, units)
            ),
            "end_display": None if end is None else to_display(end.value_mgdl, units),
            "minutes_to_peak": None if peak is None else round((peak.ts_utc - meal.ts_utc) / 60),
            "bolus_units": round(bolus_units, 1),
            "iob_start_units": round(iob_start, 1),
        }
        # Gate on ISF alone, matching bolus_backtest and the config docs: one
        # switch turns the experimental reference on everywhere or nowhere.
        if isf_mgdl is not None:
            ref = bolus_reference(
                bg_mgdl=None if start is None else start.value_mgdl,
                target_mgdl=target_mgdl,
                isf_mgdl_per_unit=isf_mgdl,
                icr_g_per_unit=icr,
                # A plate where only some items carry a carb count still gets a
                # figure, built from the carbs that ARE known. Withholding it
                # entirely obfuscated more than it protected: the response table
                # went blank on the majority of real plates, including ones with
                # substantial logged carbs beside one unlogged side dish. The
                # figure is a floor, and `ref_carbs_partial` marks it as one so
                # it can be shown with the same "*" the rest of the app uses.
                carbs_g=meal.total_carbs,
                iob_units=iob_start,
            )
            row["ref"] = ref.as_dict()
            row["ref_carbs_partial"] = (
                meal.total_carbs is not None and not meal.carbs_complete
            )
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


def basal_adherence(
    doses: list[InsulinDose],
    start_utc: int,
    end_utc: int,
    tzinfo,
    *,
    prescribed_units: float | None = None,
) -> dict:
    """Basal doses per local day across the window, against the prescription.

    This counts *days*, not doses, because that is the question a clinician
    actually asks: was the once-daily dose taken each day? Two doses on one day
    and none the next totals the same as one a day and means something quite
    different.

    ``prescribed_units`` is the clinician's figure when one has been recorded.
    Note what it is used for and what it is not: days are compared against it to
    count matches, and nothing here suggests, adjusts, or judges a dose. With no
    prescription the day counts still stand on their own — the comparison fields
    simply go None rather than the function inventing an expected value.

    Days are local days, so a dose at 23:50 and one at 00:10 land on the dates a
    person would put them on. Partial days at the window edges are included and
    flagged, since the caller's window rarely lands on midnight.
    """
    basals = [d for d in doses if (d.kind or "") == "basal" and start_utc <= d.ts_utc <= end_utc]

    by_day: dict[str, dict] = {}
    day = datetime.fromtimestamp(start_utc, tzinfo).date()
    last = datetime.fromtimestamp(end_utc, tzinfo).date()
    while day <= last:
        by_day[day.isoformat()] = {"day": day.isoformat(), "count": 0, "units": 0.0}
        day += timedelta(days=1)

    for d in basals:
        key = datetime.fromtimestamp(d.ts_utc, tzinfo).date().isoformat()
        # A dose can only fall outside the prebuilt range if the caller passed a
        # dose list wider than its own window; ignore rather than inventing a day.
        if key not in by_day:
            continue
        by_day[key]["count"] += 1
        by_day[key]["units"] += d.units

    days = []
    for entry in by_day.values():
        entry["units"] = round(entry["units"], 1)
        # "matches" is only meaningful with a prescription to match against.
        entry["matches_prescribed"] = (
            None
            if prescribed_units is None
            else (entry["count"] == 1 and abs(entry["units"] - prescribed_units) < 0.05)
        )
        days.append(entry)
    days.sort(key=lambda e: e["day"])

    # The edge days are partial, so a zero there may just be the window cutting
    # in after the usual dose time. Flag them instead of counting them as missed.
    if days:
        days[0]["partial"] = True
        days[-1]["partial"] = True

    whole = [e for e in days if not e.get("partial")]
    return {
        "days": days,
        "day_count": len(days),
        "whole_day_count": len(whole),
        "dose_count": len(basals),
        "total_units": round(sum(d.units for d in basals), 1),
        "days_with_none": sum(1 for e in whole if e["count"] == 0),
        "days_with_multiple": sum(1 for e in whole if e["count"] > 1),
        "prescribed_units": prescribed_units,
        "days_matching_prescribed": (
            None if prescribed_units is None else sum(1 for e in whole if e["matches_prescribed"])
        ),
    }


def smooth_glucose(
    readings: list[GlucoseReading],
    units: str,
    *,
    window_minutes: float = 31.0,
    reject_sigmas: float = 3.0,
    min_samples: int = 5,
) -> list[dict]:
    """A trend line through the readings: outliers rejected, then averaged.

    CGM traces carry a persistent wobble of roughly ±0.8 mmol/L that alternates
    up-down-up on a 25–35 minute cycle. It is not the bloodstream doing that — it
    is the sensor's lag-compensating filter ringing, plus local perfusion changes
    at the sensor site. This function exists to take it out so the underlying
    movement is legible.

    Two stages, because they answer different problems:

    * A **Hampel filter** replaces any reading that sits more than
      ``reject_sigmas`` robust deviations from its neighbourhood's median. That
      catches a genuine one-off — a dropped-out sample, a compression spike —
      without touching a real excursion, whose neighbours have moved with it.
      Median/MAD rather than mean/σ deliberately: an outlier can't drag the
      statistic used to detect it.
    * A **centred mean** over ``window_minutes`` then cancels the oscillation.
      The window wants to span one whole cycle — averaging a full period of
      something that swings symmetrically leaves nothing of it behind — which is
      where the 31-minute default comes from. Shorten it and the trend line just
      tracks the ringing it was meant to remove.

    Windows are measured in *time*, not in samples, so a gap doesn't quietly widen
    them. Where a window holds fewer than ``min_samples`` readings the point is
    skipped rather than guessed, so the line breaks over a sensor outage instead of
    drawing a confident average across it.

    Centred, not trailing: this is a retrospective chart, so using both sides is
    both more accurate and free of lag. The trade is that the most recent half
    window is built from fewer readings — true of the oldest half too — which is
    the honest cost of not inventing a value.
    """
    if not readings:
        return []

    ts = [r.ts_utc for r in readings]
    raw = [r.value_mgdl for r in readings]
    n = len(readings)
    half = window_minutes * 60 / 2

    def window_bounds():
        """Yield (i, lo, hi) index bounds of the time window centred on each point.
        Both pointers only move forward, so the whole scan stays linear."""
        lo = hi = 0
        for i in range(n):
            while ts[lo] < ts[i] - half:
                lo += 1
            while hi + 1 < n and ts[hi + 1] <= ts[i] + half:
                hi += 1
            yield i, lo, hi

    # --- stage 1: reject outliers -------------------------------------------
    cleaned = list(raw)
    for i, lo, hi in window_bounds():
        window = raw[lo : hi + 1]
        med = statistics.median(window)
        # 1.4826 × MAD estimates σ for normally-distributed data. Readings are
        # stored as whole mg/dL and often sit dead flat for minutes, which makes
        # MAD zero and every neighbour an "infinite" outlier — hence the floor.
        spread = max(1.4826 * statistics.median([abs(v - med) for v in window]), _MIN_SPREAD_MGDL)
        if abs(raw[i] - med) > reject_sigmas * spread:
            cleaned[i] = med

    # --- stage 2: average ----------------------------------------------------
    out = []
    for i, lo, hi in window_bounds():
        window = cleaned[lo : hi + 1]
        if len(window) < min_samples:
            continue
        out.append({
            "ts_utc": ts[i],
            # One digit finer than the conventional rounding: this is a mean of
            # ~31 readings, so the extra precision is real, and without it the
            # line is drawn as a staircase (steps between points average ~0.04
            # mmol/L, well under the 0.1 the display unit rounds to).
            "value": to_display(sum(window) / len(window), units, extra_dp=1),
        })
    return out


def day_coverage(
    readings: list[GlucoseReading], tz: tzinfo, *, max_gap_seconds: int = 600
) -> dict[str, float]:
    """Fraction of each local day that actually has CGM data behind it, 0..1.

    Counting readings would mean knowing the cadence, and this app's history holds
    more than one: the live poller writes roughly once a minute, while a Home
    Assistant backfill or the source's own graph window arrive every five. A day
    recorded at 5-minute cadence is not two thirds empty, so coverage is measured
    in *time* rather than in rows.

    Each consecutive pair within a day contributes the time between them, capped at
    ``max_gap_seconds``. Past that cap the sensor was plainly not reporting — a
    failed sensor, a flat phone, a day away from the receiver — so the span counts
    as a hole rather than as covered time.

    A day's average glucose is only comparable to another day's if both cover
    roughly the same amount of day, which is what this exists to say.
    """
    by_day: dict[str, list[int]] = {}
    for r in readings:
        day = datetime.fromtimestamp(r.ts_utc, tz).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(r.ts_utc)

    out: dict[str, float] = {}
    for day, stamps in by_day.items():
        stamps.sort()
        covered = sum(
            min(b - a, max_gap_seconds) for a, b in zip(stamps, stamps[1:])
        )
        out[day] = min(1.0, covered / 86400)
    return out


def day_window_start(now: int, tz: tzinfo, days: int) -> int:
    """Epoch of the local midnight that opens a window of the last ``days`` days.

    A rolling ``now - days × 86400`` window slices its oldest day in half, which
    makes a per-day table lie: a part-day of food lands in a column beside whole
    ones and reads as a light day rather than a clipped one. Snapping the start to
    local midnight is what makes every row a whole day — today excepted, since it
    is still in progress, which the table has to say out loud rather than hide.

    ``days = 1`` means "today so far". DST-safe: it steps back over calendar dates
    and rebuilds midnight in ``tz`` instead of subtracting a fixed span of seconds.
    """
    today = datetime.fromtimestamp(now, tz).date()
    first = today - timedelta(days=max(1, days) - 1)
    return int(datetime(first.year, first.month, first.day, tzinfo=tz).timestamp())


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


def notes_by_day(notes: list[Note], tz: tzinfo) -> list[dict]:
    """Free-text context events grouped by local calendar day, oldest first.

    There is no maths to do here — a note carries no number. The grouping is the
    whole point: the day key is the same ``%Y-%m-%d`` string ``daily_breakdown``
    and ``daily_intake`` use, so a row that sits 3 mmol/L above the rest of the
    week can be read next to "vomiting bug, ate nothing" instead of being
    explained by guesswork. Making that connection is the reader's job; this only
    puts the two within reach of each other.

    A day appears only if something was noted on it — an empty day is the normal
    case, not a gap worth a row.
    """
    days: dict[str, list[dict]] = {}
    for n in notes:
        local = datetime.fromtimestamp(n.ts_utc, tz)
        days.setdefault(local.strftime("%Y-%m-%d"), []).append(
            {"ts_utc": n.ts_utc, "time": local.strftime("%H:%M"), "text": n.text}
        )
    return [
        {"day": day, "notes": sorted(days[day], key=lambda n: n["ts_utc"])}
        for day in sorted(days)
    ]


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


def _clock(minute_of_day: float) -> str:
    m = int(round(minute_of_day)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _band(hour: int) -> str:
    for name, start, end in _DAY_BANDS:
        if start <= end:
            if start <= hour < end:
                return name
        elif hour >= start or hour < end:  # the band that wraps midnight
            return name
    return "night"


def _clock_span(minutes: list[int]) -> dict:
    """Where a set of clock times sits, as the tightest window that holds them.

    Clock times are circular: 23:40 and 00:10 are half an hour apart, but a plain
    min/max calls that a 23½-hour spread and an average puts it at noon. So the
    window is found by rotating to the *largest empty gap* — the times either side
    of that gap are the ends of the habit, and the spread is what is left.
    """
    if not minutes:
        return {"median": None, "earliest": None, "latest": None, "spread_min": None}
    xs = sorted(minutes)
    n = len(xs)
    gaps = [(xs[(i + 1) % n] - xs[i]) % 1440 for i in range(n)]
    g = max(range(n), key=lambda i: gaps[i])  # the hole nobody doses/eats in
    start, end = xs[(g + 1) % n], xs[g]

    # Walk the times in window order, unrolling past midnight so the median is
    # taken on a straight line rather than a wrapped one.
    unrolled, base, prev = [], 0, None
    for i in range(n):
        v = xs[(g + 1 + i) % n]
        if prev is not None and v < prev:
            base += 1440
        unrolled.append(v + base)
        prev = v
    return {
        "median": _clock(statistics.median(unrolled)),
        "earliest": _clock(start),
        "latest": _clock(end),
        "spread_min": (end - start) % 1440,
    }


def _sizes(values: list[float]) -> dict:
    """Mean/median/min/max of a set of doses or amounts, or nulls if empty."""
    if not values:
        return {"mean": None, "median": None, "min": None, "max": None}
    return {
        "mean": round(statistics.fmean(values), 1),
        "median": round(statistics.median(values), 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
    }


def insulin_timing(doses: list[InsulinDose], tz: tzinfo) -> dict:
    """When doses land and how big they are, per kind — insulin as a habit.

    ``insulin_summary`` answers how much and how often; this answers *when*, which
    a total cannot show. A basal always at 22:00 and one scattered over six hours
    produce the same weekly units and are not the same behaviour, and a correction
    habit that clusters in one part of the day says where the day goes wrong.

    Nothing here judges a dose. It reports the pattern of the ones that were given.
    """
    by_kind: dict[str, dict] = {}
    for d in sorted(doses, key=lambda d: d.ts_utc):
        local = datetime.fromtimestamp(d.ts_utc, tz)
        k = by_kind.setdefault(
            d.kind or "bolus",
            {"count": 0, "total_units": 0.0, "_units": [], "_minutes": [], "by_band": {}},
        )
        k["count"] += 1
        k["total_units"] += d.units
        k["_units"].append(d.units)
        k["_minutes"].append(local.hour * 60 + local.minute)
        band = _band(local.hour)
        k["by_band"][band] = k["by_band"].get(band, 0) + 1

    for k in by_kind.values():
        k["total_units"] = round(k["total_units"], 1)
        k["units"] = _sizes(k.pop("_units"))
        k["clock"] = _clock_span(k.pop("_minutes"))
    return {"by_kind": by_kind}


def meal_timing(meals: list[Meal], tz: tzinfo) -> dict:
    """The shape of the eating day: how many plates, at what times, how far apart.

    Grazing and three square meals can log the same carbs, and the difference is
    only visible in the timestamps. ``intervals_hours`` counts the gaps *within* a
    local day (short ones are snacking on top of meals); ``overnight_fast_hours``
    is the gap from a day's last plate to the next day's first, which is the one
    long window the glucose trace gets to settle in.

    Meal type is whatever the user tagged; untagged plates are grouped under
    ``unspecified`` rather than guessed at from the clock.
    """
    ordered = sorted(meals, key=lambda m: m.ts_utc)
    by_type: dict[str, dict] = {}
    by_band: dict[str, int] = {}
    by_day: dict[str, list[Meal]] = {}

    for m in ordered:
        local = datetime.fromtimestamp(m.ts_utc, tz)
        t = by_type.setdefault(
            m.meal_type or "unspecified", {"count": 0, "_minutes": [], "_carbs": []}
        )
        t["count"] += 1
        t["_minutes"].append(local.hour * 60 + local.minute)
        if m.total_carbs is not None:
            t["_carbs"].append(m.total_carbs)
        band = _band(local.hour)
        by_band[band] = by_band.get(band, 0) + 1
        by_day.setdefault(local.strftime("%Y-%m-%d"), []).append(m)

    for t in by_type.values():
        t["clock"] = _clock_span(t.pop("_minutes"))
        carbs = t.pop("_carbs")
        t["carbs_g"] = _sizes(carbs)
        t["with_carbs"] = len(carbs)

    # Gaps between plates inside one local day, and the overnight break between
    # days. Split because they answer different questions and a single list of
    # "gaps" would let a 12-hour night dominate the median of the snacking ones.
    intervals: list[float] = []
    for day in sorted(by_day):
        rows = by_day[day]
        intervals += [
            (b.ts_utc - a.ts_utc) / 3600 for a, b in zip(rows, rows[1:]) if b.ts_utc > a.ts_utc
        ]
    fasts: list[float] = []
    days = sorted(by_day)
    for prev_day, next_day in zip(days, days[1:]):
        # Only across consecutive calendar days: a gap over a day with no meals
        # logged is a hole in the record, not a 36-hour fast.
        if (
            datetime.strptime(next_day, "%Y-%m-%d") - datetime.strptime(prev_day, "%Y-%m-%d")
        ).days != 1:
            continue
        fasts.append((by_day[next_day][0].ts_utc - by_day[prev_day][-1].ts_utc) / 3600)

    counts = [len(v) for v in by_day.values()]
    return {
        "meal_count": len(ordered),
        "days_with_meals": len(by_day),
        "per_day": _sizes([float(c) for c in counts]),
        "by_type": by_type,
        "by_band": by_band,
        "intervals_hours": _sizes(intervals),
        "overnight_fast_hours": _sizes(fasts),
    }


def _bolus_timing_band(lag_min: int | None) -> str:
    """How a meal's insulin sat against the plate. ``None`` = no rapid dose within
    ``_BOLUS_PAIR_WINDOW``, which is a real category, not missing data."""
    if lag_min is None:
        return "no rapid dose"
    if lag_min <= -5:
        return "pre-bolus"
    if lag_min < 5:
        return "with meal"
    return "after eating"


def _response_group(rows: list[dict]) -> dict:
    """Pooled response figures for a set of ``post_meal`` rows."""
    peaks = [r["peak_delta_display"] for r in rows if r.get("peak_delta_display") is not None]
    carbs = [r["carbs_g"] for r in rows if r.get("carbs_g") is not None]
    return {
        "n": len(rows),
        "mean_peak_delta": round(statistics.fmean(peaks), 1) if peaks else None,
        "median_peak_delta": round(statistics.median(peaks), 1) if peaks else None,
        "max_peak_delta": max(peaks) if peaks else None,
        "mean_minutes_to_peak": round(statistics.fmean([r["minutes_to_peak"] for r in rows])),
        "mean_peak": round(statistics.fmean([r["peak_display"] for r in rows]), 1),
        "with_carbs": len(carbs),
        "mean_carbs_g": round(statistics.fmean(carbs), 1) if carbs else None,
        "mean_bolus_units": round(statistics.fmean([r["bolus_units"] for r in rows]), 1),
    }


def meal_response_groups(
    responses: list[dict],
    tz: tzinfo,
    *,
    min_occurrences: int = 2,
) -> dict:
    """Post-meal responses pooled by what was eaten, when, and how insulin landed.

    One meal's +4 is anecdote; the same food doing it five times is a pattern, and
    that only shows up pooled. Takes the rows ``post_meal_responses`` already
    produced rather than re-deriving anything.

    Association, and weak association at that. A plate is several foods at once,
    and the same food recurs beside different doses, different starting glucose
    and different days — so a food at the top of ``by_food`` is somewhere to look,
    never something shown to have caused the rise. ``by_food`` needs
    ``min_occurrences`` sightings before a food is pooled at all; one-offs stay
    visible as individual rows in ``post_meal``.
    """
    by_food: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    by_type: dict[str, list[dict]] = {}
    by_band: dict[str, list[dict]] = {}
    by_bolus: dict[str, list[dict]] = {}

    # A meal whose 2h window is still filling has no response to pool yet; letting
    # one in would drag every average toward a number that doesn't exist.
    for row in [r for r in responses if not r.get("pending")]:
        for item in row.get("items", []):
            key = item["name"].strip().casefold()
            if not key:
                continue
            names.setdefault(key, item["name"].strip())
            by_food.setdefault(key, []).append(row)
        by_type.setdefault(row.get("meal_type") or "unspecified", []).append(row)
        by_band.setdefault(_band(datetime.fromtimestamp(row["ts_utc"], tz).hour), []).append(row)
        by_bolus.setdefault(_bolus_timing_band(row.get("bolus_lag_min")), []).append(row)

    foods = [
        dict(_response_group(rows), food=names[key])
        for key, rows in by_food.items()
        if len(rows) >= min_occurrences
    ]
    # Worst mean rise first: that is the order the question is asked in.
    foods.sort(key=lambda g: (g["mean_peak_delta"] is None, -(g["mean_peak_delta"] or 0)))

    def grouped(src: dict[str, list[dict]], label: str) -> list[dict]:
        out = [dict(_response_group(rows), **{label: key}) for key, rows in src.items()]
        out.sort(key=lambda g: -g["n"])
        return out

    return {
        "min_occurrences": min_occurrences,
        "by_food": foods,
        "foods_below_threshold": sum(1 for rows in by_food.values() if len(rows) < min_occurrences),
        "by_meal_type": grouped(by_type, "meal_type"),
        "by_time_of_day": grouped(by_band, "band"),
        "by_bolus_timing": grouped(by_bolus, "timing"),
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
