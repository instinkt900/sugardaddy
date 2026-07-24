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

from sugardaddy import analysis  # noqa: E402
from sugardaddy.constants import mmol_to_mgdl  # noqa: E402
from sugardaddy.models import GlucoseReading, InsulinDose, Meal, MealItem  # noqa: E402

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
    assert cc == {"total": 2, "with_carbs": 1, "percent": 50.0}, cc
    assert analysis.carb_coverage([])["percent"] == 0.0


def test_post_meal_still_sorted_recent_first():
    readings = [r(i * 300, 8.0) for i in range(0, 30)]  # every 5 min, ~2.5h
    meals = [
        Meal(ts_utc=T0 + 100, name="early"),
        Meal(ts_utc=T0 + 3600, name="late"),
    ]
    out = analysis.post_meal_responses(readings, meals, UNITS)
    assert [m["description"] for m in out] == ["late", "early"], out


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")


if __name__ == "__main__":
    _run_all()
