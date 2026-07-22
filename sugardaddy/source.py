"""Glucose data sources.

`GlucoseSource` is the seam that keeps the rest of the app independent of any one
provider. `LibreLinkUpSource` is the real one (Abbott's LibreLinkUp via the
`pylibrelinkup` client). Home Assistant is used only for the one-off historical
backfill (see backfill.py), not as a runtime source — but the ABC leaves room to
add one later.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from sugardaddy.config import LibreLinkConfig
from sugardaddy.models import GlucoseReading

log = logging.getLogger("sugardaddy.source")


class SourceError(RuntimeError):
    pass


class GlucoseSource(ABC):
    @abstractmethod
    def connect(self) -> None:
        """Authenticate / prepare. Safe to call again to re-establish."""

    @abstractmethod
    def latest(self) -> GlucoseReading | None:
        """The single most recent reading (with a trend), or None."""

    @abstractmethod
    def recent(self) -> list[GlucoseReading]:
        """A window of recent readings (~last 12h) for gap-filling on startup."""


def _to_reading(m, *, with_trend: bool) -> GlucoseReading | None:
    """Map a pylibrelinkup measurement to our model. Uses the UTC
    factory_timestamp and the mg/dL value; trend only exists on `latest`."""
    ts = getattr(m, "factory_timestamp", None) or getattr(m, "timestamp", None)
    if ts is None:
        return None
    mgdl = getattr(m, "value_in_mg_per_dl", None)
    if not mgdl:  # 0.0 or None → fall back to generic value
        mgdl = getattr(m, "value", 0.0)
    if not mgdl:
        return None
    trend = None
    if with_trend:
        t = getattr(m, "trend", None)
        if t is not None:
            try:
                trend = int(t)
            except (TypeError, ValueError):
                trend = None
    return GlucoseReading(ts_utc=int(ts.timestamp()), value_mgdl=float(mgdl), trend=trend)


class LibreLinkUpSource(GlucoseSource):
    def __init__(self, cfg: LibreLinkConfig):
        self.cfg = cfg
        self._client = None
        self._patient = None

    def connect(self) -> None:
        try:
            from pylibrelinkup import APIUrl, PyLibreLinkUp
        except ImportError as exc:  # pragma: no cover
            raise SourceError("pylibrelinkup is not installed") from exc

        if not self.cfg.email or not self.cfg.password:
            raise SourceError(
                "missing credentials — set SUGARDADDY_LIBRE_EMAIL and SUGARDADDY_LIBRE_PASSWORD"
            )

        region = self.cfg.region.upper()
        try:
            api_url = getattr(APIUrl, region)
        except AttributeError as exc:
            raise SourceError(f"unknown region {region!r} (see pylibrelinkup APIUrl)") from exc

        self._client = PyLibreLinkUp(
            email=self.cfg.email, password=self.cfg.password, api_url=api_url
        )
        self._client.authenticate()
        self._patient = self._resolve_patient()
        log.info("connected to LibreLinkUp (region %s)", region)

    def _resolve_patient(self):
        patients = self._client.get_patients()
        if not patients:
            raise SourceError("LibreLinkUp account follows no patients")
        want = self.cfg.patient_id.strip()
        if want:
            for p in patients:
                if want in (str(getattr(p, "patient_id", "")), str(getattr(p, "id", ""))):
                    return p
            raise SourceError(f"configured patient_id {want!r} not found among followed patients")
        if len(patients) > 1:
            log.warning(
                "account follows %d patients; using the first. Set [librelink].patient_id to choose.",
                len(patients),
            )
        return patients[0]

    def _ensure(self) -> None:
        if self._client is None or self._patient is None:
            self.connect()

    def latest(self) -> GlucoseReading | None:
        self._ensure()
        try:
            m = self._client.latest(patient_identifier=self._patient)
        except Exception as exc:
            # A stale token surfaces here; drop the client so the next call re-auths.
            log.warning("latest() failed (%s); will reconnect next poll", exc)
            self._client = None
            raise SourceError(str(exc)) from exc
        return _to_reading(m, with_trend=True) if m else None

    def recent(self) -> list[GlucoseReading]:
        self._ensure()
        try:
            measurements = self._client.graph(patient_identifier=self._patient)
        except Exception as exc:
            log.warning("graph() failed (%s)", exc)
            self._client = None
            raise SourceError(str(exc)) from exc
        out = []
        for m in measurements or []:
            r = _to_reading(m, with_trend=False)
            if r:
                out.append(r)
        return out


def build_source(cfg: LibreLinkConfig) -> GlucoseSource:
    return LibreLinkUpSource(cfg)
