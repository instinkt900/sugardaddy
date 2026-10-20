"""Tests for the line between *logging* a meal and *reusing* one.

No test framework is required: run directly with

    python tests/test_logging_vs_reuse.py

Logging is a record of what happened; the food library and saved meals are what
gets reused. They used to bleed into each other — a carb-less plate line joined
the library as a "pending" food, and logging a named meal rewrote the saved
meal of that name — and these tests pin the separation. They drive the real app
(FastAPI's TestClient over a temporary SQLite file) because the behaviour lives
in the write routes, not in a pure function.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from sugardaddy.analysis import dose_presets, meal_moment  # noqa: E402
from sugardaddy.models import GlucoseReading, InsulinDose  # noqa: E402
from sugardaddy.web import create_app  # noqa: E402

T0 = 1_784_764_800  # 2026-07-23 00:00:00 UTC


def fresh_client(extra: str = "") -> TestClient:
    tmp = Path(tempfile.mkdtemp(prefix="sugardaddy-test-"))
    cfg = tmp / "config.toml"
    cfg.write_text(
        f'[database]\npath = "{tmp / "t.db"}"\n'
        '[web]\ntimezone = "UTC"\nunits = "mmol/L"\n' + extra
    )
    return TestClient(create_app(str(cfg), start_ingest=False))


def log_meal(c: TestClient, **body) -> dict:
    r = c.post("/api/meal", json={"ts": "", **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_typed_one_off_does_not_join_the_library():
    c = fresh_client()
    log_meal(c, items=[{"name": "Holiday burrito", "count": 1}])
    assert c.get("/api/foods").json() == [], "a one-off line must not become a library food"
    todo = c.get("/api/missing-carbs").json()
    assert [t["name"] for t in todo] == ["Holiday burrito"], "but it is still on the to-do"


def test_keep_puts_a_line_in_the_library():
    c = fresh_client()
    log_meal(c, items=[{"name": "Toast", "carbs_g": 15, "keep": True}])
    foods = c.get("/api/foods").json()
    assert [(f["name"], f["carbs_g"]) for f in foods] == [("Toast", 15)]


def test_a_known_name_still_takes_the_library_carbs():
    c = fresh_client()
    c.post("/api/foods", json={"name": "Milo bar", "carbs_g": 16})
    meal = log_meal(c, items=[{"name": "milo bar"}])
    assert meal["items"][0]["carbs_g"] == 16
    assert c.get("/api/missing-carbs").json() == []


def test_filling_missing_carbs_completes_every_blank_line_by_name():
    c = fresh_client()
    log_meal(c, items=[{"name": "Burrito"}])
    log_meal(c, items=[{"name": "burrito"}, {"name": "Chips", "carbs_g": 30}])
    r = c.post("/api/missing-carbs", json={"name": "Burrito", "carbs_g": 55}).json()
    assert r["filled_items"] == 2
    assert c.get("/api/missing-carbs").json() == []
    assert c.get("/api/foods").json() == [], "filling a one-off doesn't add it to the library"


def test_filling_never_rewrites_a_recorded_value():
    c = fresh_client()
    log_meal(c, items=[{"name": "Rice", "carbs_g": 28}])
    log_meal(c, items=[{"name": "Rice"}])
    c.post("/api/missing-carbs", json={"name": "Rice", "carbs_g": 40})
    carbs = sorted(
        i["carbs_g"] for m in c.get("/api/entries?hours=1").json()["meals"] for i in m["items"]
    )
    assert carbs == [28, 40], carbs


def test_logging_a_named_meal_leaves_the_saved_meal_alone():
    c = fresh_client()
    c.post("/api/meal-templates", json={"name": "Red Rooster", "items": [{"name": "Rippa", "carbs_g": 47}]})
    log_meal(c, name="Red Rooster", items=[{"name": "Rippa", "carbs_g": 47}, {"name": "Extra chips", "carbs_g": 60}])
    (t,) = c.get("/api/meal-templates").json()
    assert [i["name"] for i in t["items"]] == ["Rippa"], "a tweaked plate must not redefine the saved meal"
    assert t["uses"] == 1


def test_save_template_is_the_explicit_way_to_update_it():
    c = fresh_client()
    log_meal(c, name="Lunch", save_template=True, items=[{"name": "Wrap", "carbs_g": 40}])
    (t,) = c.get("/api/meal-templates").json()
    assert t["name"] == "Lunch" and len(t["items"]) == 1


def test_favourites_round_trip():
    c = fresh_client()
    f = c.post("/api/foods", json={"name": "Biscuit", "carbs_g": 11.5}).json()
    c.patch(f"/api/foods/{f['id']}", json={"favourite": True})
    assert c.get("/api/foods").json()[0]["favourite"] is True
    sc = c.get("/api/shortcuts").json()
    assert [x["name"] for x in sc["foods"]] == ["Biscuit"]


def test_dose_presets_learned_then_set():
    c = fresh_client()
    for u in (10, 10, 8):
        c.post("/api/insulin", data={"units": u, "kind": "bolus"})
    p = c.get("/api/settings/dose-presets").json()
    assert p["custom"] is False and p["presets"]["bolus"] == [8, 10]
    c.put("/api/settings/dose-presets", json={"presets": {"bolus": [4, "6", 0, -1], "basal": [36]}})
    p = c.get("/api/settings/dose-presets").json()
    assert p["custom"] is True and p["presets"]["bolus"] == [4, 6] and p["presets"]["correction"] == []
    c.put("/api/settings/dose-presets", json={"presets": None})
    assert c.get("/api/settings/dose-presets").json()["custom"] is False


def test_dose_presets_basal_needs_repeats_or_the_prescription():
    once = [InsulinDose(ts_utc=T0, units=10, kind="basal")]
    assert dose_presets(once)["basal"] == []
    assert dose_presets(once, basal_fallback=36)["basal"] == [36]
    twice = once + [InsulinDose(ts_utc=T0 + 1, units=10, kind="basal")]
    assert dose_presets(twice, basal_fallback=36)["basal"] == [10]


def test_meal_moment_excludes_the_meals_own_bolus_from_iob():
    readings = [GlucoseReading(ts_utc=T0 + i * 300, value_mgdl=150) for i in range(-6, 7)]
    doses = [
        InsulinDose(ts_utc=T0 - 3600, units=4, kind="bolus"),  # earlier: on board
        InsulinDose(ts_utc=T0 + 300, units=8, kind="bolus"),   # co-timed: the meal's own
        InsulinDose(ts_utc=T0, units=36, kind="basal"),        # never counted
    ]
    m = meal_moment(readings, doses, T0)
    assert m["start"].ts_utc == T0
    assert m["bolus_units"] == 8
    assert 0 < m["iob_start"] < 4


def test_reference_at_a_meal_uses_its_own_moment():
    c = fresh_client("[insulin]\nisf = 2.0\nicr = 10\ntarget_bg = 6.0\n")
    meal = log_meal(c, items=[{"name": "Pasta", "carbs_g": 50}])
    r = c.get(f"/api/bolus-reference?meal_id={meal['id']}&carbs=80").json()
    assert r["at_meal"] is True
    assert r["ref"]["carb_units"] == 8.0, "carbs from the query override the plate"
    assert "glucose" in r["ref"]["missing"], "no reading near the meal → no correction"
    ins = c.get(f"/api/meal/{meal['id']}/insight").json()
    assert ins["reference"]["ref"]["carb_units"] == 5.0


def test_estimate_lines_keep_their_tag():
    c = fresh_client()
    m = log_meal(c, items=[{"name": "Something at the fair", "carbs_g": 40, "tags": "estimate"}])
    assert m["items"][0]["tags"] == "estimate"


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"\n{n} checks passed")
