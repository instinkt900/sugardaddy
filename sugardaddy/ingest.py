"""Background glucose ingestion.

Authenticate to the source, backfill the recent window once so a fresh install
has an immediate graph, then poll the latest reading on an interval and store it
(deduped on timestamp). The loop never dies on error — it logs, drops the
connection, and retries — so a stale token or a brief network blip just pauses
ingestion rather than taking the app down. Manual meal/dose logging is unaffected
regardless.
"""

from __future__ import annotations

import logging
import threading
import time

from sugardaddy.config import Config, load_config
from sugardaddy.db import Database
from sugardaddy.source import GlucoseSource, SourceError, build_source

log = logging.getLogger("sugardaddy.ingest")


def sync_recent(db: Database, source: GlucoseSource) -> int:
    """Pull the recent (~12h) window and store any new readings."""
    readings = source.recent()
    added = db.add_readings(readings)
    log.info("recent sync: %d fetched, %d new", len(readings), added)
    return added


def poll_latest(db: Database, source: GlucoseSource) -> bool:
    """Fetch and store the single latest reading. Returns True if it was new."""
    reading = source.latest()
    if reading is None:
        return False
    new = db.add_reading(reading)
    log.debug(
        "latest %.0f mg/dL @ %d (%s)", reading.value_mgdl, reading.ts_utc,
        "new" if new else "dup",
    )
    return new


def ingest_loop(cfg: Config, db: Database, stop: threading.Event | None = None) -> None:
    """Run the poll loop until `stop` is set (or forever if None)."""
    source = build_source(cfg.librelink)
    interval = max(15.0, cfg.librelink.poll_interval_seconds)
    did_initial_sync = False

    def sleep(seconds: float) -> bool:
        """Sleep, but wake early if asked to stop. Returns True if stopping."""
        if stop is None:
            time.sleep(seconds)
            return False
        return stop.wait(seconds)

    while True:
        try:
            if not did_initial_sync:
                sync_recent(db, source)
                did_initial_sync = True
            else:
                poll_latest(db, source)
        except SourceError as exc:
            log.warning("ingest paused: %s", exc)
            # Force a fresh connect+recent-sync on the next successful cycle.
            did_initial_sync = False
        except Exception as exc:  # never let the loop die
            log.error("unexpected ingest error: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            did_initial_sync = False

        if sleep(interval):
            log.info("ingest loop stopping")
            return


def start_background(cfg: Config, db: Database) -> tuple[threading.Thread, threading.Event]:
    """Spawn the ingest loop as a daemon thread (used by `serve`)."""
    stop = threading.Event()
    t = threading.Thread(
        target=ingest_loop, args=(cfg, db, stop), name="ingest", daemon=True
    )
    t.start()
    return t, stop


def run_ingest(config_path: str, once: bool = False) -> int:
    cfg = load_config(config_path)
    db = Database(cfg.database.path)
    db.init_db()

    if once:
        source = build_source(cfg.librelink)
        try:
            source.connect()
            added = sync_recent(db, source)
            poll_latest(db, source)
        except SourceError as exc:
            log.error("ingest failed: %s", exc)
            return 1
        log.info("done (%d readings now stored)", db.reading_count())
        return 0

    log.info("starting ingest loop (every %.0fs)", cfg.librelink.poll_interval_seconds)
    try:
        ingest_loop(cfg, db)
    except KeyboardInterrupt:
        pass
    return 0
