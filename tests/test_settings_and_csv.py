"""Tests for desktop-edited settings and CSV import/export.

No test framework is required: run directly with

    python tests/test_settings_and_csv.py

Settings: the file gives defaults, the DB's overrides win, a bad edit changes
nothing, every change is in the history, the CLI path sees the same values, and
SUGARDADDY_NO_OUTBOUND beats any override (staging runs on a snapshot of the
live DB, overrides included). CSV: export → import into an empty DB reproduces
the data, and importing the same file twice adds nothing.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from sugardaddy import csvio  # noqa: E402
from sugardaddy.config import ConfigError, load_config  # noqa: E402
from sugardaddy.db import Database  # noqa: E402
from sugardaddy.models import GlucoseReading, InsulinDose, Meal, MealItem, Note  # noqa: E402
from sugardaddy.settings import LiveConfig, load_effective  # noqa: E402
from sugardaddy.web import create_app  # noqa: E402

T0 = 1_784_764_800  # 2026-07-23 00:00:00 UTC
MEL = ZoneInfo("Australia/Melbourne")


def setup(extra: str = ""):
    tmp = Path(tempfile.mkdtemp(prefix="sugardaddy-test-"))
    cfg_path = tmp / "config.toml"
    cfg_path.write_text(
        f'[database]\npath = "{tmp / "t.db"}"\n'
        '[web]\ntimezone = "Australia/Melbourne"\nunits = "mmol/L"\n'
        "[insulin]\nisf = 1.4\nicr = 10\n" + extra
    )
    base = load_config(str(cfg_path))
    db = Database(base.database.path)
    db.init_db()
    return str(cfg_path), base, db


def test_db_overrides_the_file_and_reset_restores_it():
    path, base, db = setup()
    live = LiveConfig(base, db)
    assert live.insulin.isf == 1.4
    live.update({"insulin": {"isf": "1.8"}})
    assert live.insulin.isf == 1.8, "the override wins"
    assert load_effective(path).insulin.isf == 1.8, "the CLI path sees it too"
    live.reset(["insulin.isf"])
    assert live.insulin.isf == 1.4
    hist = db.setting_history()
    assert [(h["key"], h["old"], h["new"], h["reset"]) for h in hist] == [
        ("insulin.isf", 1.8, 1.4, True), ("insulin.isf", 1.4, 1.8, False)]


def test_a_bad_edit_changes_nothing():
    _, base, db = setup()
    live = LiveConfig(base, db)
    for bad in ({"web": {"target_low": 12}}, {"web": {"timezone": "Mars/Base"}},
                {"insulin": {"isf": "-1"}}, {"insulin": {"isf": "abc"}},
                {"victoriametrics": {"enabled": True, "url": "ftp://x"}}):
        try:
            live.update(bad)
            raise AssertionError(f"accepted {bad}")
        except ConfigError:
            pass
    assert live.web.target_low == 3.9 and live.insulin.isf == 1.4
    assert db.setting_history() == []


def test_blank_isf_switches_the_reference_off():
    _, base, db = setup()
    live = LiveConfig(base, db)
    live.update({"insulin": {"isf": ""}})
    assert live.insulin.isf is None and live.isf_mgdl is None


def test_setting_the_file_value_stores_no_override():
    _, base, db = setup()
    live = LiveConfig(base, db)
    live.update({"insulin": {"icr": 12}})
    live.update({"insulin": {"icr": 10}})
    view = {f["key"]: f for s in live.view()["sections"] for f in s["fields"]}
    assert view["insulin.icr"]["source"] == "file"


def test_secrets_are_reported_as_set_or_not_never_shown():
    _, base, db = setup()
    base.librelink.password = "hunter2"
    base.librelink.email = "a@example.com"
    view = LiveConfig(base, db).view()
    assert "hunter2" not in repr(view)
    assert view["secrets"][0]["set"] is True


def test_no_outbound_beats_any_override():
    _, base, db = setup()
    live = LiveConfig(base, db)
    live.update({"victoriametrics": {"enabled": True, "url": "http://192.0.2.10:8428"}})
    assert live.victoriametrics.enabled
    os.environ["SUGARDADDY_NO_OUTBOUND"] = "1"
    try:
        assert not LiveConfig(base, db).victoriametrics.enabled
    finally:
        del os.environ["SUGARDADDY_NO_OUTBOUND"]


def test_web_routes_follow_a_settings_change():
    path, _, _ = setup()
    c = TestClient(create_app(path, start_ingest=False))
    assert c.get("/api/timeline?hours=1").json()["units"] == "mmol/L"
    r = c.put("/api/settings", json={"changes": {"web": {"units": "mg/dL", "target_low": 70, "target_high": 180}}})
    assert r.status_code == 200, r.text
    assert c.get("/api/timeline?hours=1").json()["units"] == "mg/dL"
    bad = c.put("/api/settings", json={"changes": {"web": {"target_low": 500}}})
    assert bad.status_code == 400 and "target" in bad.json()["error"]


def fill(db: Database):
    db.add_readings([GlucoseReading(ts_utc=T0 + i * 300, value_mgdl=100 + i, trend=3) for i in range(5)])
    db.add_dose(InsulinDose(ts_utc=T0, units=8, kind="bolus", note="lunch, pre-bolus"))
    db.add_dose(InsulinDose(ts_utc=T0 + 3600, units=36, kind="basal"))
    db.add_note(Note(ts_utc=T0, text="sick\nall day"))
    db.add_meal(Meal(ts_utc=T0, name="Red Rooster", meal_type="lunch", items=[
        MealItem(name="Rippa roll", carbs_g=47, count=1),
        MealItem(name="Chips, large", carbs_g=None, count=0.5),
        MealItem(name="Cake", carbs_g=40, tags="estimate"),
    ]))


def test_csv_round_trip_and_reimport_is_a_no_op():
    _, _, src = setup()
    _, _, dst = setup()
    fill(src)
    for kind in csvio.KINDS:
        text = csvio.export_csv(src, kind, 0, T0 + 86400, MEL)
        dry = csvio.import_csv(dst, kind, text, MEL, dry_run=True)
        assert dry["errors"] == [] and dry["duplicates"] == 0, (kind, dry)
        real = csvio.import_csv(dst, kind, text, MEL, dry_run=False)
        assert real["added"] == dry["added"] > 0, (kind, real)
        again = csvio.import_csv(dst, kind, text, MEL, dry_run=False)
        assert again["added"] == 0 and again["duplicates"] == real["added"], (kind, again)
    (m,) = dst.meals_between(0, T0 + 86400)
    assert m.name == "Red Rooster" and m.meal_type == "lunch"
    assert [(i.name, i.carbs_g, i.count, i.tags) for i in m.items] == [
        ("Rippa roll", 47, 1, ""), ("Chips, large", None, 0.5, ""), ("Cake", 40, 1, "estimate")]
    assert [n.text for n in dst.notes_between(0, T0 + 86400)] == ["sick\nall day"]
    assert [r.value_mgdl for r in dst.readings_between(0, T0 + 86400)] == [100, 101, 102, 103, 104]


def test_import_reads_hand_made_sheets_and_reports_bad_rows():
    _, _, db = setup()
    text = "time,units,kind\n2026-07-23 12:30,6,bolus\n2026-07-23T13:00:00+10:00,abc,bolus\n,4,bolus\n2026-07-23 14:00,2,turbo\n"
    r = csvio.import_csv(db, "insulin", text, MEL, dry_run=False)
    assert r["added"] == 1 and r["error_count"] == 3, r
    (d,) = db.doses_between(0, T0 + 86400 * 2)
    # A time without an offset is read in the configured zone (AEST, +10).
    assert d.ts_utc == T0 + 2.5 * 3600, d.ts_utc


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"\n{n} checks passed")
