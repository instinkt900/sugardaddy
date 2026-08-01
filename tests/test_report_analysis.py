"""Tests for the pure analysis functions behind `sugardaddy report`.

No test framework is required: run directly with

    python tests/test_report_analysis.py

Each check is a plain ``assert``; the file is also import-safe for pytest if it
is ever added. Timestamps are fixed epoch seconds so nothing depends on the wall
clock or the machine's local zone.
"""

from __future__ import annotations

import sys
from datetime import timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugardaddy import analysis, iob  # noqa: E402
from sugardaddy.constants import mgdl_to_mmol, mmol_to_mgdl  # noqa: E402
from sugardaddy.models import GlucoseReading, InsulinDose, Meal, MealItem, Note  # noqa: E402

UNITS = "mmol/L"
LOW = mmol_to_mgdl(3.9)
HIGH = mmol_to_mgdl(10.0)

# 2026-07-23 00:00:00 UTC as an anchor for building fixtures.
T0 = 1_784_764_800


def r(offset_s: int, mmol: float) -> GlucoseReading:
    return GlucoseReading(ts_utc=T0 + offset_s, value_mgdl=mmol_to_mgdl(mmol))


def test_variability():
    v = analysis.variability([r(0, 5.0), r(60, 5.0), r(120, 5.0)], UNITS)
    assert v["cv_percent"] == 0.0, v  # identical values -> no spread
    v2 = analysis.variability([r(0, 4.0), r(60, 10.0)], UNITS)
    assert v2["mean"] == 7.0, v2
    assert v2["cv_percent"] > 0, v2
    assert analysis.variability([], UNITS)["n"] == 0


def test_low_episodes_grouping():
    # Two dips: a long one (10 contiguous 1-min lows) and a single later low,
    # separated by a >20-min in-range gap -> must be TWO episodes, not eleven.
    readings = [r(i * 60, 3.2) for i in range(10)]  # 00:00–00:09, nadir 3.2
    readings.append(r(50 * 60, 8.0))  # in range at 00:50 (breaks the run)
    readings.append(r(90 * 60, 3.7))  # lone low at 01:30
    eps = analysis.low_episodes(readings, LOW, UNITS)
    assert len(eps) == 2, eps
    assert eps[0]["reading_count"] == 10, eps[0]
    assert eps[0]["nadir"] == 3.2, eps[0]
    assert eps[0]["duration_min"] == 9, eps[0]
    assert eps[1]["reading_count"] == 1, eps[1]


def test_low_episodes_none():
    assert analysis.low_episodes([r(0, 6.0), r(60, 7.0)], LOW, UNITS) == []


def test_daily_and_hourly_use_tz():
    # 22:00 UTC on Jul 22 is 08:00 next day in +10; the day/hour bucketing must
    # follow the supplied tz, not UTC.
    plus10 = timezone.utc  # placeholder replaced below
    from datetime import timedelta

    plus10 = timezone(timedelta(hours=10))
    # T0 is 2026-07-23 00:00 UTC = 2026-07-23 10:00 in +10.
    daily = analysis.daily_breakdown([r(0, 8.0)], LOW, HIGH, UNITS, plus10)
    assert daily[0]["day"] == "2026-07-23", daily
    hourly = analysis.hourly_profile([r(0, 8.0)], LOW, HIGH, UNITS, plus10)
    assert hourly[0]["hour"] == 10, hourly
    # In UTC the same reading lands at hour 0.
    hourly_utc = analysis.hourly_profile([r(0, 8.0)], LOW, HIGH, UNITS, timezone.utc)
    assert hourly_utc[0]["hour"] == 0, hourly_utc


def test_bucket_tir():
    # 4 readings: 2 in range, 1 low, 1 high -> TIR 50%, below 25%, above 25%.
    rs = [r(0, 3.0), r(60, 6.0), r(120, 8.0), r(180, 15.0)]
    d = analysis.daily_breakdown(rs, LOW, HIGH, UNITS, timezone.utc)[0]
    assert d["tir_percent"] == 50.0, d
    assert d["below_percent"] == 25.0, d
    assert d["above_percent"] == 25.0, d


