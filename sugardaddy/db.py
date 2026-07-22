"""SQLite persistence.

One file holds three tables on a shared UTC timeline. Glucose readings dedup on
their timestamp so the poller and the backfill can both write freely. A thin
Database wrapper hands out short-lived connections; SQLite handles the rest.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sugardaddy.models import GlucoseReading, InsulinDose, Meal

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

CREATE TABLE IF NOT EXISTS meals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc      INTEGER NOT NULL,
    carbs_g     REAL,
    description TEXT    NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '',
    note        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_meals_ts ON meals(ts_utc);
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
            conn.executescript(_SCHEMA)

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

    # --- meals -----------------------------------------------------------

    def add_meal(self, m: Meal) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO meals (ts_utc, carbs_g, description, tags, note) VALUES (?, ?, ?, ?, ?)",
                (m.ts_utc, m.carbs_g, m.description, m.tags, m.note),
            )
            return cur.lastrowid

    def update_meal(self, meal_id: int, **fields) -> bool:
        return self._update(
            "meals", meal_id, fields, {"ts_utc", "carbs_g", "description", "tags", "note"}
        )

    def delete_meal(self, meal_id: int) -> bool:
        return self._delete("meals", meal_id)

    def meals_between(self, start: int, end: int) -> list[Meal]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM meals WHERE ts_utc BETWEEN ? AND ? ORDER BY ts_utc",
                (start, end),
            ).fetchall()
        return [_meal(r) for r in rows]

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


def _meal(row: sqlite3.Row) -> Meal:
    return Meal(
        id=row["id"],
        ts_utc=row["ts_utc"],
        carbs_g=row["carbs_g"],
        description=row["description"],
        tags=row["tags"],
        note=row["note"],
    )
