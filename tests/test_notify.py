"""Tests for the basal reminder (sugardaddy.analysis.basal_status + notify).

No test framework is required: run directly with

    python tests/test_notify.py

Same conventions as the other test files — plain ``assert``, fixed epoch
timestamps, pytest-compatible. Nothing here touches the network or the crypto
stack: ``notify``'s pywebpush/cryptography imports are lazy, and the gate tests
drive ``run_pass`` in dry-run mode against a throwaway SQLite file with no
subscriptions registered.

Two things are worth pinning down. First the threshold arithmetic, including the
"never logged a basal" case, which must stay silent — nagging a fresh install
about a dose it has no evidence you take would be worse than not reminding at all.
Second the gates, because the failure mode of a poller that checks every 15
minutes is 96 notifications a day.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugardaddy import notify  # noqa: E402
from sugardaddy.analysis import basal_status  # noqa: E402
from sugardaddy.config import (  # noqa: E402
    Config,
    DatabaseConfig,
    LibreLinkConfig,
    NotifyConfig,
    WebConfig,
)
from sugardaddy.db import Database  # noqa: E402
from sugardaddy.models import InsulinDose  # noqa: E402

HOUR = 3600
NOW = 1_784_764_800  # 2026-07-23 00:00:00 UTC
SYD = ZoneInfo("Australia/Sydney")


# --- the threshold ----------------------------------------------------------


def test_recent_basal_is_not_due():
    s = basal_status(NOW - 12 * HOUR, NOW)
    assert s["due"] is False, s
    assert s["hours_since"] == 12.0
    assert s["overdue_hours"] == 0.0


def test_leniency_is_added_to_the_interval():
    # Default 24 + 1: at 24.5 h the dose is late but the reminder holds off.
    assert basal_status(NOW - 24 * HOUR - 1800, NOW)["due"] is False
    assert basal_status(NOW - 25 * HOUR, NOW)["due"] is True


def test_due_exactly_on_the_boundary():
    s = basal_status(NOW - 25 * HOUR, NOW, interval_hours=24, leniency_hours=1)
    assert s["due"] is True and s["overdue_hours"] == 0.0, s


def test_leniency_can_be_widened():
    late = NOW - 26 * HOUR
    assert basal_status(late, NOW, leniency_hours=1)["due"] is True
    assert basal_status(late, NOW, leniency_hours=3)["due"] is False


def test_zero_leniency_fires_at_the_interval():
    assert basal_status(NOW - 24 * HOUR, NOW, leniency_hours=0)["due"] is True


def test_overdue_hours_measures_past_the_threshold():
    # 30 h since the dose, threshold at 25 h -> 5 h overdue, not 30.
    s = basal_status(NOW - 30 * HOUR, NOW)
    assert s["overdue_hours"] == 5.0, s


def test_never_logged_is_never_due():
    s = basal_status(None, NOW)
    assert s["due"] is False and s["hours_since"] is None and s["due_at"] is None, s


# --- the payload ------------------------------------------------------------


def test_payload_reports_the_log_not_a_dose():
    s = basal_status(NOW - 25 * HOUR, NOW)
    p = notify.build_payload(s, SYD)
    assert p["title"] == "Basal not logged"
    assert "No basal dose logged for 25 h" in p["body"], p
    # The one wording rule that matters: it must never read as an instruction.
    assert "take" not in p["body"].lower(), p


def test_payload_names_when_the_last_dose_was_in_local_time():
    # 2026-07-21 23:00 UTC is 09:00 on Wed 22 July in Sydney (UTC+10).
    s = basal_status(NOW - 25 * HOUR, NOW)
    assert notify.build_payload(s, SYD)["body"].endswith("last one was Wed 09:00.")


def test_repost_is_silent_and_does_not_realert():
    s = basal_status(NOW - 30 * HOUR, NOW)
    first = notify.build_payload(s, SYD)
    again = notify.build_payload(s, SYD, repost=True)
    assert first["silent"] is False and first["renotify"] is True
    assert again["silent"] is True and again["renotify"] is False
    # Same tag either way, so a repost replaces rather than stacks.
    assert first["tag"] == again["tag"]


def test_gap_is_coarse():
    assert notify.humanize_gap(25.4) == "25 h"
    assert notify.humanize_gap(24.0) == "24 h"
    assert notify.humanize_gap(50.0) == "2 days"


# --- the gates --------------------------------------------------------------


def _cfg(db_path: str, **notify_kw) -> Config:
    kw = {"enabled": True, "subject": "mailto:you@example.com"}
    kw.update(notify_kw)
    return Config(
        librelink=LibreLinkConfig(),
        database=DatabaseConfig(path=db_path),
        web=WebConfig(),
        notify=NotifyConfig(**kw),
    )


def _db(tmp: str, *basal_offsets_hours: float) -> Database:
    db = Database(str(Path(tmp) / "test.db"))
    db.init_db()
    for h in basal_offsets_hours:
        db.add_dose(InsulinDose(ts_utc=NOW - int(h * HOUR), units=12, kind="basal"))
    return db


def _run(db, cfg):
    """One dry-run pass. Dry-run stops short of the network and stamps nothing,
    so the gates can be exercised without a subscription or a push service."""
    return notify.run_pass(db, cfg, NOW, SYD, dry_run=True)


def test_pass_is_quiet_when_nothing_is_overdue():
    tmp = tempfile.mkdtemp()
    try:
        result = _run(_db(tmp, 2), _cfg(tmp))
        assert result["due"] is False and "payload" not in result, result
    finally:
        shutil.rmtree(tmp)


def test_pass_ignores_bolus_doses():
    # A day of boluses says nothing about basal — the reminder must still fire.
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, 30)
        db.add_dose(InsulinDose(ts_utc=NOW - HOUR, units=6, kind="bolus"))
        db.add_dose(InsulinDose(ts_utc=NOW - 2 * HOUR, units=3, kind="correction"))
        assert _run(db, _cfg(tmp))["due"] is True
    finally:
        shutil.rmtree(tmp)


def test_pass_notifies_once_then_waits_for_the_repeat_interval():
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, 30)
        cfg = _cfg(tmp, repeat_hours=4)
        first = _run(db, cfg)
        assert "payload" in first and first["repost"] is False, first

        # Simulate that first delivery having succeeded (run_pass only stamps on a
        # real send), then check the same dose stays quiet until the interval.
        anchor = str(first["status"]["last_ts"])
        db.set_state("basal_notified_anchor", anchor)
        db.set_state("basal_notified_at", str(NOW - HOUR))
        assert "payload" not in _run(db, cfg)

        db.set_state("basal_notified_at", str(NOW - 5 * HOUR))
        again = _run(db, cfg)
        assert "payload" in again and again["repost"] is True, again
        assert again["payload"]["silent"] is True
    finally:
        shutil.rmtree(tmp)


def test_repeat_hours_zero_means_one_notification_per_missed_dose():
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, 30)
        cfg = _cfg(tmp, repeat_hours=0)
        db.set_state("basal_notified_anchor", str(NOW - 30 * HOUR))
        db.set_state("basal_notified_at", str(NOW - 20 * HOUR))
        assert "payload" not in _run(db, cfg)
    finally:
        shutil.rmtree(tmp)


def test_logging_a_basal_rearms_the_reminder():
    # The anchor is the dose we last notified about. A newer basal that is itself
    # overdue is a different cycle, so it alerts rather than being suppressed.
    tmp = tempfile.mkdtemp()
    try:
        db = _db(tmp, 60, 30)
        cfg = _cfg(tmp, repeat_hours=4)
        db.set_state("basal_notified_anchor", str(NOW - 60 * HOUR))
        db.set_state("basal_notified_at", str(NOW - 35 * HOUR))
        result = _run(db, cfg)
        assert result["repost"] is False and "payload" in result, result
        assert result["status"]["last_ts"] == NOW - 30 * HOUR
    finally:
        shutil.rmtree(tmp)


def test_pass_says_nothing_when_no_basal_was_ever_logged():
    tmp = tempfile.mkdtemp()
    try:
        result = _run(_db(tmp), _cfg(tmp))
        assert result["due"] is False and result["status"]["last_ts"] is None, result
    finally:
        shutil.rmtree(tmp)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")


if __name__ == "__main__":
    _run_all()
