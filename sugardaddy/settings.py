"""Settings edited from the desktop, layered over the TOML.

The TOML stays the source of defaults and of everything the app needs before
it can open its database (the DB path, host/port). Anything the desktop's
Settings view changes is stored in the database and **wins over the file**; the
file value comes back the moment the override is reset. That split is what lets
ISF/ICR, targets, reminders and the export be changed without a redeploy, while
keeping the app bootable from the file alone.

Two things never move into the DB:

- **Secrets** (LibreLinkUp credentials, the VAPID signing key, the HA token)
  stay env-only. The DB is copied around on purpose — the review skill pulls it
  to other machines, staging runs on a snapshot of it — so a secret stored there
  would travel with every copy.
- **Bootstrap** values (database path, host, port), which are needed to find the
  DB in the first place.

Every override change is recorded in ``settings_history``, because ISF/ICR feed
the bolus reference and a figure is only explainable if you can see which
numbers it was computed with, and since when.

``SUGARDADDY_NO_OUTBOUND=1`` forces the push reminder and the VictoriaMetrics
export off whatever the DB says. Staging sets it: a snapshot of the live DB
carries the live instance's overrides, and without this a staging copy would
start exporting into the real Grafana the moment prod's export was switched on.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sugardaddy.config import (
    Config,
    ConfigError,
    _check_notify,
    _check_prescription,
    _check_victoriametrics,
    load_config,
)
from sugardaddy.db import Database

_KEY = "config.overrides"


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str  # float | int | bool | str | choice | date — "opt" prefix = may be blank
    help: str = ""
    unit: str = ""
    choices: tuple = ()
    # When the change takes effect, said in the UI so nobody waits on a value
    # that only applies after a reconnect.
    applies: str = "now"


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    note: str
    fields: tuple


SECTIONS: tuple[Section, ...] = (
    Section("web", "Display", "How glucose is shown everywhere, and the band time-in-range counts against.", (
        Field("units", "Glucose units", "choice", choices=("mmol/L", "mg/dL"),
              help="Display only — storage is always mg/dL. Targets and ISF below are in this unit."),
        Field("target_low", "Target low", "float", unit="display unit"),
        Field("target_high", "Target high", "float", unit="display unit"),
        Field("timezone", "Timezone", "str", help="IANA name, e.g. Australia/Melbourne."),
    )),
    Section("insulin", "Insulin maths", "Your own numbers — what the active-insulin curve and the experimental bolus reference run on. "
            "Never inferred from history. Leave ISF blank to switch the reference off everywhere.", (
        Field("isf", "ISF", "optfloat", unit="display unit per u", help="How far 1 u brings glucose down."),
        Field("icr", "ICR", "optfloat", unit="g per u", help="Grams of carbohydrate 1 u covers."),
        Field("target_bg", "Correction target", "optfloat", unit="display unit",
              help="What a correction aims at. Blank = middle of the target band."),
        Field("dia_minutes", "Duration of action", "int", unit="min"),
        Field("peak_minutes", "Time to peak", "int", unit="min"),
    )),
    Section("prescription", "Prescription", "What the clinician prescribed — a record the reports read back, never used in any calculation. "
            "The reviewed date is required once anything is filled in.", (
        Field("reviewed", "Reviewed on", "optdate"),
        Field("basal_product", "Basal insulin", "str"),
        Field("basal_units", "Basal dose", "optfloat", unit="u"),
        Field("basal_timing", "Basal timing", "str"),
        Field("rapid_product", "Rapid insulin", "str"),
        Field("isf", "Prescribed ISF", "optfloat", unit="display unit per u"),
        Field("icr", "Prescribed ICR", "optfloat", unit="g per u"),
    )),
    Section("notify", "Basal reminder", "One push notification: no basal dose logged for the interval plus leniency. "
            "Needs SUGARDADDY_VAPID_PRIVATE_KEY in the environment and HTTPS.", (
        Field("enabled", "Reminder on", "bool"),
        Field("subject", "Contact", "str", help="mailto: or https:// address push services can reach you on."),
        Field("basal_interval_hours", "Expected interval", "float", unit="h"),
        Field("basal_leniency_hours", "Leniency", "float", unit="h"),
        Field("repeat_hours", "Repeat every", "float", unit="h", help="0 = remind once per missed dose."),
        Field("poll_seconds", "Check every", "int", unit="s", help="0 = off (for driving `notify` from cron)."),
        Field("ttl_seconds", "Hold for offline phones", "int", unit="s"),
    )),
    Section("victoriametrics", "VictoriaMetrics export", "Mirrors glucose, doses and meals into VictoriaMetrics for Grafana.", (
        Field("enabled", "Export on", "bool"),
        Field("url", "Server URL", "str", help="Base URL, e.g. http://192.0.2.10:8428"),
        Field("interval_seconds", "Sync every", "int", unit="s"),
        Field("timeout_seconds", "Timeout", "float", unit="s"),
    )),
    Section("librelink", "LibreLinkUp", "The glucose feed. Credentials stay in the environment.", (
        Field("region", "Region", "str", applies="reconnect", help="AU, EU, US, …"),
        Field("patient_id", "Patient ID", "str", applies="reconnect",
              help="Only needed when the account follows more than one person."),
        Field("poll_interval_seconds", "Poll every", "float", unit="s"),
    )),
    Section("backfill", "Home Assistant import", "Where the one-off history import reads from. The token is entered at import time, never stored.", (
        Field("ha_url", "Home Assistant URL", "str"),
        Field("ha_entity", "Glucose entity", "str", help="e.g. sensor.libre_glucose"),
    )),
)

_FIELDS = {(s.id, f.name): f for s in SECTIONS for f in s.fields}


def no_outbound() -> bool:
    return os.environ.get("SUGARDADDY_NO_OUTBOUND", "").strip() not in ("", "0", "false")


# --- parsing / validation ------------------------------------------------------


def _parse(field: Field, raw):
    """Coerce a submitted value to the field's type. Blank on an optional field
    is a real answer (None — e.g. "no ISF, reference off"), not a parse error."""
    kind = field.kind
    optional = kind.startswith("opt")
    base = kind[3:] if optional else kind
    if isinstance(raw, str):
        raw = raw.strip()
    if raw is None or raw == "":
        if optional:
            return None
        if base == "str":
            return ""
        raise ConfigError(f"{field.label} can't be blank")
    try:
        if base == "float":
            return float(raw)
        if base == "int":
            return int(float(raw))
        if base == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("1", "true", "on", "yes")
        if base == "choice":
            if raw not in field.choices:
                raise ConfigError(f"{field.label} must be one of {', '.join(field.choices)}")
            return raw
        return str(raw)  # str, date (date format checked by _check_prescription)
    except (TypeError, ValueError):
        raise ConfigError(f"{field.label}: {raw!r} is not a valid {base}")


def _validate(cfg: Config) -> None:
    """The file loader's checks plus the ones only a live edit can get wrong."""
    _check_prescription(cfg.prescription)
    _check_notify(cfg.notify)
    _check_victoriametrics(cfg.victoriametrics)
    try:
        ZoneInfo(cfg.web.timezone)
    except Exception:
        raise ConfigError(f"unknown timezone {cfg.web.timezone!r}")
    if not 0 < cfg.web.target_low < cfg.web.target_high:
        raise ConfigError("target low must be above 0 and below target high")
    for name in ("isf", "icr", "target_bg"):
        v = getattr(cfg.insulin, name)
        if v is not None and v <= 0:
            raise ConfigError(f"{name.upper()} must be greater than 0")
    if cfg.insulin.dia_minutes <= 0 or not 0 < cfg.insulin.peak_minutes < cfg.insulin.dia_minutes:
        raise ConfigError("time to peak must be above 0 and shorter than the duration of action")
    if cfg.librelink.poll_interval_seconds < 15:
        raise ConfigError("LibreLinkUp poll must be at least 15 s")