def test_insulin_summary():
    doses = [
        InsulinDose(ts_utc=T0, units=8.0, kind="correction"),
        InsulinDose(ts_utc=T0 + 60, units=7.0, kind="bolus"),
        InsulinDose(ts_utc=T0 + 120, units=6.0, kind="correction"),
    ]
    s = analysis.insulin_summary(doses)
    assert s["count"] == 3
    assert s["total_units"] == 21.0, s
    assert s["by_kind"]["correction"]["count"] == 2, s
    assert s["by_kind"]["correction"]["units"] == 14.0, s
    assert s["by_kind"]["bolus"]["count"] == 1, s
    assert analysis.insulin_summary([])["count"] == 0


def test_carb_coverage():
    with_carbs = Meal(ts_utc=T0, items=[MealItem(name="toast", carbs_g=20.0, count=1)])
    without = Meal(ts_utc=T0 + 60, items=[MealItem(name="mystery", count=1)])
    cc = analysis.carb_coverage([with_carbs, without])
    assert cc == {"total": 2, "with_carbs": 1, "partial": 0, "percent": 50.0}, cc
    assert analysis.carb_coverage([])["percent"] == 0.0


def test_day_window_start_snaps_to_local_midnight():
    from datetime import datetime, timedelta

    plus10 = timezone(timedelta(hours=10))
    # T0 is 2026-07-23 10:00 in +10. A 3-day window must open at midnight on the
    # 21st, not 72 h before now — otherwise the oldest row is a part-day.
    start = analysis.day_window_start(T0, plus10, 3)
    assert datetime.fromtimestamp(start, plus10).isoformat() == "2026-07-21T00:00:00+10:00"
    # days=1 is "today so far", opening at this morning's midnight.
    one = analysis.day_window_start(T0, plus10, 1)
    assert datetime.fromtimestamp(one, plus10).isoformat() == "2026-07-23T00:00:00+10:00"
    assert one > T0 - 86400  # i.e. NOT a rolling 24h window


def test_day_window_start_survives_dst():
    # Sydney leaves DST at 03:00 on 2026-04-05 (+11 -> +10), making that day 25 h
    # long. Stepping back over calendar dates has to land on real midnights; a
    # fixed 86400 multiple would drift an hour and pull in part of the day before.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    syd = ZoneInfo("Australia/Sydney")
    now = int(datetime(2026, 4, 6, 12, 0, tzinfo=syd).timestamp())  # day after
    start = analysis.day_window_start(now, syd, 3)
    assert datetime.fromtimestamp(start, syd).isoformat() == "2026-04-04T00:00:00+11:00"
    naive = now - 3 * 86400  # what a rolling window would have given
    assert start != naive and datetime.fromtimestamp(naive, syd).hour == 13


def test_smooth_cancels_the_sensor_oscillation():
    # The real artifact this exists for: ~0.8 mmol/L swinging up-down on a ~30 min
    # cycle, riding on a flat 8.0. A window spanning one whole period should leave
    # almost none of it behind.
    import math

    readings = [
        r(i * 60, 8.0 + 0.8 * math.sin(2 * math.pi * i / 30))
        for i in range(240)  # 4 h of 1-minute data
    ]
    out = analysis.smooth_glucose(readings, UNITS)
    # Ignore the first/last half-window, where the window is only partly filled.
    middle = [p["value"] for p in out if 20 * 60 <= p["ts_utc"] - T0 <= 200 * 60]
    assert middle, out
    worst = max(abs(v - 8.0) for v in middle)
    assert worst < 0.15, f"oscillation survived smoothing: worst deviation {worst}"
    # And the raw series really did carry the swing we just removed.
    assert max(abs(mgdl_to_mmol(x.value_mgdl) - 8.0) for x in readings) > 0.75


