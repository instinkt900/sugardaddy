"""Experimental bolus *reference* — a figure to argue with, never a dose to take.

This is Layer 4 of docs/plans/insulin-awareness.md, and it is the one part of the
app that deliberately crosses the "not a medical device" line for personal,
retrospective use. Read the safety boundary there before extending this module.
Three rules shape everything here:

1. **It is a cross-check, not a directive.** The point is the *reconciliation* —
   "it says 12 u, I feel 6, where's the disconnect?" A number the user obeys
   blindly would be worse than none, so this never returns a bare figure: it
   returns the components, and callers are expected to show them.
2. **Parameters are user/clinician-owned.** ISF and ICR are never inferred or
   auto-tuned from history (too noisy — see the plan doc). Unset means the
   reference is *unavailable*, not guessed: a confident number on a shaky ISF is
   worse than no number, so we gate hard and say which input is missing.
3. **Pure arithmetic**, like ``iob.py`` and ``analysis.py`` — no I/O, no clock,
   no config lookups — so it is trivially testable and shared by report and web.

Everything is in mg/dL internally (the storage unit); callers convert ISF and
target from the display unit at the edges, as elsewhere in the app.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BolusReference:
    """A calculated reference dose *and the components that produced it*.

    ``suggested_units`` is ``None`` whenever a required input is missing — the
    reason lands in ``missing`` so the caller can say "no ISF configured" rather
    than silently showing a number that only covers half the picture."""

    suggested_units: float | None
    carb_units: float | None        # carbs / ICR
    correction_units: float | None  # (glucose - target) / ISF; negative when below target
    iob_units: float                # already-active insulin, subtracted (anti-stacking)
    missing: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.suggested_units is not None

    def as_dict(self) -> dict:
        return {
            "suggested_units": self.suggested_units,
            "carb_units": self.carb_units,
            "correction_units": self.correction_units,
            "iob_units": self.iob_units,
            "missing": list(self.missing),
        }


def bolus_reference(
    *,
    bg_mgdl: float | None,
    target_mgdl: float | None,
    isf_mgdl_per_unit: float | None,
    icr_g_per_unit: float | None,
    carbs_g: float | None,
    iob_units: float = 0.0,
) -> BolusReference:
    """The standard open-loop formula: carbs/ICR + (glucose-target)/ISF - IOB.

    The **IOB subtraction is the anti-stacking guard** — the single most useful
    part of the calculation, and the one a tired human most often skips. Each
    component degrades independently: no carbs logged still gives a correction
    figure, and a dose below target yields a *negative* correction that eats into
    the carb cover, which is the intended behaviour.

    The total is floored at zero (insulin cannot be un-given) but is **not**
    rounded to a syringe increment — it stays at 1 dp so it reads as a computed
    reference to compare against, not a prescription to dial up.
    """
    missing: list[str] = []

    # Carb cover. Needs both a logged carb count and a configured ratio; carbs of
    # 0 is a real answer (a correction-only dose), None means "never logged".
    carb_units = None
    if icr_g_per_unit is None or icr_g_per_unit <= 0:
        missing.append("icr")
    elif carbs_g is None:
        missing.append("carbs")
    else:
        carb_units = carbs_g / icr_g_per_unit

    # Correction toward target. Signed on purpose.
    correction_units = None
    if isf_mgdl_per_unit is None or isf_mgdl_per_unit <= 0:
        missing.append("isf")
    elif bg_mgdl is None:
        missing.append("glucose")
    elif target_mgdl is None:
        missing.append("target")
    else:
        correction_units = (bg_mgdl - target_mgdl) / isf_mgdl_per_unit

    if carb_units is None and correction_units is None:
        # Nothing computable — report why rather than implying zero.
        return BolusReference(None, None, None, round(iob_units, 2), missing)

    total = (carb_units or 0.0) + (correction_units or 0.0) - iob_units
    return BolusReference(
        suggested_units=round(max(total, 0.0), 1),
        carb_units=None if carb_units is None else round(carb_units, 2),
        correction_units=None if correction_units is None else round(correction_units, 2),
        iob_units=round(iob_units, 2),
        missing=missing,
    )


def describe(ref: BolusReference, units: str = "u") -> str:
    """One-line component breakdown, so a disagreement with the user's own call is
    *diagnosable* at a glance ("the carb half is what's driving this, not the
    correction"). Empty string when nothing was computable."""
    if not ref.available:
        return ""
    bits = []
    if ref.carb_units is not None:
        bits.append(f"{ref.carb_units:g}{units} carbs")
    if ref.correction_units is not None:
        bits.append(f"{ref.correction_units:+g}{units} correction")
    if ref.iob_units:
        bits.append(f"-{ref.iob_units:g}{units} active")
    return " · ".join(bits)
