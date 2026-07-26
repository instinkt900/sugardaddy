"""Tests for the EXPERIMENTAL bolus reference (sugardaddy.bolus + the backtest).

No test framework is required: run directly with

    python tests/test_bolus.py

Same conventions as test_report_analysis.py — plain ``assert``, fixed epoch
timestamps, pytest-compatible. The emphasis here is on the *gating*: this is the
one module that can emit a dose-shaped number, so "refuses to answer when an
input is missing" is as important to pin down as the arithmetic itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugardaddy import analysis  # noqa: E402
from sugardaddy.bolus import bolus_reference, describe  # noqa: E402
from sugardaddy.constants import mmol_to_mgdl  # noqa: E402
from sugardaddy.models import GlucoseReading, InsulinDose, Meal, MealItem  # noqa: E402

UNITS = "mmol/L"
T0 = 1_784_764_800  # 2026-07-23 00:00:00 UTC

# 2.5 mmol/L per unit, 10 g per unit, aiming at 6.0 mmol/L.
ISF = mmol_to_mgdl(2.5)
ICR = 10.0
TARGET = mmol_to_mgdl(6.0)


def r(offset_s: int, mmol: float) -> GlucoseReading:
    return GlucoseReading(ts_utc=T0 + offset_s, value_mgdl=mmol_to_mgdl(mmol))


def meal(offset_s: int, carbs: float | None, name: str = "meal") -> Meal:
    item = MealItem(name=name, count=1, carbs_g=carbs)
    return Meal(ts_utc=T0 + offset_s, name=name, items=[item])


# --------------------------------------------------------------------------
# the formula itself
# --------------------------------------------------------------------------


def test_components_add_up():
    # 60 g at 10 g/u = 6 u; 11 mmol/L against a 6.0 target at 2.5 = +2 u; minus 1 u IOB.
    ref = bolus_reference(
        bg_mgdl=mmol_to_mgdl(11.0), target_mgdl=TARGET, isf_mgdl_per_unit=ISF,
        icr_g_per_unit=ICR, carbs_g=60, iob_units=1.0,
    )
    assert ref.carb_units == 6.0, ref
    assert ref.correction_units == 2.0, ref
    assert ref.suggested_units == 7.0, ref  # 6 + 2 - 1
    assert ref.available and ref.missing == []


def test_below_target_gives_negative_correction():
    """Under target the correction must EAT INTO the carb cover, not clamp to 0 —
    that subtraction is the whole point of dosing with a low-ish starting BG."""
    ref = bolus_reference(
        bg_mgdl=mmol_to_mgdl(3.5), target_mgdl=TARGET, isf_mgdl_per_unit=ISF,
        icr_g_per_unit=ICR, carbs_g=50, iob_units=0.0,
    )
    assert ref.correction_units == -1.0, ref
    assert ref.suggested_units == 4.0, ref  # 5 - 1


def test_iob_subtraction_floors_at_zero():
    """Insulin already on board can cancel the dose entirely, but never invert it."""
    ref = bolus_reference(
        bg_mgdl=mmol_to_mgdl(7.0), target_mgdl=TARGET, isf_mgdl_per_unit=ISF,
        icr_g_per_unit=ICR, carbs_g=0, iob_units=6.0,
    )
    assert ref.suggested_units == 0.0, ref
    assert ref.iob_units == 6.0


def test_missing_isf_blocks_the_correction_half():
    ref = bolus_reference(
        bg_mgdl=mmol_to_mgdl(12.0), target_mgdl=TARGET, isf_mgdl_per_unit=None,
        icr_g_per_unit=ICR, carbs_g=30, iob_units=0.0,
    )
    assert "isf" in ref.missing
    assert ref.correction_units is None
    assert ref.suggested_units == 3.0, ref  # carb half still usable


def test_no_inputs_at_all_returns_no_number():
    """The safety case: nothing computable must yield None, not a confident 0."""
    ref = bolus_reference(
        bg_mgdl=None, target_mgdl=None, isf_mgdl_per_unit=None,
        icr_g_per_unit=None, carbs_g=None, iob_units=0.0,
    )
    assert ref.suggested_units is None
    assert not ref.available
    assert "isf" in ref.missing and "icr" in ref.missing
    assert describe(ref) == ""


def test_unknown_carbs_differs_from_zero_carbs():
    """A meal logged without carbs must NOT be treated as a zero-carb meal."""
    known = bolus_reference(
        bg_mgdl=TARGET, target_mgdl=TARGET, isf_mgdl_per_unit=ISF,
        icr_g_per_unit=ICR, carbs_g=0, iob_units=0.0,
    )
    unknown = bolus_reference(
        bg_mgdl=TARGET, target_mgdl=TARGET, isf_mgdl_per_unit=ISF,
        icr_g_per_unit=ICR, carbs_g=None, iob_units=0.0,
    )
    assert known.carb_units == 0.0 and known.missing == []
    assert unknown.carb_units is None and "carbs" in unknown.missing


def test_describe_shows_every_component():
    ref = bolus_reference(
        bg_mgdl=mmol_to_mgdl(11.0), target_mgdl=TARGET, isf_mgdl_per_unit=ISF,
        icr_g_per_unit=ICR, carbs_g=60, iob_units=1.0,
    )
    note = describe(ref)
    assert "6u carbs" in note and "+2u correction" in note and "-1u active" in note, note


# --------------------------------------------------------------------------
# the backtest
# --------------------------------------------------------------------------


def test_backtest_is_gated_on_isf():
    out = analysis.bolus_backtest([r(0, 8.0)], [InsulinDose(ts_utc=T0, units=3)], [], UNITS)
    assert out["available"] is False
    assert "ISF" in out["reason"]
    assert out["events"] == [] and out["agreement"] is None


def test_backtest_scores_a_meal_dose():
    readings = [r(0, 11.0)]
    doses = [InsulinDose(ts_utc=T0, units=8.0, kind="bolus")]
    out = analysis.bolus_backtest(
        readings, doses, [meal(0, 60)], UNITS,
        isf_mgdl=ISF, icr=ICR, target_mgdl=TARGET,
    )
    assert out["available"] is True
    (e,) = out["events"]
    assert e["carbs_g"] == 60 and e["carbs_known"] is True
    assert e["ref"]["suggested_units"] == 8.0  # 6 carb + 2 correction, no IOB
    assert e["delta_units"] == 0.0             # gave exactly the reference
    assert out["agreement"]["n"] == 1 and out["agreement"]["within_1u_percent"] == 100.0


def test_backtest_treats_a_lone_dose_as_a_correction():
    """No meal nearby means carbs=0 (known), not carbs=unknown — otherwise every
    standalone correction would be unscoreable."""
    out = analysis.bolus_backtest(
        [r(0, 11.0)], [InsulinDose(ts_utc=T0, units=3.0, kind="correction")], [], UNITS,
        isf_mgdl=ISF, icr=ICR, target_mgdl=TARGET,
    )
    (e,) = out["events"]
    assert e["carbs_g"] == 0.0 and e["carbs_known"] is True
    assert e["ref"]["suggested_units"] == 2.0   # (11-6)/2.5
    assert e["delta_units"] == 1.0              # gave 1 u more than the reference


def test_backtest_subtracts_earlier_insulin():
    """The anti-stacking guard: an identical second dose must score differently
    because the first one is still working."""
    doses = [
        InsulinDose(ts_utc=T0, units=5.0, kind="bolus"),
        InsulinDose(ts_utc=T0 + 3600, units=5.0, kind="correction"),
    ]
    out = analysis.bolus_backtest(
        [r(0, 11.0), r(3600, 11.0)], doses, [], UNITS,
        isf_mgdl=ISF, icr=ICR, target_mgdl=TARGET,
    )
    first = [e for e in out["events"] if e["ts_utc"] == T0][0]
    second = [e for e in out["events"] if e["ts_utc"] == T0 + 3600][0]
    assert first["iob_units"] == 0.0
    assert second["iob_units"] > 3.0, second      # most of 5 u still on board
    assert second["ref"]["suggested_units"] == 0.0  # fully covered already
    assert second["delta_units"] == 5.0            # flagged as stacking


def test_backtest_excludes_basal():
    out = analysis.bolus_backtest(
        [r(0, 11.0)], [InsulinDose(ts_utc=T0, units=36.0, kind="basal")], [], UNITS,
        isf_mgdl=ISF, icr=ICR, target_mgdl=TARGET,
    )
    assert out["events"] == []


def test_agreement_prefers_fully_logged_events():
    """A dose whose carbs were never logged tests the logging, not the formula, so
    it must not pollute the headline agreement number."""
    doses = [
        InsulinDose(ts_utc=T0, units=8.0, kind="bolus"),
        InsulinDose(ts_utc=T0 + 7200, units=8.0, kind="bolus"),
    ]
    meals = [meal(0, 60), meal(7200, None, "unlogged")]
    out = analysis.bolus_backtest(
        [r(0, 11.0), r(7200, 11.0)], doses, meals, UNITS,
        isf_mgdl=ISF, icr=ICR, target_mgdl=TARGET,
    )
    a = out["agreement"]
    assert a["n_full_inputs"] == 1
    assert a["n"] == 1                 # scored on the complete event only
    assert a["mean_abs_delta"] == 0.0  # the unlogged one would have skewed this
    unlogged = [e for e in out["events"] if e["carbs_g"] is None][0]
    assert "carbs" in unlogged["ref"]["missing"]


def test_partly_carbed_plate_counts_as_unknown():
    """A plate with 2 items where only 1 carries carbs must NOT pass as a
    confident smaller meal — that silently understates the carb half and lets an
    incomplete event into the agreement stats."""
    partial = Meal(
        ts_utc=T0,
        name="eggs on toast",
        items=[MealItem(name="egg with toast", count=4, carbs_g=20), MealItem(name="chocolate")],
    )
    assert partial.total_carbs == 80.0     # display total is unchanged
    assert partial.carbs_complete is False  # but arithmetic must not trust it

    out = analysis.bolus_backtest(
        [r(0, 6.9)], [InsulinDose(ts_utc=T0, units=8.0, kind="bolus")], [partial], UNITS,
        isf_mgdl=ISF, icr=ICR, target_mgdl=TARGET,
    )
    (e,) = out["events"]
    assert e["carbs_known"] is False
    assert e["carbs_g"] is None
    assert "carbs" in e["ref"]["missing"]
    assert out["agreement"]["n_full_inputs"] == 0

    rows = analysis.post_meal_responses(
        [r(0, 6.9), r(3600, 7.5)], [partial], UNITS, [InsulinDose(ts_utc=T0, units=8.0)],
        isf_mgdl=ISF, icr=ICR, target_mgdl=TARGET,
    )
    assert "carbs" in rows[0]["ref"]["missing"]


def test_fully_carbed_plate_still_counts():
    """The fix must not make every multi-item meal unknown."""
    full = Meal(
        ts_utc=T0,
        name="full plate",
        items=[MealItem(name="toast", count=2, carbs_g=20), MealItem(name="juice", carbs_g=15)],
    )
    assert full.carbs_complete is True and full.total_carbs == 55.0
    out = analysis.bolus_backtest(
        [r(0, 6.0)], [InsulinDose(ts_utc=T0, units=5.5, kind="bolus")], [full], UNITS,
        isf_mgdl=ISF, icr=ICR, target_mgdl=TARGET,
    )
    (e,) = out["events"]
    assert e["carbs_known"] is True and e["carbs_g"] == 55.0
    assert out["agreement"]["n_full_inputs"] == 1


def test_carb_coverage_reports_partial_plates():
    meals = [
        Meal(ts_utc=T0, items=[MealItem(name="a", carbs_g=10)]),                        # complete
        Meal(ts_utc=T0 + 1, items=[MealItem(name="a", carbs_g=10), MealItem(name="b")]),  # partial
        Meal(ts_utc=T0 + 2, items=[MealItem(name="b")]),                                # none
    ]
    cc = analysis.carb_coverage(meals)
    assert cc["total"] == 3
    assert cc["with_carbs"] == 2  # unchanged: partial still shows a total
    assert cc["partial"] == 1


def test_post_meal_reference_is_absent_without_isf():
    """The desktop columns hinge on this key being missing when ISF is unset."""
    rows = analysis.post_meal_responses(
        [r(0, 8.0), r(3600, 9.0)], [meal(0, 40)], UNITS, [InsulinDose(ts_utc=T0, units=4)]
    )
    assert rows and "ref" not in rows[0]

    rows = analysis.post_meal_responses(
        [r(0, 8.0), r(3600, 9.0)], [meal(0, 40)], UNITS, [InsulinDose(ts_utc=T0, units=4)],
        isf_mgdl=ISF, icr=ICR, target_mgdl=TARGET,
    )
    assert rows[0]["ref"]["suggested_units"] is not None
    assert rows[0]["ref_delta_units"] is not None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")


if __name__ == "__main__":
    _run_all()
