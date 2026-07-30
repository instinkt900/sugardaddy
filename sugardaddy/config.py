"""Configuration loading.

One TOML file describes the whole app. Secrets are never stored in the TOML:
the LibreLinkUp credentials come from SUGARDADDY_LIBRE_EMAIL / _PASSWORD, and the
one-time HA backfill token from SUGARDADDY_HA_TOKEN.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # py3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from sugardaddy.constants import (
    DEFAULT_TARGET_HIGH_MMOL,
    DEFAULT_TARGET_LOW_MMOL,
)


class ConfigError(RuntimeError):
    pass


@dataclass
class LibreLinkConfig:
    # Region code understood by pylibrelinkup's APIUrl enum (AU, EU, US, ...).
    region: str = "AU"
    poll_interval_seconds: float = 60.0
    # Optional: pick a specific followed patient when the account follows >1.
    patient_id: str = ""
    # Credentials come from the environment, never the TOML.
    email: str = ""
    password: str = ""


@dataclass
class DatabaseConfig:
    path: str = "/data/sugardaddy.db"


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    timezone: str = "Australia/Sydney"
    units: str = "mmol/L"  # "mmol/L" | "mg/dL"
    target_low: float = DEFAULT_TARGET_LOW_MMOL
    target_high: float = DEFAULT_TARGET_HIGH_MMOL


@dataclass
class InsulinConfig:
    # Rapid-acting activity curve, user/clinician-owned (see docs/plans/
    # insulin-awareness.md). Used only for retrospective IOB context, never dosing.
    dia_minutes: int = 300  # Duration of Insulin Action (~4–6 h for rapid analogs)
    peak_minutes: int = 75  # time to peak activity (~65–75 min aspart/lispro)
    # Strength parameters for the EXPERIMENTAL retrospective bolus reference.
    # Unset (None) keeps that feature switched off entirely — the app must never
    # infer or auto-tune these, so "no value" means "no figure", not "guess one".
    # isf/target_bg are in the DISPLAY unit (like target_low/high); icr is grams.
    isf: float | None = None        # glucose drop per 1 u
    icr: float | None = None        # grams of carb covered by 1 u
    target_bg: float | None = None  # correction aim; defaults to the target band midpoint


@dataclass
class NotifyConfig:
    """Push notifications. One reminder only: basal has gone unlogged.

    The app is its own Web Push application server — it signs and encrypts every
    message with its own VAPID key (see notify.py), so there is no notification
    account or API key anywhere. ``enabled`` is the whole feature's on/off switch.
    """

    enabled: bool = False
    # VAPID contact address, required by push services so they can reach whoever
    # runs this server about a misbehaving application server. mailto: or https:.
    subject: str = ""
    # How often the in-process notifier checks. 0 turns it off, for driving
    # `sugardaddy notify` from cron instead; the default suits the Docker
    # deployment, which has no cron in the container.
    poll_seconds: int = 900
    # How long the push service should hold a message for a phone that's offline.
    ttl_seconds: int = 86_400
    # The reminder itself: how long a gap between logged basal doses is expected,
    # and how much grace to allow on top before saying anything. Kept separate so
    # 24 h stays the honest threshold while the nag holds off until 25.
    basal_interval_hours: float = 24.0
    basal_leniency_hours: float = 1.0
    # Re-push a still-unlogged basal this often, so swiping the notification away
    # only buys you until the next one. Reposts are silent and never re-alert —
    # only the first notification of a cycle makes a noise.
    #
    # Fractional hours are allowed and the default is deliberately short: the
    # reminder is worth most in the first hour, while "just take the usual dose"
    # is still the answer. Hours later it is a judgement call instead, and no
    # amount of nudging helps. A value below poll_seconds cannot be honoured,
    # since that is how often the check runs. 0 = notify once per cycle.
    repeat_hours: float = 0.5


@dataclass
class BackfillConfig:
    # Only used by the one-shot `backfill` command to seed history from HA.
    ha_url: str = ""
    ha_entity: str = ""
    token: str = ""  # from SUGARDADDY_HA_TOKEN


@dataclass
class Config:
    librelink: LibreLinkConfig
    database: DatabaseConfig
    web: WebConfig
    insulin: InsulinConfig = field(default_factory=InsulinConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    backfill: BackfillConfig = field(default_factory=BackfillConfig)
    # Env-only: the VAPID signing key. Anyone holding it can push to every
    # subscribed device, so it must never land in a file that could be committed.
    vapid_private_key: str = ""

    @property
    def target_low_mgdl(self) -> float:
        from sugardaddy.constants import mmol_to_mgdl

        return mmol_to_mgdl(self.web.target_low)

    @property
    def target_high_mgdl(self) -> float:
        from sugardaddy.constants import mmol_to_mgdl

        return mmol_to_mgdl(self.web.target_high)

    @property
    def isf_mgdl(self) -> float | None:
        """Configured ISF in mg/dL per unit, or None when unset. ISF is a *delta*,
        so it scales by the same factor as a reading (no offset involved)."""
        from sugardaddy.constants import mmol_to_mgdl

        if self.insulin.isf is None:
            return None
        return mmol_to_mgdl(self.insulin.isf) if self.web.units == "mmol/L" else self.insulin.isf

    @property
    def bolus_target_mgdl(self) -> float:
        """What a correction aims at. Defaults to the midpoint of the target band
        — an explainable choice rather than a clinical one; set `target_bg` in
        [insulin] to use your own."""
        from sugardaddy.constants import mmol_to_mgdl

        if self.insulin.target_bg is None:
            return (self.target_low_mgdl + self.target_high_mgdl) / 2
        return (
            mmol_to_mgdl(self.insulin.target_bg)
            if self.web.units == "mmol/L"
            else self.insulin.target_bg
        )


def _known(section: dict, cls) -> dict:
    """Keep only keys the dataclass declares, so an unknown TOML key fails loudly
    instead of silently exploding the constructor."""
    allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in [{cls.__name__}]: {sorted(unknown)}")
    return {k: v for k, v in section.items() if k in allowed}


def load_config(path: str | os.PathLike) -> Config:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    with p.open("rb") as fh:
        raw = tomllib.load(fh)

    # [librelink] — credentials strictly from env; reject them in the TOML.
    ll_raw = raw.get("librelink", {})
    for secret in ("email", "password"):
        if secret in ll_raw:
            raise ConfigError(f"do not put '{secret}' in the TOML; use SUGARDADDY_LIBRE_{secret.upper()}")
    librelink = LibreLinkConfig(**_known(ll_raw, LibreLinkConfig))
    librelink.email = os.environ.get("SUGARDADDY_LIBRE_EMAIL", "").strip()
    librelink.password = os.environ.get("SUGARDADDY_LIBRE_PASSWORD", "").strip()

    database = DatabaseConfig(**_known(raw.get("database", {}), DatabaseConfig))
    web = WebConfig(**_known(raw.get("web", {}), WebConfig))
    insulin = InsulinConfig(**_known(raw.get("insulin", {}), InsulinConfig))
    notify = NotifyConfig(**_known(raw.get("notify", {}), NotifyConfig))
    _check_notify(notify)

    bf_raw = _known(raw.get("backfill", {}), BackfillConfig)
    bf_raw.pop("token", None)  # never from TOML
    backfill = BackfillConfig(**bf_raw)
    backfill.token = os.environ.get("SUGARDADDY_HA_TOKEN", "").strip()

    cfg = Config(
        librelink=librelink,
        database=database,
        web=web,
        insulin=insulin,
        notify=notify,
        backfill=backfill,
    )
    cfg.vapid_private_key = os.environ.get("SUGARDADDY_VAPID_PRIVATE_KEY", "").strip()
    return cfg


def _check_notify(notify: NotifyConfig) -> None:
    """Validate [notify] at load time, but only when it is switched on.

    A misconfigured notifier fails silently by nature — you find out by not being
    reminded — so anything checkable is checked at startup instead. A subject the
    push service rejects would otherwise only surface as a 403 hours later.
    """
    if not notify.enabled:
        return
    if not notify.subject.startswith(("mailto:", "https://")):
        raise ConfigError(
            "[notify] subject must be a 'mailto:' or 'https://' contact URL "
            f"(got {notify.subject!r})"
        )
    if notify.poll_seconds < 0:
        raise ConfigError("[notify] poll_seconds must be >= 0")
    if notify.basal_interval_hours <= 0:
        raise ConfigError("[notify] basal_interval_hours must be > 0")
    if notify.basal_leniency_hours < 0:
        raise ConfigError("[notify] basal_leniency_hours must be >= 0")
    if notify.repeat_hours < 0:
        raise ConfigError("[notify] repeat_hours must be >= 0 (0 = notify once per cycle)")
