# Plan: insulin-on-board & dosing-awareness guidance

**Status:** planning only — nothing here is built. Captured so the direction and
reasoning aren't lost.

> **Not a medical device, not dosing advice.** Everything below surfaces
> *information* the user already implicitly reasons about (trajectory, insulin
> still working, recent lows). The user makes every dosing decision. Any feature
> that edges toward suggesting an amount is explicitly gated and labelled (see
> [Safety boundary](#safety-boundary)).

---

## Motivation — de-vibe the dose

The main goal is to **calculate something concrete to check a dose against**, so
the decision isn't made purely on vibes.

The user already does the right reasoning by hand: "glucose is already dropping,
I'll take a bit less" / "it's high and still rising, I'll take a bit more." But
the human mind runs on vibes — when tired, sick, busy, or distracted, that
scrutiny gets skipped and the dose goes in on autopilot. Occasionally that
autopilot is wrong (see the stacked-correction low below).

The fix is an **external anchor**: a figure the app calculates, sitting right
where insulin is logged, that the user reconciles against their own intuition.
The de-vibe effect comes from the *reconciliation*, not from trusting the number:

> "The calculator says 12 u but I feel like I only need 6 — let me look at the
> situation and find where the disconnect is."

That moment of "why do we disagree?" is the whole point. The number exists to be
**questioned**, and the act of questioning it forces the review that vibes-mode
skips. A calculator the user obeys blindly would be *worse* than none; one they
argue with is the goal.

Two complementary anchors, same purpose — give the gut something concrete to
argue with:

1. **Awareness nudge** (ships first; lightweight, always-on). A glanceable prompt
   that surfaces the current trajectory — *"you may be more sensitive right now"*
   / *"you're running high and still climbing"* — plus active IOB. Cheap and safe:
   no assumptions about personal ratios, and it catches the eye only when the
   situation is notable.
2. **Calculated reference / bolus figure** (stretch goal, experimental — see
   [Layer 4](#layer-4--bolus-calculator--calculated-reference-stretch-goal-experimental)).
   A richer, IOB-aware suggested amount to reconcile against. Built as much for
   academic interest and the fun of seeing how accurate it can get as for daily
   use — and genuinely useful as an extra data point for judgement calls, **never**
   as a directive.

Both are the same idea at different fidelity. The awareness nudge is the reliable
first deliverable; the full calculator is a thing to build, tune, and play with as
the data justifies it.

---

## Background concepts

### Insulin on Board (IOB) & activity curves

IOB models how much of a past rapid-acting dose is still working. It needs an
**activity curve** defined by two parameters:

- **DIA** — Duration of Insulin Action (~4–6 h for rapid analogs).
- **tp** — time to peak activity (~65–75 min for aspart/NovoRapid/lispro;
  ~55 min for Fiasp).

The realistic model is the oref0/Loop **exponential curve** (Dragan Maksimović),
a closed form — no fitting once DIA and tp are chosen:

```
tau = tp * (1 - tp/td) / (1 - 2*tp/td)
a   = 2 * tau / td
S   = 1 / (1 - a + (1 + a) * exp(-td/tau))

IOB(t) = 1 - S*(1-a) * ( (t^2/(tau*td*(1-a)) - t/tau - 1) * exp(-t/tau) + 1 )
```

`IOB(t)` = fraction of one unit still active `t` minutes after the dose
(1.0 at injection → 0 at DIA). Current active insulin =
`Σ dose_units × IOB(now − dose_time)` over rapid-acting doses in the last DIA.

A trivial linear model (`IOB = 1 − t/DIA`) exists but overstates early activity;
keep it only as a transparent fallback/toggle.

**Basal is excluded from rapid-acting IOB.** Long-acting basal (e.g. the ~36 u
once-daily dose) is a separate, roughly-flat ~24 h depot; folding it into
rapid-acting IOB would badly inflate the number. The `insulin_doses.kind` column
(`bolus` | `correction` | `basal`) already lets us include only `bolus` +
`correction`.

### "Strength": ISF and ICR

- **ISF (Insulin Sensitivity Factor / correction factor)** — how far 1 u drops
  glucose. This is the "strength" number.
- **ICR (insulin-to-carb ratio)** — grams of carb 1 u covers.

**Can ISF be inferred from logged data?** In principle: isolate *clean* correction
events (little/no prior IOB, no meal within the action window, stable start),
measure ΔBG over ~DIA, and take `ISF ≈ ΔBG / units`, averaged over several.

In practice, with current data, no — too noisy:
- corrections are mostly **stacked on meal boluses** (food + insulin both moving);
- **dawn rise, meals, activity** all confound the delta;
- too few isolated events to average.

Decision: **do not derive ISF from history.** Let the user enter their
clinician-given ISF/ICR, and use history only to **retrospectively sanity-check**
it. ICR inference is blocked entirely until carb logging improves (currently ~1
in 13 meals carry carbs).

---

## Feature layers (roadmap, ordered by safety + feasibility)

### Layer 0 — prerequisites
- **Config parameters** (per user, in `config.toml`, not inferred): `dia_minutes`,
  `peak_minutes`, optional `isf` and `icr`, plus the existing target range.
  Sensible defaults, but the user/clinician owns the values.
- **More/better data**: carb logging discipline (unlocks ICR + richer meal
  analysis); a longer history than the current few days before any inference is
  trustworthy.

### Layer 1 — IOB engine + display (deterministic, safe)
- An `iob` module (mirroring `analysis.py`'s pure-function style): given doses +
  DIA/tp + a time, return active units and a per-dose breakdown.
- Show current active insulin on the phone logger and/or desktop
  ("≈ 3.2 u active, from 2 doses in the last 4 h").
- **Validate against known events first**: backtest the IOB timeline against the
  midnight low we already found — confirm high stacked IOB lines up with the dip.

### Layer 2 — dosing-awareness nudge (the priority)
A glanceable status on the insulin log form, computed from data we already
collect. **Observations only, no numbers to change, no suggested dose.**

Signals available now:
- **current glucose** vs target band;
- **rate of change** — computed from the last ~15–30 min of readings (mmol/L per
  15 min), finer than the 5-level Libre trend arrow (`trend` is stored too);
- **active IOB** (from Layer 1);
- **recent hypo** — any low in the last N hours;
- (later) time-of-day context, e.g. the known dawn window.

Nudge mapping (plain language, "pause and look" framing):
- falling fast **or** recent low **or** meaningful IOB →
  *"You may be more sensitive to insulin right now — glucose is already
  dropping / you still have insulin active / you had a low recently."*
- high **and** rising **and** little IOB →
  *"You're running high and still climbing — worth a close look before dosing."*
- in range and flat → **stay quiet** (no message). Silence when unremarkable is
  what preserves the eye-catch when it matters.

Design rules:
- Never emit a dose delta ("take 1 u less"); only surface the situation.
- Must be quiet by default; only appears when the trajectory deviates.
- Colour/emphasis tuned to catch the eye without being alarmist.

### Layer 3 — retrospective ISF/ICR sanity-check (in `report`)
- On the cleanest correction events, report the implied "1 u moved you ~X" and
  compare against the user's configured ISF. Framed as a check, not a source.
- Once carb coverage is high enough, do the same for ICR from meal boluses.

### Layer 4 — bolus calculator / calculated reference (stretch goal, experimental)
A genuine build goal, not a throwaway — pursued for academic interest and the fun
of tuning it, and doubling as the richer anchor from the motivation. It is **not**
a dosing directive: the user is confident with their own ranges and wants the
number as something to *reconcile against*, per the "says 12, I feel 6" example.
- Combine current BG, target, ISF, ICR, carbs, **minus IOB** (the IOB subtraction
  is the anti-stacking guard).
- Show the calculated figure **and its components** ("≈ 3.2 u active · ~4 above
  target at your set ISF · N g carbs") so a disagreement is *diagnosable* — the
  user can see which input drives the gap between the number and their gut.
- Design it to **invite disagreement**: surface the reasoning, make it easy to see
  why the number is what it is. Never present a single authoritative dose.
- Clearly labelled experimental / a cross-check, not a guide; opt-in, not the
  default view.
- Accuracy tracks data: gate on configured ISF/ICR and enough clean history, and
  show it as rough (and say so) until then. Prototyping it early for fun is fine;
  wiring it into the daily logging flow waits for the data to justify it.

---

## Safety boundary

- **Layers 1–2 are informational**: IOB is arithmetic over logged doses; the
  nudge restates trajectory + recent context. Neither suggests an amount.
- **A bolus calculator (Layer 4) is a regulated medical-device function** — it's
  exactly what the app's "not a medical device" stance carves out. Building it,
  even for personal use, crosses that line deliberately, not by accident. It is an
  experiment and a cross-check, never a directive: its design goal is to be
  *questioned* — it shows its components so a disconnect with the user's judgement
  is diagnosable, and the user's own call always wins. Keep it opt-in and labelled
  experimental; a confident number on a shaky ISF is worse than no number.
- **Parameters (DIA, tp, ISF, ICR, target) are user/clinician-owned.** The app
  never silently picks or auto-tunes them; a confident number on a shaky ISF is
  worse than no number.
- Guidance quality tracks data quality — gate ISF/ICR features on enough clean
  events / carb coverage, and say so rather than presenting a shaky figure as
  solid.

---

## Data model & implementation notes

Already present:
- `insulin_doses` with `kind` (bolus/correction/basal), `units`, `ts_utc` — enough
  for IOB with basal excluded.
- `glucose_readings` (~1/min live) with `trend` — enough for computed rate of
  change and recent-low detection.
- `analysis.py` pure-function pattern + the `report` command as the reusable,
  testable home for new deterministic maths.

To add:
- Config: `dia_minutes`, `peak_minutes`, `isf`, `icr` (all optional; defaults +
  validation via the existing `_known()` mechanism).
- `iob` module: pure functions (`iob_at(doses, t, dia, tp)`, activity curve,
  per-dose breakdown) with plain-assert tests like `tests/test_report_analysis.py`.
- A small "current situation" helper feeding both the logger nudge (live) and the
  `report` (retrospective validation).

## Validation approach
- Backtest IOB against the known stacked-correction low before trusting the curve.
- Require a minimum count of clean correction events before showing any
  ISF-derived figure; otherwise report "not enough clean events yet."
- Treat the nudge like the review's talking points: anchored to real signals,
  declinable, never prescriptive.

## Open questions
- MDI (multiple daily injections) vs pump assumptions — IOB is pump-native; verify
  the exponential curve is a reasonable fit for the user's specific rapid-acting
  insulin, and expose DIA/tp so it can be tuned.
- Rate-of-change window and thresholds for the nudge (needs tuning against real
  data once there's more of it).
- Where the nudge lives in the UI and how it stays glanceable without nagging.