def test_smooth_rejects_a_lone_spike_but_keeps_a_real_move():
    # One wild reading in an otherwise flat trace must not drag the line.
    flat = [r(i * 60, 6.0) for i in range(120)]
    flat[60] = r(60 * 60, 14.0)  # single absurd sample
    out = analysis.smooth_glucose(flat, UNITS)
    at_spike = [p["value"] for p in out if abs(p["ts_utc"] - (T0 + 60 * 60)) < 90]
    assert at_spike and max(at_spike) < 6.2, at_spike

    # A genuine sustained shift is a different thing and must survive: 6 -> 10 and
    # stay there. Smoothing may round the corner, it must not erase the step.
    step = [r(i * 60, 6.0) for i in range(120)] + [r((120 + i) * 60, 10.0) for i in range(120)]
    out = analysis.smooth_glucose(step, UNITS)
    ends = [p["value"] for p in out if p["ts_utc"] - T0 > 200 * 60]
    assert ends and min(ends) > 9.8, ends


def test_smooth_keeps_enough_precision_to_draw_a_curve():
    # Measured on real data, the smoothed line moves about 0.04 mmol/L per point.
    # Rounded to the conventional 0.1 that becomes runs of identical values with
    # sudden steps between them, and the chart draws a staircase, not a line.
    ramp = [r(i * 60, 6.0 + i * 0.04) for i in range(120)]
    vals = [p["value"] for p in analysis.smooth_glucose(ramp, UNITS)]
    repeats = sum(1 for a, b in zip(vals, vals[1:]) if a == b)
    assert repeats / len(vals) < 0.1, f"{repeats}/{len(vals)} points repeat — line will look blocky"

    # Slope-independent version of the same property: whatever the data does, the
    # series must resolve finer than the display convention would. On a genuinely
    # flat stretch a flat line is correct, so counting repeats alone proves nothing.
    flat = [r(i * 60, 6.0 + i * 0.002) for i in range(240)]
    out = [p["value"] for p in analysis.smooth_glucose(flat, UNITS)]
    assert len(set(out)) > 3 * len({round(v, 1) for v in out}), sorted(set(out))[:5]

    # mg/dL is quoted as a whole number, and needs the same treatment.
    v2 = [p["value"] for p in analysis.smooth_glucose(flat, "mg/dL")]
    assert len(set(v2)) > 3 * len({round(v) for v in v2})


def test_smooth_breaks_over_a_sensor_gap_instead_of_guessing():
    # 10 min of data, an 8 h hole, then 10 min more. Points whose window is nearly
    # empty are skipped rather than averaged across the gap.
    before = [r(i * 60, 6.0) for i in range(10)]
    after = [r(8 * 3600 + i * 60, 12.0) for i in range(10)]
    out = analysis.smooth_glucose(before + after, UNITS, min_samples=5)
    assert all(p["value"] < 7 or p["value"] > 11 for p in out), out
    assert analysis.smooth_glucose([], UNITS) == []


def test_day_coverage_is_measured_in_time_not_rows():
    # A full day at 5-minute cadence (288 rows) and one at 1-minute cadence (1440)
    # are both fully covered. Counting rows would call the first one two-thirds
    # empty, which is the trap this function exists to avoid: history is backfilled
    # at 5 min, while the live poller writes about once a minute.
    coarse = [r(i * 300, 6.0) for i in range(288)]
    fine = [r(i * 60, 6.0) for i in range(1440)]
    cov = analysis.day_coverage(coarse, timezone.utc)
    assert cov["2026-07-23"] > 0.99, cov
    assert analysis.day_coverage(fine, timezone.utc)["2026-07-23"] > 0.99


def test_day_coverage_counts_a_sensor_gap_as_a_hole():
    # 6 h of readings, then nothing. The single 18 h gap must not count as covered
    # time just because a reading sits on either side of it.
    morning = [r(i * 300, 6.0) for i in range(72)]      # 00:00–06:00
    assert 0.24 < analysis.day_coverage(morning, timezone.utc)["2026-07-23"] < 0.26

    # Same day, but with an 18 h hole in the middle rather than at the end.
    split = [r(i * 300, 6.0) for i in range(36)] + [r(21 * 3600 + i * 300, 6.0) for i in range(36)]
    cov = analysis.day_coverage(split, timezone.utc)["2026-07-23"]
    assert cov < 0.3, cov  # ~6 h of data + one capped gap, not ~24 h