def apply(base: Config, overrides: dict) -> Config:
    """``base`` with ``overrides`` ({section: {field: value}}) laid on top.

    Unknown keys are ignored rather than fatal: an override written by a newer
    build (or for a field since removed) must not stop an older one booting."""
    cfg = copy.deepcopy(base)
    for sec, fields in (overrides or {}).items():
        for name, value in (fields or {}).items():
            if (sec, name) in _FIELDS:
                setattr(getattr(cfg, sec), name, value)
    if no_outbound():
        cfg.notify.enabled = False
        cfg.victoriametrics.enabled = False
    return cfg


def read_overrides(db: Database) -> dict:
    return db.get_setting(_KEY) or {}


def load_effective(config_path: str, db_path: str = "") -> Config:
    """File config plus the DB's overrides — what the CLI commands (report,
    notify, export, backfill, ingest) run on, so they agree with the web app.
    ``db_path`` reads the overrides from that DB (the review skill's copied DB)."""
    base = load_config(config_path)
    db = Database(db_path or base.database.path)
    db.init_db()
    try:
        return apply(base, read_overrides(db))
    except Exception:
        return base


# --- the live config ---------------------------------------------------------


class LiveConfig:
    """A Config that follows the DB's overrides while the app runs.

    Attribute access is forwarded to the current effective Config, so code
    written against a plain Config (``cfg.insulin.isf``, ``cfg.isf_mgdl``) keeps
    working and sees an edit on its next read — the web routes and the background
    loops all hold this one object. Never cache a sub-object across a long wait
    (``ins = cfg.insulin`` and sleep); read through ``cfg`` each time.
    """

    def __init__(self, base: Config, db: Database):
        self._base = base
        self._db = db
        self._lock = threading.Lock()
        try:
            self._current = apply(base, read_overrides(db))
            _validate(self._current)
        except ConfigError:
            # Bad stored overrides must not stop the app booting: fall back to
            # the file, and let the Settings view show (and fix) the problem.
            self._current = apply(base, {})

    def __getattr__(self, name):
        return getattr(self._current, name)

    @property
    def base(self) -> Config:
        return self._base

    def update(self, changes: dict, now: int | None = None) -> None:
        """Apply ``{section: {field: value}}`` edits (see ``reset`` for going back
        to the file). Validates the whole result before storing anything, so a
        bad edit changes nothing. A value equal to the file's is stored as no
        override at all, so the file stays in charge of it."""
        now = now or int(time.time())
        with self._lock:
            overrides = copy.deepcopy(read_overrides(self._db))
            log_rows = []
            for sec, fields in changes.items():
                for name, raw in fields.items():
                    field = _FIELDS.get((sec, name))
                    if field is None:
                        raise ConfigError(f"{sec}.{name} is not editable here")
                    value = _parse(field, raw)
                    before = self._value(overrides, sec, name)
                    file_value = getattr(getattr(self._base, sec), name)
                    if value == file_value:
                        overrides.get(sec, {}).pop(name, None)  # same as file: no override
                    else:
                        overrides.setdefault(sec, {})[name] = value
                    if value != before:
                        log_rows.append((f"{sec}.{name}", before, value))
            candidate = apply(self._base, overrides)
            _validate(candidate)
            self._db.set_setting(_KEY, {k: v for k, v in overrides.items() if v})
            for key, before, after in log_rows:
                self._db.log_setting_change(now, key, before, after)
            self._current = candidate

    def reset(self, keys: list[str], now: int | None = None) -> None:
        """Drop overrides (``"section.field"``), returning those to the file."""
        now = now or int(time.time())
        with self._lock:
            overrides = copy.deepcopy(read_overrides(self._db))
            log_rows = []
            for key in keys:
                sec, _, name = key.partition(".")
                if name in overrides.get(sec, {}):
                    before = overrides[sec].pop(name)
                    log_rows.append((key, before, getattr(getattr(self._base, sec), name)))
            candidate = apply(self._base, overrides)
            _validate(candidate)
            self._db.set_setting(_KEY, {k: v for k, v in overrides.items() if v})
            for key, before, after in log_rows:
                self._db.log_setting_change(now, key, before, after, reset=True)
            self._current = candidate

    def _value(self, overrides: dict, sec: str, name: str):
        if name in overrides.get(sec, {}):
            return overrides[sec][name]
        return getattr(getattr(self._base, sec), name)

    def view(self) -> dict:
        """Everything the Settings view shows: each editable field with its
        effective value, the file's value and where the effective one came from;
        the bootstrap values (read-only); which secrets are set (never their
        values); and the change history."""
        overrides = read_overrides(self._db)
        sections = []
        for s in SECTIONS:
            fields = []
            for f in s.fields:
                over = f.name in overrides.get(s.id, {})
                fields.append({
                    "key": f"{s.id}.{f.name}", "name": f.name, "label": f.label, "kind": f.kind,
                    "help": f.help, "unit": f.unit.replace("display unit", self._current.web.units),
                    "choices": list(f.choices), "applies": f.applies,
                    "value": getattr(getattr(self._current, s.id), f.name),
                    "file_value": getattr(getattr(self._base, s.id), f.name),
                    "source": "db" if over else "file",
                })
            sections.append({"id": s.id, "title": s.title, "note": s.note, "fields": fields})
        cur = self._current
        return {
            "sections": sections,
            "no_outbound": no_outbound(),
            "bootstrap": [
                {"label": "Database", "value": cur.database.path},
                {"label": "Listen on", "value": f"{cur.web.host}:{cur.web.port}"},
            ],
            "secrets": [
                {"name": "SUGARDADDY_LIBRE_EMAIL / _PASSWORD", "what": "LibreLinkUp login",
                 "set": bool(cur.librelink.email and cur.librelink.password)},
                {"name": "SUGARDADDY_VAPID_PRIVATE_KEY", "what": "push signing key",
                 "set": bool(cur.vapid_private_key)},
                {"name": "SUGARDADDY_HA_TOKEN", "what": "Home Assistant token (optional — can be pasted at import)",
                 "set": bool(cur.backfill.token)},
            ],
            "history": self._db.setting_history(50),
        }

