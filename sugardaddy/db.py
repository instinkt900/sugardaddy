"""SQLite persistence.

One file holds the glucose/insulin/meal timeline plus a reusable food library
and saved meal templates. Glucose readings dedup on their timestamp so the
poller and the backfill can both write freely. Meals are composite: a header row
plus snapshot line items, so editing a food (or a template) never rewrites
history. A thin Database wrapper hands out short-lived connections.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sugardaddy.models import (
    Food,
    GlucoseReading,
    InsulinDose,
    Meal,
    MealItem,
    MealTemplate,
    MealTemplateItem,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS glucose_readings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc     INTEGER NOT NULL UNIQUE,
    value_mgdl REAL    NOT NULL,
    trend      INTEGER,
    source     TEXT    NOT NULL DEFAULT 'librelinkup'
);
CREATE INDEX IF NOT EXISTS idx_glucose_ts ON glucose_readings(ts_utc);

CREATE TABLE IF NOT EXISTS insulin_doses (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc INTEGER NOT NULL,
    units  REAL    NOT NULL,
    kind   TEXT    NOT NULL DEFAULT 'bolus',
    note   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_insulin_ts ON insulin_doses(ts_utc);

-- Food library: reusable items, values per single unit/serving. Editing a food
-- never changes history — logging snapshots the values into meal_items.
CREATE TABLE IF NOT EXISTS foods (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    carbs_g     REAL,
    calories    REAL,
    tags        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_foods_name ON foods(name COLLATE NOCASE);

-- Logged meals: a header on the timeline...
CREATE TABLE IF NOT EXISTS meals (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc INTEGER NOT NULL,
    name   TEXT    NOT NULL DEFAULT '',
    note   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_meals_ts ON meals(ts_utc);

-- ...and its plate of snapshot items. food_id is soft provenance only.
CREATE TABLE IF NOT EXISTS meal_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    meal_id     INTEGER NOT NULL REFERENCES meals(id) ON DELETE CASCADE,
    food_id     INTEGER,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    carbs_g     REAL,
    calories    REAL,
    count       REAL    NOT NULL DEFAULT 1,
    tags        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_meal_items_meal ON meal_items(meal_id);

-- Saved meals (templates): named plates for fast logging. Items are live-linked
-- to foods (preferred at load) with a snapshot fallback for ad-hoc/deleted foods.
CREATE TABLE IF NOT EXISTS meal_templates (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meal_template_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES meal_templates(id) ON DELETE CASCADE,
    food_id     INTEGER,
    name        TEXT    NOT NULL,
    carbs_g     REAL,
    calories    REAL,
    count       REAL    NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_mti_template ON meal_template_items(template_id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_db(self) -> None:
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            # Rename the legacy `meals` aside first so _SCHEMA recreates it with
            # the new shape, then copy the old data into the new tables.
            legacy_meals = self._has_column(conn, "meals", "description")
            if legacy_meals:
                conn.execute("ALTER TABLE meals RENAME TO _legacy_meals")
            conn.executescript(_SCHEMA)
            self._migrate_legacy(conn, legacy_meals)

    # --- migration -------------------------------------------------------

    def _migrate_legacy(self, conn: sqlite3.Connection, legacy_meals: bool) -> None:
        """Copy pre-food-library data into the new tables. Idempotent: once the
        old columns/tables are gone (no legacy_meals, no known_meals) it is a
        no-op, so it runs safely on every startup."""
        if self._table_exists(conn, "known_meals"):
            # Old one-line shortcuts become entries in the food library.
            for r in conn.execute("SELECT name, carbs_g, tags FROM known_meals"):
                conn.execute(
                    "INSERT INTO foods (name, description, carbs_g, calories, tags) "
                    "VALUES (?, '', ?, NULL, ?)",
                    (r["name"], r["carbs_g"], r["tags"] or ""),
                )
            conn.execute("DROP TABLE known_meals")

        if legacy_meals:
            # Each flat meal row becomes a header + one snapshot item.
            for r in conn.execute(
                "SELECT ts_utc, carbs_g, description, tags, note FROM _legacy_meals ORDER BY id"
            ):
                cur = conn.execute(
                    "INSERT INTO meals (ts_utc, name, note) VALUES (?, ?, ?)",
                    (r["ts_utc"], r["description"] or "", r["note"] or ""),
                )
                conn.execute(
                    "INSERT INTO meal_items "
                    "(meal_id, food_id, name, description, carbs_g, calories, count, tags) "
                    "VALUES (?, NULL, ?, '', ?, NULL, 1, ?)",
                    (cur.lastrowid, r["description"] or "(meal)", r["carbs_g"], r["tags"] or ""),
                )
            conn.execute("DROP TABLE _legacy_meals")

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
        if not Database._table_exists(conn, table):
            return False
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        return column in cols

    # --- glucose ---------------------------------------------------------

    def add_reading(self, r: GlucoseReading) -> bool:
        """Insert one reading; returns True if it was new (deduped on ts_utc)."""
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO glucose_readings (ts_utc, value_mgdl, trend, source) "
                "VALUES (?, ?, ?, ?)",
                (r.ts_utc, r.value_mgdl, r.trend, r.source),
            )
            return cur.rowcount > 0

    def add_readings(self, readings: list[GlucoseReading]) -> int:
        """Bulk insert; returns the number of newly inserted rows."""
        if not readings:
            return 0
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO glucose_readings (ts_utc, value_mgdl, trend, source) "
                "VALUES (?, ?, ?, ?)",
                [(r.ts_utc, r.value_mgdl, r.trend, r.source) for r in readings],
            )
            return conn.total_changes - before

    def latest_reading(self) -> GlucoseReading | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM glucose_readings ORDER BY ts_utc DESC LIMIT 1"
            ).fetchone()
        return _reading(row) if row else None

    def readings_between(self, start: int, end: int) -> list[GlucoseReading]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM glucose_readings WHERE ts_utc BETWEEN ? AND ? ORDER BY ts_utc",
                (start, end),
            ).fetchall()
        return [_reading(r) for r in rows]

    def reading_count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM glucose_readings").fetchone()[0]

    # --- insulin ---------------------------------------------------------

    def add_dose(self, d: InsulinDose) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO insulin_doses (ts_utc, units, kind, note) VALUES (?, ?, ?, ?)",
                (d.ts_utc, d.units, d.kind, d.note),
            )
            return cur.lastrowid

    def update_dose(self, dose_id: int, **fields) -> bool:
        return self._update("insulin_doses", dose_id, fields, {"ts_utc", "units", "kind", "note"})

    def delete_dose(self, dose_id: int) -> bool:
        return self._delete("insulin_doses", dose_id)

    def doses_between(self, start: int, end: int) -> list[InsulinDose]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM insulin_doses WHERE ts_utc BETWEEN ? AND ? ORDER BY ts_utc",
                (start, end),
            ).fetchall()
        return [_dose(r) for r in rows]

    # --- foods (library) -------------------------------------------------

    def list_foods(self) -> list[Food]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM foods ORDER BY name COLLATE NOCASE").fetchall()
        return [_food(r) for r in rows]

    def get_food(self, food_id: int) -> Food | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM foods WHERE id = ?", (food_id,)).fetchone()
        return _food(row) if row else None

    def add_food(self, f: Food) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO foods (name, description, carbs_g, calories, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (f.name, f.description, f.carbs_g, f.calories, f.tags),
            )
            return cur.lastrowid

    def update_food(self, food_id: int, **fields) -> bool:
        return self._update(
            "foods", food_id, fields, {"name", "description", "carbs_g", "calories", "tags"}
        )

    def delete_food(self, food_id: int) -> bool:
        return self._delete("foods", food_id)

    # --- meals (composite: header + snapshot items) ----------------------

    def add_meal(self, m: Meal) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO meals (ts_utc, name, note) VALUES (?, ?, ?)",
                (m.ts_utc, m.name, m.note),
            )
            meal_id = cur.lastrowid
            self._insert_meal_items(conn, meal_id, m.items)
            return meal_id

    def get_meal(self, meal_id: int) -> Meal | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
            if row is None:
                return None
            items = conn.execute(
                "SELECT * FROM meal_items WHERE meal_id = ? ORDER BY id", (meal_id,)
            ).fetchall()
        meal = _meal(row)
        meal.items = [_meal_item(i) for i in items]
        return meal

    def update_meal(self, meal_id: int, *, items: list[MealItem] | None = None, **fields) -> bool:
        """Update header fields and (optionally) replace the whole plate."""
        with self.connect() as conn:
            changed = False
            cols = {k: v for k, v in fields.items() if k in {"ts_utc", "name", "note"} and v is not None}
            if cols:
                assignments = ", ".join(f"{k} = ?" for k in cols)
                cur = conn.execute(
                    f"UPDATE meals SET {assignments} WHERE id = ?", (*cols.values(), meal_id)
                )
                changed = cur.rowcount > 0
            else:
                changed = conn.execute("SELECT 1 FROM meals WHERE id = ?", (meal_id,)).fetchone() is not None
            if items is not None and changed:
                conn.execute("DELETE FROM meal_items WHERE meal_id = ?", (meal_id,))
                self._insert_meal_items(conn, meal_id, items)
            return changed

    def delete_meal(self, meal_id: int) -> bool:
        # meal_items cascade via the FK (PRAGMA foreign_keys=ON).
        return self._delete("meals", meal_id)

    def meals_between(self, start: int, end: int) -> list[Meal]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM meals WHERE ts_utc BETWEEN ? AND ? ORDER BY ts_utc",
                (start, end),
            ).fetchall()
            meals = [_meal(r) for r in rows]
            by_id = {m.id: m for m in meals}
            if by_id:
                placeholders = ",".join("?" * len(by_id))
                items = conn.execute(
                    f"SELECT * FROM meal_items WHERE meal_id IN ({placeholders}) ORDER BY id",
                    tuple(by_id),
                ).fetchall()
                for i in items:
                    by_id[i["meal_id"]].items.append(_meal_item(i))
        return meals

    @staticmethod
    def _insert_meal_items(conn: sqlite3.Connection, meal_id: int, items: list[MealItem]) -> None:
        conn.executemany(
            "INSERT INTO meal_items "
            "(meal_id, food_id, name, description, carbs_g, calories, count, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (meal_id, i.food_id, i.name, i.description, i.carbs_g, i.calories, i.count, i.tags)
                for i in items
            ],
        )

    # --- meal templates (saved meals) ------------------------------------

    def list_meal_templates(self) -> list[MealTemplate]:
        """Templates with items resolved against the live food library: a food's
        current name/carbs/calories win, falling back to the stored snapshot when
        the food was deleted (or the item was ad-hoc)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM meal_templates ORDER BY name COLLATE NOCASE"
            ).fetchall()
            templates = [MealTemplate(id=r["id"], name=r["name"], items=[]) for r in rows]
            by_id = {t.id: t for t in templates}
            if by_id:
                placeholders = ",".join("?" * len(by_id))
                items = conn.execute(
                    f"""
                    SELECT ti.id, ti.template_id, ti.food_id, ti.count,
                           COALESCE(f.name, ti.name)         AS name,
                           COALESCE(f.carbs_g, ti.carbs_g)   AS carbs_g,
                           COALESCE(f.calories, ti.calories) AS calories,
                           (f.id IS NOT NULL)                AS food_live
                    FROM meal_template_items ti
                    LEFT JOIN foods f ON f.id = ti.food_id
                    WHERE ti.template_id IN ({placeholders})
                    ORDER BY ti.id
                    """,
                    tuple(by_id),
                ).fetchall()
                for i in items:
                    by_id[i["template_id"]].items.append(
                        MealTemplateItem(
                            id=i["id"],
                            name=i["name"],
                            count=i["count"],
                            carbs_g=i["carbs_g"],
                            calories=i["calories"],
                            # Drop a dangling food_id so the client treats it as ad-hoc.
                            food_id=i["food_id"] if i["food_live"] else None,
                        )
                    )
        return templates

    def add_meal_template(self, t: MealTemplate) -> int:
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO meal_templates (name) VALUES (?)", (t.name,))
            template_id = cur.lastrowid
            self._insert_template_items(conn, template_id, t.items)
            return template_id

    def update_meal_template(
        self, template_id: int, *, name: str | None = None, items: list[MealTemplateItem] | None = None
    ) -> bool:
        with self.connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM meal_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if not exists:
                return False
            if name is not None and name.strip():
                conn.execute(
                    "UPDATE meal_templates SET name = ? WHERE id = ?", (name.strip(), template_id)
                )
            if items is not None:
                conn.execute(
                    "DELETE FROM meal_template_items WHERE template_id = ?", (template_id,)
                )
                self._insert_template_items(conn, template_id, items)
            return True

    def delete_meal_template(self, template_id: int) -> bool:
        return self._delete("meal_templates", template_id)

    @staticmethod
    def _insert_template_items(
        conn: sqlite3.Connection, template_id: int, items: list[MealTemplateItem]
    ) -> None:
        conn.executemany(
            "INSERT INTO meal_template_items "
            "(template_id, food_id, name, carbs_g, calories, count) VALUES (?, ?, ?, ?, ?, ?)",
            [(template_id, i.food_id, i.name, i.carbs_g, i.calories, i.count) for i in items],
        )

    # --- generic helpers -------------------------------------------------

    def _update(self, table: str, row_id: int, fields: dict, allowed: set[str]) -> bool:
        cols = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not cols:
            return False
        assignments = ", ".join(f"{k} = ?" for k in cols)
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?",
                (*cols.values(), row_id),
            )
            return cur.rowcount > 0

    def _delete(self, table: str, row_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
            return cur.rowcount > 0


def _reading(row: sqlite3.Row) -> GlucoseReading:
    return GlucoseReading(
        id=row["id"],
        ts_utc=row["ts_utc"],
        value_mgdl=row["value_mgdl"],
        trend=row["trend"],
        source=row["source"],
    )


def _dose(row: sqlite3.Row) -> InsulinDose:
    return InsulinDose(
        id=row["id"],
        ts_utc=row["ts_utc"],
        units=row["units"],
        kind=row["kind"],
        note=row["note"],
    )


def _food(row: sqlite3.Row) -> Food:
    return Food(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        carbs_g=row["carbs_g"],
        calories=row["calories"],
        tags=row["tags"],
    )


def _meal(row: sqlite3.Row) -> Meal:
    return Meal(id=row["id"], ts_utc=row["ts_utc"], name=row["name"], note=row["note"], items=[])


def _meal_item(row: sqlite3.Row) -> MealItem:
    return MealItem(
        id=row["id"],
        food_id=row["food_id"],
        name=row["name"],
        description=row["description"],
        carbs_g=row["carbs_g"],
        calories=row["calories"],
        count=row["count"],
        tags=row["tags"],
    )
