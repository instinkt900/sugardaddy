"""CSV export and import of the logged data.

One file per kind — glucose, insulin, meals, notes — in a shape meant to be
opened in a spreadsheet as much as read back in:

- every row has ``time`` (local ISO 8601 *with* its UTC offset, so it is
  unambiguous wherever the file ends up) **and** ``ts_utc`` (epoch seconds);
- glucose carries both mmol/L and mg/dL;
- meals are one row per plate line, with the meal's own fields repeated and a
  ``meal_id`` to group them back into plates.

Import accepts what export writes, and is forgiving about what a hand-made
sheet leaves out: ``ts_utc`` wins when present, otherwise ``time`` is parsed
(a time without an offset is read in the configured timezone); ``meal_id`` may
be blank (lines are then grouped by time and meal name). Re-importing a file
is safe — rows that already exist are counted as duplicates and skipped, so an
export-then-import round trip changes nothing.

Every import is run as a dry run first by the desktop, which shows the counts
and the rejected lines before anything is written.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, tzinfo

from sugardaddy.constants import INSULIN_KINDS, MEAL_TYPES, mgdl_to_mmol, mmol_to_mgdl
from sugardaddy.db import Database
from sugardaddy.models import GlucoseReading, InsulinDose, Meal, MealItem, Note

KINDS = ("glucose", "insulin", "meals", "notes")

COLUMNS = {
    "glucose": ["time", "ts_utc", "mmol_l", "mg_dl", "trend", "source"],
    "insulin": ["time", "ts_utc", "units", "kind", "note"],
    "meals": ["meal_id", "time", "ts_utc", "meal_name", "meal_type", "meal_note",
              "item", "count", "carbs_g", "calories", "tags"],
    "notes": ["time", "ts_utc", "text"],
}


class RowError(ValueError):
    pass


def _stamp(ts: int, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")


def _num(v):
    return "" if v is None else (int(v) if float(v).is_integer() else v)


# --- export ------------------------------------------------------------------


def export_csv(db: Database, kind: str, start: int, end: int, tz: tzinfo) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(COLUMNS[kind])
    if kind == "glucose":
        for r in db.readings_between(start, end):
            w.writerow([_stamp(r.ts_utc, tz), r.ts_utc, round(mgdl_to_mmol(r.value_mgdl), 2),
                        round(r.value_mgdl, 1), "" if r.trend is None else r.trend, r.source])
    elif kind == "insulin":
        for d in db.doses_between(start, end):
            w.writerow([_stamp(d.ts_utc, tz), d.ts_utc, _num(d.units), d.kind, d.note])
    elif kind == "meals":
        for m in db.meals_between(start, end):
            for i in m.items or [MealItem(name="")]:
                w.writerow([m.id, _stamp(m.ts_utc, tz), m.ts_utc, m.name, m.meal_type, m.note,
                            i.name, _num(i.count), _num(i.carbs_g), _num(i.calories), i.tags])
    elif kind == "notes":
        for n in db.notes_between(start, end):
            w.writerow([_stamp(n.ts_utc, tz), n.ts_utc, n.text])
    else:
        raise ValueError(f"unknown kind {kind!r}")
    return out.getvalue()


# --- import ------------------------------------------------------------------


def _ts(row: dict, tz: tzinfo) -> int:
    raw = (row.get("ts_utc") or "").strip()
    if raw:
        try:
            return int(float(raw))
        except ValueError:
            raise RowError(f"ts_utc {raw!r} is not a number")
    t = (row.get("time") or "").strip()
    if not t:
        raise RowError("needs a time or ts_utc")
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        raise RowError(f"time {t!r} is not ISO 8601 (e.g. 2026-10-20T14:42:00+11:00)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


def _opt_float(row: dict, col: str):
    v = (row.get(col) or "").strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        raise RowError(f"{col} {v!r} is not a number")


def import_csv(db: Database, kind: str, text: str, tz: tzinfo, *, dry_run: bool = True) -> dict:
    """Parse ``text`` and (unless ``dry_run``) store the new rows.

    Returns ``{"rows", "added", "duplicates", "errors": [{"line", "error"}]}``.
    A row with an error is skipped and reported; the rest still go in — the
    dry run is where a bad file is caught, not here."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows, errors, total = [], [], 0
    for line, row in enumerate(reader, start=2):  # line 1 is the header
        total += 1
        row = {(k or "").strip().lower(): (v or "") for k, v in row.items()}
        try:
            rows.append((line, _ts(row, tz), row))
        except RowError as exc:
            errors.append({"line": line, "error": str(exc)})
    handler = {"glucose": _glucose, "insulin": _insulin, "meals": _meals, "notes": _notes}[kind]
    added, dups = handler(db, rows, errors, dry_run)
    return {"rows": total, "added": added,
            "duplicates": dups, "errors": errors[:50], "error_count": len(errors), "dry_run": dry_run}


