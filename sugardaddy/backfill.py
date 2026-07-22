"""One-shot historical seed from Home Assistant.

Your HA recorder likely already holds months of Libre history. This pulls that
via HA's history REST API (long-lived token) and imports it into our DB, so the
charts have depth from day one. After this runs, HA is no longer involved.

HA stores the sensor in its display unit; for an Australian Libre setup that is
mmol/L, which we convert to our canonical mg/dL. Override with --unit if your HA
sensor is in mg/dL.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from sugardaddy.config import load_config
from sugardaddy.constants import mmol_to_mgdl
from sugardaddy.db import Database
from sugardaddy.models import GlucoseReading

log = logging.getLogger("sugardaddy.backfill")

_SKIP_STATES = {"unavailable", "unknown", "", "none"}


def _parse_ts(s: str) -> int | None:
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def fetch_history(ha_url: str, entity: str, token: str, days: int) -> list[GlucoseReading]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    url = f"{ha_url.rstrip('/')}/api/history/period/{start.isoformat()}"
    params = {
        "filter_entity_id": entity,
        "end_time": end.isoformat(),
        "minimal_response": "",
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    resp = httpx.get(url, params=params, headers=headers, timeout=120.0)
    resp.raise_for_status()
    data = resp.json()  # list of per-entity lists

    readings: list[GlucoseReading] = []
    for series in data:
        for state in series:
            raw = str(state.get("state", "")).strip().lower()
            if raw in _SKIP_STATES:
                continue
            try:
                value = float(state["state"])
            except (KeyError, ValueError):
                continue
            ts = _parse_ts(state.get("last_changed") or state.get("last_updated") or "")
            if ts is None:
                continue
            readings.append(GlucoseReading(ts_utc=ts, value_mgdl=value, source="ha-backfill"))
    return readings


def run_backfill(config_path: str, days: int = 90, unit: str = "") -> int:
    cfg = load_config(config_path)
    bf = cfg.backfill
    if not bf.ha_url or not bf.ha_entity:
        log.error("set [backfill].ha_url and [backfill].ha_entity in the config")
        return 1
    if not bf.token:
        log.error("missing HA token — set SUGARDADDY_HA_TOKEN")
        return 1

    unit = (unit or cfg.web.units).strip()
    db = Database(cfg.database.path)
    db.init_db()

    log.info("fetching %d days of %s from HA (%s)", days, bf.ha_entity, bf.ha_url)
    try:
        readings = fetch_history(bf.ha_url, bf.ha_entity, bf.token, days)
    except httpx.HTTPError as exc:
        log.error("HA request failed: %s", exc)
        return 1

    if unit == "mmol/L":
        for r in readings:
            r.value_mgdl = mmol_to_mgdl(r.value_mgdl)
        log.info("converted %d values mmol/L → mg/dL", len(readings))

    added = db.add_readings(readings)
    log.info("backfill: %d fetched, %d new (total now %d)", len(readings), added, db.reading_count())
    return 0
