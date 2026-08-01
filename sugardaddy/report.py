"""`sugardaddy report` — a retrospective read of the stored timeline.

Deterministic number-crunching only: it pulls a window of readings/meals/doses
and runs the pure functions in ``analysis`` over them, emitting either a
human-readable text report or ``--json`` for a downstream consumer (e.g. an
analysis skill) to interpret. It deliberately makes no clinical judgements and
gives no advice — it reports what the data shows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sugardaddy import __version__, analysis
from sugardaddy.config import load_config
from sugardaddy.db import Database


def _tz(name: str):
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone.utc


def build_report(db: Database, cfg, days: int, now_utc: int, tzinfo) -> dict:
    """Assemble the full report dict for the window ending at ``now_utc``.

    Split out from ``run_report`` so it is testable without argparse or stdout.
    """
    start = now_utc - days * 86400
    readings = db.readings_between(start, now_utc)
    meals = db.meals_between(start, now_utc)
    doses = db.doses_between(start, now_utc)
    # IOB for a meal near the window start can draw on a dose up to a DIA earlier.
    pm_doses = db.doses_between(start - cfg.insulin.dia_minutes * 60, now_utc)

    low = cfg.target_low_mgdl
    high = cfg.target_high_mgdl
    units = cfg.web.units

    summary = analysis.summarize(readings, low, high, units)
    span = None
    if readings:
        span = {
            "first_utc": readings[0].ts_utc,
            "last_utc": readings[-1].ts_utc,
            "first_local": datetime.fromtimestamp(readings[0].ts_utc, tzinfo).isoformat(),
            "last_local": datetime.fromtimestamp(readings[-1].ts_utc, tzinfo).isoformat(),
        }

    return {
        "generated_utc": now_utc,
        "app_version": __version__,
        "window_days": days,
        "timezone": cfg.web.timezone or "UTC",
        "units": units,
        "target_low": cfg.web.target_low,
        "target_high": cfg.web.target_high,
        "reading_span": span,
        "summary": summary.as_dict(),
        "variability": analysis.variability(readings, units),
        "daily": analysis.daily_breakdown(readings, low, high, units, tzinfo),
        "hourly": analysis.hourly_profile(readings, low, high, units, tzinfo),
        "low_episodes": analysis.low_episodes(readings, low, units),
        "insulin": analysis.insulin_summary(doses),
        # What the clinician prescribed, echoed back with its date so a stale
        # entry is visible. Reported, never fed into any calculation.
        "prescription": _prescription(cfg),
        "basal_adherence": analysis.basal_adherence(
            doses,
            start,
            now_utc,
            tzinfo,
            prescribed_units=cfg.prescription.basal_units,
        ),
        "carb_coverage": analysis.carb_coverage(meals),
        "post_meal": analysis.post_meal_responses(
            readings,
            meals,
            units,
            pm_doses,
            dia_minutes=cfg.insulin.dia_minutes,
            peak_minutes=cfg.insulin.peak_minutes,
            isf_mgdl=cfg.isf_mgdl,
            icr=cfg.insulin.icr,
            target_mgdl=cfg.bolus_target_mgdl,
        ),
        # Experimental, and off unless [insulin].isf is configured — see
        # docs/plans/insulin-awareness.md Layer 4.
        "bolus_backtest": analysis.bolus_backtest(
            readings,
            doses,
            meals,
            units,
            isf_mgdl=cfg.isf_mgdl,
            icr=cfg.insulin.icr,
            target_mgdl=cfg.bolus_target_mgdl,
            dia_minutes=cfg.insulin.dia_minutes,
            peak_minutes=cfg.insulin.peak_minutes,
            history_doses=pm_doses,
        ),
        "meal_count": len(meals),
        "dose_count": len(doses),
    }


def _prescription(cfg) -> dict:
    """The [prescription] section as reported. ``configured: false`` and nothing
    else when empty, so a consumer cannot mistake blank fields for a prescription
    of nothing."""
    rx = cfg.prescription
    if not rx.configured:
        return {"configured": False}
    return {
        "configured": True,
        "reviewed": rx.reviewed,
        "basal_product": rx.basal_product,
        "basal_units": rx.basal_units,
        "basal_timing": rx.basal_timing,
        "rapid_product": rx.rapid_product,
        "icr": rx.icr,
        "isf": rx.isf,
        # Whether the experimental reference is running on the clinician's own
        # numbers or the user's placeholders. That is the difference between a
        # backtest worth quoting and one that needs hedging, so it is stated
        # rather than left to be inferred from two sections matching.
        "matches_insulin_section": (
            rx.icr is not None
            and rx.isf is not None
            and rx.icr == cfg.insulin.icr
            and rx.isf == cfg.insulin.isf
        ),
    }


def _local(ts: int, tzinfo) -> str:
    return datetime.fromtimestamp(ts, tzinfo).strftime("%Y-%m-%d %H:%M")


def _fmt_text(rep: dict, tzinfo) -> str:
    u = rep["units"]
    L = []
    L.append(f"Sugar Daddy report — last {rep['window_days']} days ({rep['timezone']}, {u})")
    L.append(f"target range {rep['target_low']}–{rep['target_high']} {u}")
    span = rep["reading_span"]
    if span:
        L.append(f"data: {span['first_local'][:16].replace('T', ' ')} → {span['last_local'][:16].replace('T', ' ')}")
    L.append("")

    s = rep["summary"]
    if not s["reading_count"]:
        L.append("No glucose readings in this window.")
        return "\n".join(L)

    v = rep["variability"]
    L.append("OVERALL")
    L.append(f"  readings      {s['reading_count']}")
    L.append(f"  average       {s['avg_display']} {u}   (est. GMI {s['gmi_percent']}%)")
    L.append(f"  time in range {s['tir_percent']}%   below {s['below_percent']}%   above {s['above_percent']}%")
    L.append(f"  variability   SD {v['sd']} {u}   CV {v['cv_percent']}%  (<=36% = stable)")
    L.append(f"  lows/highs    {s['low_count']} low readings, {s['high_count']} high readings")
    L.append("")

    L.append("PER DAY")
    L.append(f"  {'day':<11}{'n':>5}{'avg':>7}{'min':>6}{'max':>7}{'TIR%':>7}{'low%':>7}{'high%':>7}")
    for d in rep["daily"]:
        L.append(
            f"  {d['day']:<11}{d['n']:>5}{d['avg']:>7}{d['min']:>6}{d['max']:>7}"
            f"{d['tir_percent']:>7}{d['below_percent']:>7}{d['above_percent']:>7}"
        )
    L.append("")

    L.append("BY HOUR OF DAY")
    L.append(f"  {'hr':<5}{'n':>5}{'avg':>7}{'min':>6}{'max':>7}{'TIR%':>7}")
    for h in rep["hourly"]:
        L.append(
            f"  {h['hour']:02d}:00{h['n']:>5}{h['avg']:>7}{h['min']:>6}{h['max']:>7}{h['tir_percent']:>7}"
        )
    L.append("")

    eps = rep["low_episodes"]
    L.append(f"LOW EPISODES ({len(eps)})")
    if not eps:
        L.append("  none")
    for e in eps:
        L.append(
            f"  {_local(e['start_utc'], tzinfo)} → {_local(e['end_utc'], tzinfo)[11:]}"
            f"  {e['duration_min']} min, nadir {e['nadir']} {u}"
        )
    L.append("")

    ins = rep["insulin"]
    L.append(f"INSULIN — {ins['count']} doses, {ins['total_units']} u total")
    for kind, k in sorted(ins["by_kind"].items()):
        L.append(f"  {kind:<12}{k['count']:>3} doses   {k['units']} u")
    L.append("")

    L.extend(_fmt_basal(rep.get("prescription") or {}, rep.get("basal_adherence") or {}))

    cc = rep["carb_coverage"]
    L.append(f"CARB LOGGING — {cc['with_carbs']}/{cc['total']} meals have a carb count ({cc['percent']}%)")
    if cc.get("partial"):
        L.append(f"  {cc['partial']} of those are PARTIAL (some items uncarbed) — the total understates the meal")
    L.append("")

    L.append("POST-MEAL RESPONSE (start → peak → +2h; IOB@start excludes the meal bolus)")
    if not rep["post_meal"]:
        L.append("  no meals with a matching glucose window")
    for m in rep["post_meal"]:
        carbs = f"{m['carbs_g']}g" if m["carbs_g"] is not None else "  ?"
        bolus = f"{m['bolus_units']}u" if m["bolus_units"] else "  ·"
        iob = f"{m['iob_start_units']}u" if m["iob_start_units"] else "  ·"
        L.append(
            f"  {_local(m['ts_utc'], tzinfo)}  {(m['description'] or '')[:28]:<28}"
            f"  carbs {carbs:>5}  bolus {bolus:>5}  IOB {iob:>5}"
            f"  {m['start_display']} → {m['peak_display']} (+{m['minutes_to_peak']}m) → {m['end_display']}"
            f"   Δ{m['peak_delta_display']:+}"
        )

    bt = rep.get("bolus_backtest") or {}
    if bt.get("available"):
        L.append("")
        L.extend(_fmt_backtest(bt, tzinfo))
    return "\n".join(L)


def _fmt_basal(rx: dict, ad: dict) -> list[str]:
    """Basal per day, and the prescription it is measured against.

    The prescription line always carries its review date. Wording stays in the
    past tense throughout ("prescribed", "logged") — this reports what was
    recorded against what was written down, and neither half is an instruction.
    """
    if not ad.get("days"):
        return []
    L = ["BASAL BY DAY"]
    if rx.get("configured"):
        bits = []
        if rx.get("basal_product"):
            bits.append(rx["basal_product"])
        if rx.get("basal_units") is not None:
            bits.append(f"{rx['basal_units']} u")
        if rx.get("basal_timing"):
            bits.append(rx["basal_timing"])
        if bits:
            L.append(f"  prescribed: {', '.join(bits)}  (as at {rx.get('reviewed') or 'undated'})")
    else:
        L.append("  no prescription recorded — see [prescription] in the config")

    for e in ad["days"]:
        mark = "  (part day)" if e.get("partial") else ""
        tick = ""
        if e["matches_prescribed"] is True:
            tick = "  ✓"
        elif e["matches_prescribed"] is False and not e.get("partial"):
            tick = "  ✗"
        L.append(f"  {e['day']}  {e['count']} dose(s)  {e['units']} u{tick}{mark}")

    whole = ad["whole_day_count"]
    if ad["prescribed_units"] is not None:
        L.append(
            f"  {ad['days_matching_prescribed']}/{whole} whole days match the prescribed dose"
        )
    if ad["days_with_none"]:
        L.append(f"  {ad['days_with_none']} whole day(s) with no basal logged")
    if ad["days_with_multiple"]:
        L.append(f"  {ad['days_with_multiple']} whole day(s) with more than one basal logged")
    L.append("")
    return L


def _fmt_backtest(bt: dict, tzinfo) -> list[str]:
    """The experimental bolus reference, framed the way the plan doc requires:
    the calculator is what's on trial here, and its components are shown so a
    disagreement with the user's own dose is diagnosable rather than mysterious."""
    L = ["BOLUS REFERENCE — EXPERIMENTAL, not dosing advice"]
    L.append("  The calculator is what's being tested here, not your dosing. These figures")
    L.append("  come from configured ISF/ICR values and exist to be questioned, never followed.")
    L.append("  Δ is (your dose − reference): + means you gave more than the formula.")
    a = bt["agreement"]
    if not a or not a["n"]:
        L.append("  not enough scoreable doses yet")
        return L

    L.append(
        f"  agreement     n={a['n']}"
        + (f" ({a['n_full_inputs']} with every input logged)" if a["n_full_inputs"] else "")
        + f"   mean |Δ| {a['mean_abs_delta']} u   bias {a['mean_signed_delta']:+} u"
        f"   within 1 u {a['within_1u_percent']}%"
    )
    if a["n"] < 10:
        L.append("  ⚠ too few doses to read anything into these numbers yet")
    L.append("")
    incomplete = False
    for e in bt["events"]:
        ref = e["ref"]
        # A partial reference (e.g. carbs never logged) is NOT a recommendation of
        # that amount — mark it so a bare "0.0u" can't be read as "give nothing".
        partial = bool(ref["missing"])
        incomplete = incomplete or partial
        got = "—" if ref["suggested_units"] is None else f"{ref['suggested_units']}u" + ("*" if partial else "")
        delta = "  —" if e["delta_units"] is None else f"{e['delta_units']:+}" + ("*" if partial else "")
        bg = e["glucose_display"] if e["glucose_display"] is not None else "?"
        carbs = "  ?" if e["carbs_g"] is None else f"{e['carbs_g']:g}g"
        L.append(
            f"  {_local(e['ts_utc'], tzinfo)}  {e['kind']:<11}"
            f"  gave {e['actual_units']:g}u  ref {got:>6}  Δ{delta:>6}"
            f"   (bg {bg}, carbs {carbs})"
        )
        if e["ref_note"]:
            L.append(f"        {e['ref_note']}")
        if partial:
            L.append(f"        incomplete — no {', '.join(sorted(set(ref['missing'])))} for this dose")
    if incomplete:
        L.append("")
        L.append("  * incomplete inputs — a partial figure, excluded from the agreement stats.")
    return L


def run_report(
    config_path: str,
    *,
    db_path: str = "",
    days: int = 14,
    as_json: bool = False,
) -> int:
    cfg = load_config(config_path)
    db = Database(db_path or cfg.database.path)
    tzinfo = _tz(cfg.web.timezone)
    now_utc = int(datetime.now(timezone.utc).timestamp())

    rep = build_report(db, cfg, days, now_utc, tzinfo)

    if as_json:
        print(json.dumps(rep, indent=2))
    else:
        print(_fmt_text(rep, tzinfo))
    return 0
