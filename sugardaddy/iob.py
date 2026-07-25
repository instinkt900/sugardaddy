"""Insulin-on-board maths — how much of a past rapid-acting dose is still working.

Pure arithmetic over logged doses (no I/O, no clock, no config), mirroring
``analysis.py`` so it is trivially testable and shared by the web/report layers.
This is Layer 1 of docs/plans/insulin-awareness.md: **informational only** — it
describes insulin already given, it never suggests a dose.

Model is the oref0/Loop exponential activity curve (Dragan Maksimović): a closed
form fixed by two user/clinician-owned parameters, DIA and time-to-peak. Only
**rapid-acting** insulin counts — long-acting basal is a separate ~flat depot and
folding it in would badly inflate the number, so `kind == "basal"` is excluded.
"""

from __future__ import annotations

import math

from sugardaddy.models import InsulinDose

# Sensible rapid-analog defaults (aspart/NovoRapid/lispro). The real values are
# user/clinician-owned and come from config; these only keep the maths usable
# out of the box. See [insulin] in config.example.toml.
DEFAULT_DIA_MINUTES = 300
DEFAULT_PEAK_MINUTES = 75

# Doses that contribute to rapid-acting IOB. Basal is a separate depot.
RAPID_KINDS = ("bolus", "correction")


def is_rapid(dose: InsulinDose) -> bool:
    """Whether a dose counts toward rapid-acting IOB (bolus/correction, not basal).
    An unset/empty kind is treated as a bolus, matching insulin_summary()."""
    return (dose.kind or "bolus") in RAPID_KINDS


def iob_fraction(minutes: float, dia_minutes: float, peak_minutes: float) -> float:
    """Fraction of one unit still active ``minutes`` after injection (1.0 at the
    dose → 0 at DIA), per the exponential curve. Clamped outside [0, DIA]."""
    if minutes <= 0:
        return 1.0
    if minutes >= dia_minutes:
        return 0.0

    td = float(dia_minutes)
    tp = float(peak_minutes)
    t = float(minutes)

    tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
    a = 2 * tau / td
    s = 1 / (1 - a + (1 + a) * math.exp(-td / tau))

    return 1 - s * (1 - a) * (
        (t * t / (tau * td * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1
    )


def active_iob(
    doses: list[InsulinDose],
    at_ts: int,
    *,
    dia_minutes: float = DEFAULT_DIA_MINUTES,
    peak_minutes: float = DEFAULT_PEAK_MINUTES,
) -> float:
    """Active rapid-acting units at ``at_ts`` = Σ units × iob_fraction(elapsed).
    Only doses at or before ``at_ts`` and within the action window contribute."""
    total = 0.0
    for d in doses:
        if not is_rapid(d):
            continue
        minutes = (at_ts - d.ts_utc) / 60
        if minutes < 0 or minutes >= dia_minutes:
            continue
        total += d.units * iob_fraction(minutes, dia_minutes, peak_minutes)
    return total
