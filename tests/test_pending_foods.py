"""Tests for foods logged without carbs — the "awaiting details" to-do list.

No test framework is required: run directly with

    python tests/test_pending_foods.py

Same conventions as the other test files — plain ``assert``, fixed epoch
timestamps, pytest-compatible. This one uses a real (temporary) SQLite file
rather than pure functions, because the behaviour under test is precisely where
the write layer is allowed to reach back into history: filling a blank snapshot
is completing an entry, overwriting a recorded one would be rewriting it, and
the line between them lives in SQL.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugardaddy.db import Database  # noqa: E402
from sugardaddy.models import Food, Meal, MealItem  # noqa: E402

T0 = 1_784_764_800  # 2026-07-23 00:00:00 UTC


def fresh_db() -> Database:
    tmp = tempfile.mkdtemp(prefix="sugardaddy-test-")
    db = Database(Path(tmp) / "test.db")
    db.init_db()
    return db


def test_pending_is_about_carbs_not_calories():
    assert Food(name="toast").pending
    assert Food(name="toast", calories=90).pending, "calories alone don't complete a food"
    assert not Food(name="toast", carbs_g=15).pending
    # 0 g is a real answer (salad, meat) — not the same as never logged.
    assert not Food(name="steak", carbs_g=0).pending


def test_details_fill_the_blanks_left_on_past_plates():
    db = fresh_db()
    fid = db.add_food(Food(name="mystery pie"))
    m = Meal(ts_utc=T0, items=[MealItem(name="mystery pie", count=2, food_id=fid)])
    meal_id = db.add_meal(m)

    filled = db.backfill_meal_items(fid, carbs_g=42)
    assert filled == 1
    item = db.get_meal(meal_id).items[0]
    assert item.carbs_g == 42
    # Per serving, as everywhere else: the count still multiplies it.
    assert db.get_meal(meal_id).total_carbs == 84


def test_a_recorded_value_is_never_rewritten():
    """The whole reason editing a food doesn't touch history: 30 g is what was
    actually eaten, whatever the library learns later."""
    db = fresh_db()
    fid = db.add_food(Food(name="toast", carbs_g=15))
    meal_id = db.add_meal(
        Meal(ts_utc=T0, items=[MealItem(name="toast", count=1, carbs_g=30, food_id=fid)])
    )
    assert db.backfill_meal_items(fid, carbs_g=15) == 0
    assert db.get_meal(meal_id).items[0].carbs_g == 30


def test_columns_are_filled_independently():
    """Calories arriving must not close off an item still waiting on carbs."""
    db = fresh_db()
    fid = db.add_food(Food(name="soup"))
    meal_id = db.add_meal(Meal(ts_utc=T0, items=[MealItem(name="soup", food_id=fid)]))

    db.backfill_meal_items(fid, calories=120)
    item = db.get_meal(meal_id).items[0]
    assert item.calories == 120
    assert item.carbs_g is None, "still awaiting carbs"

    db.backfill_meal_items(fid, carbs_g=8)
    assert db.get_meal(meal_id).items[0].carbs_g == 8


def test_backfill_only_reaches_items_from_that_food():
    db = fresh_db()
    mine = db.add_food(Food(name="mine"))
    other = db.add_food(Food(name="other"))
    meal_id = db.add_meal(
        Meal(
            ts_utc=T0,
            items=[MealItem(name="mine", food_id=mine), MealItem(name="other", food_id=other)],
        )
    )
    assert db.backfill_meal_items(mine, carbs_g=10) == 1
    items = {i.name: i for i in db.get_meal(meal_id).items}
    assert items["mine"].carbs_g == 10
    assert items["other"].carbs_g is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")


if __name__ == "__main__":
    _run_all()
