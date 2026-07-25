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


def activity_fraction(minutes: float, dia_minutes: float, peak_minutes: float) -> float:
    """Fraction of one unit *acting per minute* at ``minutes`` after the dose — the
    rate of insulin action, i.e. the (negative) derivative of the IOB curve. A bell
    that peaks near ``peak_minutes``; this is what pushes glucose down at time t,
    where iob_fraction is only how much remains on board. Clamped outside [0, DIA]."""
    if minutes <= 0 or minutes >= dia_minutes:
        return 0.0

    td = float(dia_minutes)
    tp = float(peak_minutes)
    t = float(minutes)

    tau = tp * (1 - tp / td) / (1 - 2 * tp / td)
    a = 2 * tau / td
    s = 1 / (1 - a + (1 + a) * math.exp(-td / tau))

    return (s / (tau * tau)) * t * (1 - t / td) * math.exp(-t / tau)


def _sum_over_active(doses, at_ts, dia_minutes, peak_minutes, frac):
    """Σ dose_units × frac(elapsed) over rapid-acting doses at/or before ``at_ts``
    within the action window. ``frac`` is iob_fraction or activity_fraction."""
    total = 0.0
    for d in doses:
        if not is_rapid(d):
            continue
        minutes = (at_ts - d.ts_utc) / 60
        if minutes < 0 or minutes >= dia_minutes:
            continue
        total += d.units * frac(minutes, dia_minutes, peak_minutes)
    return total


def active_iob(
    doses: list[InsulinDose],
    at_ts: int,
    *,
    dia_minutes: float = DEFAULT_DIA_MINUTES,
    peak_minutes: float = DEFAULT_PEAK_MINUTES,
) -> float:
    """Active rapid-acting units at ``at_ts`` = Σ units × iob_fraction(elapsed).
    Only doses at or before ``at_ts`` and within the action window contribute."""
    return _sum_over_active(doses, at_ts, dia_minutes, peak_minutes, iob_fraction)


def active_activity(
    doses: list[InsulinDose],
    at_ts: int,
    *,
    dia_minutes: float = DEFAULT_DIA_MINUTES,
    peak_minutes: float = DEFAULT_PEAK_MINUTES,
) -> float:
    """Aggregate rapid-acting insulin action rate at ``at_ts``, in units-per-minute
    (Σ units × activity_fraction). Multiply by 60 for units-per-hour."""
    return _sum_over_active(doses, at_ts, dia_minutes, peak_minutes, activity_fraction)


def activity_phase(
    doses: list[InsulinDose],
    at_ts: int,
    *,
    dia_minutes: float = DEFAULT_DIA_MINUTES,
    peak_minutes: float = DEFAULT_PEAK_MINUTES,
    step: int = 300,
) -> tuple[float, int] | None:
    """Where the current insulin action sits "on the ride" — magnitude-agnostic.

    Returns ``(fraction, direction)`` where ``fraction`` is the current aggregate
    action rate as a share (0..1) of the *peak* rate this active-dose batch reaches
    over its whole lifespan (looking ahead, so a still-upcoming peak counts), and
    ``direction`` is ``+1`` rising, ``-1`` falling, ``0`` at/near the peak. Returns
    ``None`` when no rapid-acting insulin is meaningfully active. Lets a compact UI
    say "70% rising" (climb still ahead) vs "20% falling" (on the tail)."""
    active = [d for d in doses if is_rapid(d) and 0 <= at_ts - d.ts_utc < dia_minutes * 60]
    if not active:
        return None

    def rate(t: int) -> float:
        return active_activity(active, t, dia_minutes=dia_minutes, peak_minutes=peak_minutes)

    now_rate = rate(at_ts)
    if now_rate <= 0:
        # Just dosed: on board but action not yet started → 0% and rising.
        return (0.0, 1) if rate(at_ts + step) > 0 else None

    # Peak of the batch's action curve across its full span (past or upcoming).
    t0 = min(d.ts_utc for d in active)
    t1 = max(d.ts_utc for d in active) + int(dia_minutes * 60)
    peak_rate = now_rate
    t = t0
    while t <= t1:
        peak_rate = max(peak_rate, rate(t))
        t += step

    fraction = now_rate / peak_rate if peak_rate > 0 else 0.0
    slope = rate(at_ts + step) - rate(at_ts - step)
    eps = peak_rate * 0.01
    direction = 1 if slope > eps else (-1 if slope < -eps else 0)
    return (fraction, direction)