def test_day_coverage_is_empty_without_readings():
    assert analysis.day_coverage([], timezone.utc) == {}


def test_daily_rollups_agree_on_day_keys():
    # /api/daily merges daily_breakdown's glucose onto daily_intake's rows by the
    # "day" key. If either side ever changed its key format or bucketing, the merge
    # would quietly produce blank glucose columns rather than fail, so pin it here.
    from datetime import timedelta

    plus10 = timezone(timedelta(hours=10))
    ts = T0 + 14 * 3600  # 2026-07-24 00:00 in +10 — the far side of a local midnight
    intake = analysis.daily_intake([Meal(ts_utc=ts, name="dinner")], [], plus10)
    glucose = analysis.daily_breakdown([r(14 * 3600, 8.0)], LOW, HIGH, UNITS, plus10)
    assert intake[0]["day"] == glucose[0]["day"] == "2026-07-24", (intake, glucose)
    # And the fields the merge reads are the ones daily_breakdown actually emits.
    assert "avg" in glucose[0] and "n" in glucose[0], glucose[0]


def test_daily_intake_excludes_basal():
    # The whole point of the column: a big nightly basal must not land in the same
    # total as the mealtime doses it sits beside.
    doses = [
        InsulinDose(ts_utc=T0, units=6.0, kind="bolus"),
        InsulinDose(ts_utc=T0 + 60, units=2.5, kind="correction"),
        InsulinDose(ts_utc=T0 + 120, units=36.0, kind="basal"),
        InsulinDose(ts_utc=T0 + 180, units=1.5, kind=""),  # unset kind = bolus
    ]
    day = analysis.daily_intake([], doses, timezone.utc)[0]
    assert day["insulin_units"] == 10.0, day
    assert day["dose_count"] == 3, day


def test_daily_intake_totals_and_partial_flags():
    full = Meal(ts_utc=T0, items=[MealItem(name="toast", carbs_g=20.0, calories=150.0, count=2)])
    # Carbs logged, calories not: the carb total stays trustworthy, calories don't.
    carbs_only = Meal(ts_utc=T0 + 60, items=[MealItem(name="juice", carbs_g=30.0, count=1)])
    day = analysis.daily_intake([full, carbs_only], [], timezone.utc)[0]
    assert day["carbs_g"] == 70.0, day       # 20x2 + 30
    assert day["calories"] == 300, day       # 150x2, juice contributes nothing
    assert day["carbs_complete"] is True, day
    assert day["calories_complete"] is False, day
    assert day["meal_count"] == 2, day


def test_daily_intake_partial_plate_still_counts():
    # One item carbed, one not. The figure is a floor, not a total — it is kept
    # (what was logged is real) but must not claim to be complete.
    plate = Meal(ts_utc=T0, items=[
        MealItem(name="rice", carbs_g=45.0, count=1),
        MealItem(name="curry", count=1),
    ])
    day = analysis.daily_intake([plate], [], timezone.utc)[0]
    assert day["carbs_g"] == 45.0 and day["carbs_complete"] is False, day


def test_daily_intake_splits_on_local_days():
    from datetime import timedelta

    plus10 = timezone(timedelta(hours=10))
    # Both meals fall on Jul 23 in UTC (01:00 and 14:00), but in +10 the second is
    # already 00:00 on the 24th. One UTC row, two local rows: the tz has to drive
    # the split, or a late dinner lands on yesterday.
    meals = [Meal(ts_utc=T0 + 3600, name="lunch"), Meal(ts_utc=T0 + 14 * 3600, name="dinner")]
    assert [d["day"] for d in analysis.daily_intake(meals, [], plus10)] == [
        "2026-07-23", "2026-07-24",
    ]
    assert [d["day"] for d in analysis.daily_intake(meals, [], timezone.utc)] == ["2026-07-23"]


def test_daily_intake_skips_days_with_nothing_logged():
    # A basal-only day contributes no rapid insulin and no meal, so it must not
    # appear as a row of zeroes implying a day of fasting.
    basal = [InsulinDose(ts_utc=T0, units=36.0, kind="basal")]
    assert analysis.daily_intake([], basal, timezone.utc) == []
    assert analysis.daily_intake([], [], timezone.utc) == []


