"""Optional export of the timeline to VictoriaMetrics, for Grafana.

Off unless ``[victoriametrics] enabled = true``. Each pass syncs the database to
VictoriaMetrics over its InfluxDB line-protocol endpoint (``/write``), stamping
every sample with the time the event happened rather than the time it was sent:

    sugardaddy_glucose_mgdl / _mmol / _trend   one sample per stored reading
    sugardaddy_insulin_units{kind,id}          one sample per logged dose
    sugardaddy_meal_*{meal_type,id}            logged, items, items_without_carbs,
                                               carbs_g / calories (when any item has one)

All series carry ``db="sugardaddy"``. The first pass sends the whole history, so
the backfill is just a sync that starts from nothing; ``sugardaddy export --full``
forgets what was sent and does it again.

Glucose rows are append-only, so a row-id cursor is enough. Doses and meals can
be edited or deleted, and VictoriaMetrics cannot change a single sample, so each
one gets its own series (the ``id`` label): a pass compares every event with a
fingerprint of what it last sent, deletes the series of anything changed or gone,
and writes the new version. That catches edits from any route without the web
layer having to know the export exists.

A failed pass changes no state, so the next one retries. Nothing here can stop
ingestion or logging — the loop logs and carries on, like the ingest loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

import httpx

from sugardaddy.config import Config, load_config
from sugardaddy.constants import mgdl_to_mmol
from sugardaddy.db import Database
from sugardaddy.models import InsulinDose, Meal

log = logging.getLogger("sugardaddy.export")
# httpx logs every request at INFO, which is a line a minute from this loop alone.
# A pass that sent something logs its own summary; failures still surface as warnings.
logging.getLogger("httpx").setLevel(logging.WARNING)

DB_LABEL = "sugardaddy"
BATCH = 5000  # lines per request; a full backfill is ~1.5k readings a day
_GLUCOSE_CURSOR = "export.glucose_id"
_EVENTS_SENT = "export.events"
_FOREVER = 2**62


class ExportError(RuntimeError):
    pass


# --- line protocol (pure) --------------------------------------------------


def _tag(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def _line(measurement: str, tags: dict[str, str], fields: dict[str, float], ts: int) -> str:
    tag_part = "".join(f",{k}={_tag(v)}" for k, v in sorted(tags.items()) if v)
    field_part = ",".join(f"{k}={v:g}" for k, v in fields.items())
    return f"{measurement}{tag_part} {field_part} {ts}"


def glucose_line(ts_utc: int, value_mgdl: float, trend: int | None) -> str:
    fields = {"mgdl": value_mgdl, "mmol": round(mgdl_to_mmol(value_mgdl), 2)}
    if trend is not None:
        fields["trend"] = trend
    return _line("sugardaddy_glucose", {}, fields, ts_utc)


def dose_line(d: InsulinDose) -> str:
    return _line("sugardaddy_insulin", {"id": str(d.id), "kind": d.kind}, {"units": d.units}, d.ts_utc)


def meal_line(m: Meal) -> str:
    fields: dict[str, float] = {
        "logged": 1,
        "items": len(m.items),
        "items_without_carbs": sum(1 for i in m.items if i.carbs_g is None),
    }
    # total_carbs sums the items that have a count, so with items_without_carbs > 0
    # it is a floor, not the plate — the same reading the post-meal table gives it.
    if m.total_carbs is not None:
        fields["carbs_g"] = m.total_carbs
    if m.total_calories is not None:
        fields["calories"] = m.total_calories
    return _line("sugardaddy_meal", {"id": str(m.id), "meal_type": m.meal_type}, fields, m.ts_utc)


def event_lines(doses: list[InsulinDose], meals: list[Meal]) -> dict[str, str]:
    """Every dose and meal as ``{key: line}``. The key names the event's series,
    and the line doubles as its fingerprint: any edit changes it."""
    lines = {f"insulin:{d.id}": dose_line(d) for d in doses}
    lines.update({f"meal:{m.id}": meal_line(m) for m in meals})
    return lines


def diff_events(sent: dict[str, str], current: dict[str, str]) -> tuple[list[str], list[str]]:
    """What a pass must do: (series to delete, keys to write).

    A changed event is both — its old series goes before the new line is written,
    because a dose whose kind changed would otherwise live on under the old label."""
    current_fp = {k: _fingerprint(v) for k, v in current.items()}
    stale = [k for k, fp in sent.items() if current_fp.get(k) != fp]
    write = [k for k, fp in current_fp.items() if sent.get(k) != fp]
    return stale, write


def _fingerprint(line: str) -> str:
    return hashlib.sha1(line.encode()).hexdigest()[:16]


def _series_match(key: str) -> str:
    kind, event_id = key.split(":", 1)
    measurement = {"insulin": "sugardaddy_insulin", "meal": "sugardaddy_meal"}[kind]
    return f'{{__name__=~"{measurement}_.*",db="{DB_LABEL}",id="{event_id}"}}'


# --- network ---------------------------------------------------------------


class VictoriaMetrics:
    def __init__(self, url: str, timeout: float):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def write(self, lines: list[str]) -> None:
        for start in range(0, len(lines), BATCH):
            body = "\n".join(lines[start : start + BATCH]) + "\n"
            self._request(
                "POST", "/write", params={"db": DB_LABEL, "precision": "s"}, content=body
            )

    def delete_series(self, match: str) -> None:
        self._request("POST", "/api/v1/admin/tsdb/delete_series", data={"match[]": match})

    def _request(self, method: str, path: str, **kwargs) -> None:
        try:
            resp = httpx.request(method, self.url + path, timeout=self.timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise ExportError(f"{path}: {exc}") from exc
        if resp.status_code >= 300:
            raise ExportError(f"{path}: HTTP {resp.status_code} {resp.text[:200]}")


# --- sync ------------------------------------------------------------------


def sync(db: Database, vm: VictoriaMetrics) -> dict:
    """One pass: new glucose readings, then dose/meal changes. Returns counts."""
    glucose = _sync_glucose(db, vm)
    deleted, written = _sync_events(db, vm)
    return {"glucose": glucose, "events_deleted": deleted, "events_written": written}


def _sync_glucose(db: Database, vm: VictoriaMetrics) -> int:
    cursor = int(db.get_state(_GLUCOSE_CURSOR) or 0)
    sent = 0
    while True:
        readings = db.readings_after_id(cursor, BATCH)
        if not readings:
            return sent
        vm.write([glucose_line(r.ts_utc, r.value_mgdl, r.trend) for r in readings])
        cursor = readings[-1].id
        db.set_state(_GLUCOSE_CURSOR, str(cursor))
        sent += len(readings)


def _sync_events(db: Database, vm: VictoriaMetrics) -> tuple[int, int]:
    sent: dict[str, str] = json.loads(db.get_state(_EVENTS_SENT) or "{}")
    current = event_lines(db.doses_between(0, _FOREVER), db.meals_between(0, _FOREVER))
    stale, write = diff_events(sent, current)
    if not stale and not write:
        return 0, 0
    for key in stale:
        vm.delete_series(_series_match(key))
        sent.pop(key)
    vm.write([current[k] for k in write])
    sent.update({k: _fingerprint(current[k]) for k in write})
    db.set_state(_EVENTS_SENT, json.dumps(sent, sort_keys=True))
    return len(stale), len(write)


def reset(db: Database, vm: VictoriaMetrics) -> None:
    """Delete every exported series and forget what was sent, so the next pass
    sends the whole history again. Deleting first matters: a resent sample would
    otherwise sit beside the old one, and a since-deleted event would linger."""
    vm.delete_series(f'{{__name__=~"sugardaddy_.*",db="{DB_LABEL}"}}')
    db.set_state(_GLUCOSE_CURSOR, "0")
    db.set_state(_EVENTS_SENT, "{}")


def export_loop(cfg: Config, db: Database, stop: threading.Event | None = None) -> None:
    """Sync on an interval until `stop` is set (or forever if None)."""
    vm = VictoriaMetrics(cfg.victoriametrics.url, cfg.victoriametrics.timeout_seconds)
    interval = max(15, cfg.victoriametrics.interval_seconds)

    while True:
        try:
            result = sync(db, vm)
            if any(result.values()):
                log.info("exported %s", result)
        except ExportError as exc:
            log.warning("export paused: %s", exc)
        except Exception as exc:  # never let the loop die
            log.error("unexpected export error: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))

        if stop is None:
            time.sleep(interval)
        elif stop.wait(interval):
            log.info("export loop stopping")
            return


def start_background(cfg: Config, db: Database) -> tuple[threading.Thread, threading.Event]:
    """Spawn the export loop as a daemon thread (used by `serve`)."""
    stop = threading.Event()
    t = threading.Thread(target=export_loop, args=(cfg, db, stop), name="export", daemon=True)
    t.start()
    return t, stop


def run_export(config_path: str, *, full: bool = False) -> int:
    """The `sugardaddy export` command: one pass (``--full`` resends everything)."""
    cfg = load_config(config_path)
    if not cfg.victoriametrics.url:
        print("error: [victoriametrics] url is not set")
        return 2
    db = Database(cfg.database.path)
    db.init_db()
    vm = VictoriaMetrics(cfg.victoriametrics.url, cfg.victoriametrics.timeout_seconds)
    try:
        if full:
            reset(db, vm)
        result = sync(db, vm)
    except ExportError as exc:
        print(f"error: {exc}")
        return 1
    print(
        f"sent {result['glucose']} glucose readings; "
        f"{result['events_written']} doses/meals written, {result['events_deleted']} removed"
    )
    return 0
