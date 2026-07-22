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
    backfill: BackfillConfig = field(default_factory=BackfillConfig)

    @property
    def target_low_mgdl(self) -> float:
        from sugardaddy.constants import mmol_to_mgdl

        return mmol_to_mgdl(self.web.target_low)

    @property
    def target_high_mgdl(self) -> float:
        from sugardaddy.constants import mmol_to_mgdl

        return mmol_to_mgdl(self.web.target_high)


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

    bf_raw = _known(raw.get("backfill", {}), BackfillConfig)
    bf_raw.pop("token", None)  # never from TOML
    backfill = BackfillConfig(**bf_raw)
    backfill.token = os.environ.get("SUGARDADDY_HA_TOKEN", "").strip()

    return Config(librelink=librelink, database=database, web=web, backfill=backfill)