def test_post_meal_still_sorted_recent_first():
    readings = [r(i * 300, 8.0) for i in range(0, 30)]  # every 5 min, ~2.5h
    meals = [
        Meal(ts_utc=T0 + 100, name="early"),
        Meal(ts_utc=T0 + 3600, name="late"),
    ]
    out = analysis.post_meal_responses(readings, meals, UNITS)
    assert [m["description"] for m in out] == ["late", "early"], out
    # No doses passed -> insulin context defaults to zero, never missing.
    assert out[0]["bolus_units"] == 0.0 and out[0]["iob_start_units"] == 0.0, out[0]


def test_iob_curve_endpoints_and_decay():
    dia, tp = 300, 75
    # Full unit on board at the instant of the dose; gone by DIA.
    assert iob.iob_fraction(0, dia, tp) == 1.0
    assert iob.iob_fraction(dia, dia, tp) == 0.0
    assert iob.iob_fraction(dia + 60, dia, tp) == 0.0
    # Monotonically decaying and bounded within (0, 1) in between.
    f60 = iob.iob_fraction(60, dia, tp)
    f180 = iob.iob_fraction(180, dia, tp)
    assert 0.0 < f180 < f60 < 1.0, (f60, f180)


def test_activity_curve_peaks_near_tp_and_zeros_at_ends():
    dia, tp = 300, 70
    assert iob.activity_fraction(0, dia, tp) == 0.0
    assert iob.activity_fraction(dia, dia, tp) == 0.0
    # The action rate should be maximal near the configured time-to-peak.
    grid = {m: iob.activity_fraction(m, dia, tp) for m in range(5, dia, 5)}
    peak_m = max(grid, key=grid.get)
    assert abs(peak_m - tp) <= 15, (peak_m, tp)
    # Activity integrated over the whole window should recover ~one unit of IOB
    # decay (area under the rate curve ≈ 1 per unit). Coarse check.
    area = sum(iob.activity_fraction(m, dia, tp) for m in range(0, dia)) * 1  # 1-min steps
    assert 0.9 < area < 1.1, area


def test_activity_phase_rising_peak_falling():
    dose = [InsulinDose(ts_utc=T0, units=5.0, kind="bolus")]
    # Well before the ~70min peak: rising, below full intensity.
    early = iob.activity_phase(dose, T0 + 20 * 60, dia_minutes=300, peak_minutes=70)
    assert early is not None and early[1] == 1 and 0 < early[0] < 1, early
    # At the configured peak: ~full intensity.
    at_peak = iob.activity_phase(dose, T0 + 70 * 60, dia_minutes=300, peak_minutes=70)
    assert at_peak[0] > 0.95, at_peak
    # On the tail: falling, reduced intensity.
    late = iob.activity_phase(dose, T0 + 200 * 60, dia_minutes=300, peak_minutes=70)
    assert late[1] == -1 and 0 < late[0] < 1, late
    # Basal-only and empty → no active rapid action.
    assert iob.activity_phase([InsulinDose(ts_utc=T0, units=30.0, kind="basal")], T0 + 3600, dia_minutes=300, peak_minutes=70) is None
    assert iob.activity_phase([], T0, dia_minutes=300, peak_minutes=70) is None


def test_active_activity_excludes_basal():
    doses = [
        InsulinDose(ts_utc=T0 - 70 * 60, units=5.0, kind="bolus"),
        InsulinDose(ts_utc=T0 - 70 * 60, units=36.0, kind="basal"),  # excluded
    ]
    rate = iob.active_activity(doses, T0, dia_minutes=300, peak_minutes=70)
    expected = 5.0 * iob.activity_fraction(70, 300, 70)
    assert abs(rate - expected) < 1e-9, (rate, expected)