def _span(rows):
    ts = [t for _, t, _ in rows]
    return (min(ts), max(ts)) if ts else (0, 0)


def _glucose(db, rows, errors, dry_run):
    readings = []
    for line, ts, row in rows:
        try:
            mg = _opt_float(row, "mg_dl")
            mmol = _opt_float(row, "mmol_l")
            if mg is None and mmol is None:
                raise RowError("needs mg_dl or mmol_l")
            value = mg if mg is not None else mmol_to_mgdl(mmol)
            trend = _opt_float(row, "trend")
            readings.append(GlucoseReading(
                ts_utc=ts, value_mgdl=value, trend=None if trend is None else int(trend),
                source=(row.get("source") or "csv").strip() or "csv"))
        except RowError as exc:
            errors.append({"line": line, "error": str(exc)})
    lo, hi = _span(rows)
    have = {r.ts_utc for r in db.readings_between(lo, hi)} if rows else set()
    seen, new = set(), []
    for r in readings:
        if r.ts_utc in have or r.ts_utc in seen:
            continue
        seen.add(r.ts_utc)
        new.append(r)
    if not dry_run and new:
        db.add_readings(new)
    return len(new), len(readings) - len(new)


def _insulin(db, rows, errors, dry_run):
    lo, hi = _span(rows)
    have = {(d.ts_utc, round(d.units, 2), d.kind) for d in db.doses_between(lo, hi)} if rows else set()
    added = dups = 0
    for line, ts, row in rows:
        try:
            units = _opt_float(row, "units")
            if not units or units <= 0:
                raise RowError("units must be a number above 0")
            kind = (row.get("kind") or "bolus").strip().lower()
            if kind not in INSULIN_KINDS:
                raise RowError(f"kind must be one of {', '.join(INSULIN_KINDS)}")
        except RowError as exc:
            errors.append({"line": line, "error": str(exc)})
            continue
        key = (ts, round(units, 2), kind)
        if key in have:
            dups += 1
            continue
        have.add(key)
        added += 1
        if not dry_run:
            db.add_dose(InsulinDose(ts_utc=ts, units=units, kind=kind, note=(row.get("note") or "").strip()))
    return added, dups


def _notes(db, rows, errors, dry_run):
    lo, hi = _span(rows)
    have = {(n.ts_utc, n.text.strip()) for n in db.notes_between(lo, hi)} if rows else set()
    added = dups = 0
    for line, ts, row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            errors.append({"line": line, "error": "text is empty"})
            continue
        if (ts, text) in have:
            dups += 1
            continue
        have.add((ts, text))
        added += 1
        if not dry_run:
            db.add_note(Note(ts_utc=ts, text=text))
    return added, dups


def _meals(db, rows, errors, dry_run):
    """Group lines into plates (by ``meal_id``, else by time + name), then skip
    any plate already logged at that moment with the same items."""
    plates: dict[tuple, dict] = {}
    for line, ts, row in rows:
        try:
            mid = (row.get("meal_id") or "").strip()
            key = ("id", mid) if mid else ("tn", ts, (row.get("meal_name") or "").strip().lower())
            p = plates.setdefault(key, {"ts": ts, "row": row, "items": [], "line": line})
            name = (row.get("item") or "").strip()
            if name:
                p["items"].append(MealItem(
                    name=name, count=_opt_float(row, "count") or 1,
                    carbs_g=_opt_float(row, "carbs_g"), calories=_opt_float(row, "calories"),
                    tags=(row.get("tags") or "").strip()))
        except RowError as exc:
            errors.append({"line": line, "error": str(exc)})
    lo, hi = _span(rows)
    have = {
        (m.ts_utc, tuple(sorted(i.name.lower() for i in m.items)))
        for m in db.meals_between(lo, hi)
    } if rows else set()
    added = dups = 0
    for p in plates.values():
        if not p["items"]:
            errors.append({"line": p["line"], "error": "a meal needs at least one item"})
            continue
        sig = (p["ts"], tuple(sorted(i.name.lower() for i in p["items"])))
        if sig in have:
            dups += 1
            continue
        have.add(sig)
        added += 1
        if not dry_run:
            r = p["row"]
            mtype = (r.get("meal_type") or "").strip().lower()
            db.add_meal(Meal(
                ts_utc=p["ts"], name=(r.get("meal_name") or "").strip(),
                meal_type=mtype if mtype in MEAL_TYPES else "",
                note=(r.get("meal_note") or "").strip(), items=p["items"]))
    return added, dups
