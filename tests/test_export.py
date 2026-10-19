"""Tests for the VictoriaMetrics export (sugardaddy.export).

No test framework is required: run directly with

    python tests/test_export.py

Plain ``assert``, fixed epoch timestamps, pytest-compatible. Nothing touches the
network: ``sync`` runs against a throwaway SQLite file and a fake server that
records what it was sent.

What matters is that the export stays a faithful copy. Glucose must go out once
per reading, including one that lands behind the cursor's timestamp. A dose or meal
that is edited or deleted after it was sent must lose its old series, or Grafana
would keep showing a dose that never happened. And a failed send must not move
the state, so the next pass retries it.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugardaddy import export  # noqa: E402
from sugardaddy.db import Database  # noqa: E402
from sugardaddy.models import GlucoseReading, InsulinDose, Meal, MealItem  # noqa: E402

T0 = 1_760_000_000


class FakeVM:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.deleted: list[str] = []
        self.fail = False

    def write(self, lines: list[str]) -> None:
        if self.fail:
            raise export.ExportError("down")
        self.lines.extend(lines)

    def delete_series(self, match: str) -> None:
        if self.fail:
            raise export.ExportError("down")
        self.deleted.append(match)


def _db() -> tuple[Database, str]:
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "test.db")
    db.init_db()
    return db, tmp


def test_lines() -> None:
    assert export.glucose_line(T0, 108, 3) == f"sugardaddy_glucose mgdl=108,mmol=5.99,trend=3 {T0}"
    assert export.glucose_line(T0, 90, None) == f"sugardaddy_glucose mgdl=90,mmol=4.99 {T0}"
    dose = InsulinDose(ts_utc=T0, units=4.5, kind="bolus", id=7)
    assert export.dose_line(dose) == f"sugardaddy_insulin,id=7,kind=bolus units=4.5 {T0}"
    meal = Meal(
        ts_utc=T0, meal_type="lunch", id=3,
        items=[MealItem(name="toast", count=2, carbs_g=15), MealItem(name="egg")],
    )
    assert export.meal_line(meal) == (
        f"sugardaddy_meal,id=3,meal_type=lunch logged=1,items=2,items_without_carbs=1,carbs_g=30 {T0}"
    )
    # No meal type, no carbs: the empty tag and the unknown fields are left out.
    bare = Meal(ts_utc=T0, id=4, items=[MealItem(name="mystery")])
    assert export.meal_line(bare) == f"sugardaddy_meal,id=4 logged=1,items=1,items_without_carbs=1 {T0}"


def test_diff_events() -> None:
    sent = {"insulin:1": export._fingerprint("a"), "insulin:2": export._fingerprint("b")}
    stale, write = export.diff_events(sent, {"insulin:1": "a", "insulin:2": "B", "meal:1": "c"})
    assert stale == ["insulin:2"]
    assert sorted(write) == ["insulin:2", "meal:1"]
    stale, write = export.diff_events(sent, {"insulin:1": "a"})
    assert (stale, write) == (["insulin:2"], [])


def test_sync_glucose_is_sent_once_including_late_rows() -> None:
    db, tmp = _db()
    try:
        vm = FakeVM()
        db.add_readings([GlucoseReading(T0 + 60 * i, 100 + i) for i in range(3)])
        assert export.sync(db, vm)["glucose"] == 3
        assert export.sync(db, vm)["glucose"] == 0
        # The recent sync can insert a reading older than the newest one.
        db.add_reading(GlucoseReading(T0 - 600, 95))
        assert export.sync(db, vm)["glucose"] == 1
        assert vm.lines[-1].endswith(str(T0 - 600))
        assert len(vm.lines) == 4
    finally:
        shutil.rmtree(tmp)


def test_sync_events_follow_edits_and_deletes() -> None:
    db, tmp = _db()
    try:
        vm = FakeVM()
        dose_id = db.add_dose(InsulinDose(ts_utc=T0, units=4, kind="bolus"))
        meal_id = db.add_meal(Meal(ts_utc=T0, items=[MealItem(name="toast", carbs_g=15)]))
        assert export.sync(db, vm)["events_written"] == 2
        assert export.sync(db, vm)["events_written"] == 0

        db.update_dose(dose_id, kind="correction")
        result = export.sync(db, vm)
        assert (result["events_deleted"], result["events_written"]) == (1, 1)
        assert vm.deleted == [f'{{__name__=~"sugardaddy_insulin_.*",db="sugardaddy",id="{dose_id}"}}']
        assert "kind=correction" in vm.lines[-1]

        db.delete_meal(meal_id)
        result = export.sync(db, vm)
        assert (result["events_deleted"], result["events_written"]) == (1, 0)
        assert f'id="{meal_id}"' in vm.deleted[-1]
    finally:
        shutil.rmtree(tmp)


def test_failed_send_keeps_state() -> None:
    db, tmp = _db()
    try:
        vm = FakeVM()
        vm.fail = True
        db.add_reading(GlucoseReading(T0, 100))
        db.add_dose(InsulinDose(ts_utc=T0, units=4))
        try:
            export.sync(db, vm)
            raise AssertionError("expected ExportError")
        except export.ExportError:
            pass
        vm.fail = False
        result = export.sync(db, vm)
        assert (result["glucose"], result["events_written"]) == (1, 1)
    finally:
        shutil.rmtree(tmp)


def test_reset_resends_everything() -> None:
    db, tmp = _db()
    try:
        vm = FakeVM()
        db.add_reading(GlucoseReading(T0, 100))
        db.add_dose(InsulinDose(ts_utc=T0, units=4))
        export.sync(db, vm)
        export.reset(db, vm)
        assert vm.deleted == ['{__name__=~"sugardaddy_.*",db="sugardaddy"}']
        result = export.sync(db, vm)
        assert (result["glucose"], result["events_written"]) == (1, 1)
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
