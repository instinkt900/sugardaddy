"""Single entrypoint for the app.

  sugardaddy serve     — run the web app (phone + desktop) + glucose poller
  sugardaddy ingest    — run only the glucose poller (or --once)
  sugardaddy backfill  — one-shot import of history from Home Assistant
  sugardaddy init-db   — create the SQLite schema and exit
  sugardaddy report    — print a retrospective analysis (text or --json)
"""

from __future__ import annotations

import argparse
import logging
import sys

from sugardaddy import __version__


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sugardaddy", description=__doc__)
    parser.add_argument("--version", action="version", version=f"sugardaddy {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the web app + glucose poller")
    p_serve.add_argument("-c", "--config", required=True, help="path to config.toml")

    p_ingest = sub.add_parser("ingest", help="run only the glucose poller")
    p_ingest.add_argument("-c", "--config", required=True, help="path to config.toml")
    p_ingest.add_argument("--once", action="store_true", help="sync once and exit")

    p_bf = sub.add_parser("backfill", help="one-shot history import from Home Assistant")
    p_bf.add_argument("-c", "--config", required=True, help="path to config.toml")
    p_bf.add_argument("--days", type=int, default=90, help="how many days back to import")
    p_bf.add_argument("--unit", default="", help="HA sensor unit: 'mmol/L' or 'mg/dL' (default: config units)")

    p_init = sub.add_parser("init-db", help="create the SQLite schema and exit")
    p_init.add_argument("-c", "--config", required=True, help="path to config.toml")

    p_report = sub.add_parser("report", help="print a retrospective analysis (text or --json)")
    p_report.add_argument("-c", "--config", required=True, help="path to config.toml (units/targets/tz)")
    p_report.add_argument("--db", default="", help="analyse this DB file instead of the one in the config")
    p_report.add_argument("--days", type=int, default=14, help="window size in days (default: 14)")
    p_report.add_argument("--json", action="store_true", help="emit JSON instead of text")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "serve":
        from sugardaddy.web import run_serve

        return run_serve(args.config)
    if args.command == "ingest":
        from sugardaddy.ingest import run_ingest

        return run_ingest(args.config, once=args.once)
    if args.command == "backfill":
        from sugardaddy.backfill import run_backfill

        return run_backfill(args.config, days=args.days, unit=args.unit)
    if args.command == "init-db":
        from sugardaddy.config import load_config
        from sugardaddy.db import Database

        cfg = load_config(args.config)
        Database(cfg.database.path).init_db()
        print(f"initialised {cfg.database.path}")
        return 0
    if args.command == "report":
        from sugardaddy.report import run_report

        return run_report(args.config, db_path=args.db, days=args.days, as_json=args.json)

    parser.print_help(sys.stderr)
    return 2
