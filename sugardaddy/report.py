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
        "carb_coverage": analysis.carb_coverage(meals),
        "post_meal": analysis.post_meal_responses(readings, meals, units),
        "meal_count": len(meals),
        "dose_count": len(doses),
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

    cc = rep["carb_coverage"]
    L.append(f"CARB LOGGING — {cc['with_carbs']}/{cc['total']} meals have a carb count ({cc['percent']}%)")
    L.append("")

    L.append("POST-MEAL RESPONSE (start → peak → +2h)")
    if not rep["post_meal"]:
        L.append("  no meals with a matching glucose window")
    for m in rep["post_meal"]:
        carbs = f"{m['carbs_g']}g" if m["carbs_g"] is not None else "  ?"
        L.append(
            f"  {_local(m['ts_utc'], tzinfo)}  {(m['description'] or '')[:28]:<28}"
            f"  carbs {carbs:>5}  {m['start_display']} → {m['peak_display']} (+{m['minutes_to_peak']}m) → {m['end_display']}"
            f"   Δ{m['peak_delta_display']:+}"
        )
    return "\n".join(L)


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