def test_active_iob_excludes_basal_and_expired():
    # One rapid dose 60 min ago contributes; a basal depot never does; an ancient
    # rapid dose past DIA has fully decayed.
    doses = [
        InsulinDose(ts_utc=T0 - 60 * 60, units=5.0, kind="bolus"),
        InsulinDose(ts_utc=T0 - 60 * 60, units=36.0, kind="basal"),  # excluded
        InsulinDose(ts_utc=T0 - 10 * 60 * 60, units=8.0, kind="bolus"),  # expired
    ]
    active = iob.active_iob(doses, T0, dia_minutes=300, peak_minutes=75)
    expected = 5.0 * iob.iob_fraction(60, 300, 75)
    assert abs(active - expected) < 1e-9, (active, expected)


def test_post_meal_insulin_context():
    readings = [r(i * 300, 8.0) for i in range(0, 30)]  # every 5 min, ~2.5h
    meal = Meal(ts_utc=T0 + 3600, name="dinner")
    doses = [
        # Co-timed with the meal (within the 20-min match window) -> meal bolus.
        InsulinDose(ts_utc=T0 + 3600 + 300, units=6.0, kind="bolus"),
        # An hour before the meal -> prior IOB, must NOT count as the bolus.
        InsulinDose(ts_utc=T0 + 3600 - 3600, units=4.0, kind="bolus"),
        # A basal an hour before -> neither bolus nor rapid IOB.
        InsulinDose(ts_utc=T0 + 3600 - 3600, units=36.0, kind="basal"),
    ]
    out = analysis.post_meal_responses(readings, [meal], UNITS, doses)
    row = out[0]
    assert row["bolus_units"] == 6.0, row
    prior_iob = 4.0 * iob.iob_fraction(60, iob.DEFAULT_DIA_MINUTES, iob.DEFAULT_PEAK_MINUTES)
    assert row["iob_start_units"] == round(prior_iob, 1), (row, prior_iob)


def test_notes_group_by_local_day_in_order():
    notes = [
        Note(ts_utc=T0 + 14 * 3600, text="second"),   # 14:00 UTC
        Note(ts_utc=T0 + 3600, text="first"),         # 01:00 UTC
        Note(ts_utc=T0 + 30 * 3600, text="next day"),
    ]
    out = analysis.notes_by_day(notes, timezone.utc)
    assert [d["day"] for d in out] == ["2026-07-23", "2026-07-24"], out
    # Notes arrive in whatever order the caller had them; within a day they must
    # come out chronological, or the narrative reads backwards.
    assert [n["text"] for n in out[0]["notes"]] == ["first", "second"], out[0]
    assert [n["time"] for n in out[0]["notes"]] == ["01:00", "14:00"], out[0]


def test_notes_split_on_the_local_day_not_utc():
    from datetime import timedelta

    plus10 = timezone(timedelta(hours=10))
    # 14:00 UTC is already 00:00 the next day in +10. The day key has to follow
    # the display timezone so a note lines up with the `daily` row it explains.
    notes = [Note(ts_utc=T0 + 3600, text="lunch"), Note(ts_utc=T0 + 14 * 3600, text="late")]
    assert [d["day"] for d in analysis.notes_by_day(notes, plus10)] == [
        "2026-07-23", "2026-07-24",
    ]
    assert [d["day"] for d in analysis.notes_by_day(notes, timezone.utc)] == ["2026-07-23"]


def test_notes_by_day_is_empty_without_notes():
    # An unnoted window is the normal case, not a gap needing a placeholder row.
    assert analysis.notes_by_day([], timezone.utc) == []


def test_notes_share_the_day_key_with_the_glucose_breakdown():
    # The whole value of the section is lining a note up against its day, so the
    # two keys have to be the same string — pinned here like the daily rollups.
    from zoneinfo import ZoneInfo

    syd = ZoneInfo("Australia/Sydney")
    ts = T0 + 20 * 3600  # 20:00 UTC = 06:00 next day in Sydney
    day = analysis.daily_breakdown([r(20 * 3600, 8.0)], LOW, HIGH, UNITS, syd)[0]["day"]
    assert analysis.notes_by_day([Note(ts_utc=ts, text="woke up rough")], syd)[0]["day"] == day


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")


if __name__ == "__main__":
    _run_all()
