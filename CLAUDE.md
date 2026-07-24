# CLAUDE.md — Sugar Daddy

Context for working on this repo. Product **display name** is "Sugar Daddy"; the
**code/package/CLI/container** name is `sugardaddy` (don't rename those).

## Purpose

A small, self-contained app for someone using a **FreeStyle Libre** CGM. It:
- ingests glucose readings over time **directly from LibreLinkUp** (no Home
  Assistant needed at runtime — the AU region is the live setup),
- lets the user log **insulin doses** and **meals** from a phone web UI,
- provides a **desktop dashboard** to review glucose/insulin/meals on one
  timeline, and
- offers a retrospective **`report`** (CLI/JSON) plus a Claude **review skill**.

**Not a medical device.** It is for personal record-keeping and *retrospective*
analysis to discuss with a clinician — never real-time dosing decisions. Keep
this framing in any user-facing text and analysis output. See
`docs/plans/insulin-awareness.md` for where future dosing-*awareness* features
are allowed to go and the hard safety boundary around them.

## Stack

- Python **3.11+**. Web: **FastAPI + Jinja2 + HTMX + Chart.js**. Storage:
  **SQLite** (stdlib `sqlite3`, WAL). Glucose source: **pylibrelinkup**.
- Server runs under **uvicorn**; the phone UI is HTMX-driven with some vanilla JS,
  the desktop UI is heavier vanilla JS. No frontend build step — static assets
  (htmx, chart.js) are vendored in `sugardaddy/static/`.
- No test framework is assumed: tests are **plain-assert** files runnable with
  bare `python` (see `tests/test_report_analysis.py`), also pytest-compatible.

## Layout

```
sugardaddy/            Python package
  cli.py               entrypoint: serve | ingest | backfill | init-db | report
  __main__.py          `python -m sugardaddy`
  web.py               FastAPI app factory, all routes, request parsing, tz helpers
  db.py                SQLite schema + Database wrapper (short-lived connections)
  models.py            dataclasses: GlucoseReading, InsulinDose, Food, Meal(+Item), MealTemplate(+Item)
  config.py            TOML loader; dataclasses; _known() rejects unknown keys; secrets from env only
  constants.py         units + GMI helpers; mg/dL <-> mmol/L; default target band (3.9–10.0 mmol/L)
  ingest.py            background poller (authenticate, backfill recent window once, then poll)
  source.py            GlucoseSource seam (keeps app independent of LibreLinkUp specifics)
  backfill.py          one-shot history seed from Home Assistant REST
  analysis.py          PURE retrospective functions (summarize, post_meal_responses,
                       variability, daily_breakdown, hourly_profile, low_episodes,
                       insulin_summary, carb_coverage) — no I/O, no clock, no config
  report.py            `report` command: window + tz resolution, calls analysis, text/JSON
  templates/           base.html, phone/index.html, desktop/dashboard.html, partials/recent.html
  static/              style.css, phone.js, desktop.js, common.js, sw.js, vendored libs, icons/
docker/                Dockerfile, docker-compose.yml
deploy/                install-server.sh, install-skill.sh, skills/sugardaddy-review/
docs/plans/            design docs (insulin-awareness.md)
tests/                 plain-assert tests
config.example.toml    the only tracked config; real config.toml is gitignored
```

## Data model conventions (important)

- **Timestamps are UTC epoch seconds (int) everywhere internally.** Display-time
  conversion to the configured timezone happens only in the web layer.
- **Glucose is stored in mg/dL**; converted to the display unit (mmol/L for AU)
  at the edges via `constants.to_display`. Deltas via `_delta_display`.
- **Meals are composite**: a header row + snapshot `meal_items`. Editing a `food`
  in the library never rewrites history (items are snapshots; `food_id` is soft
  provenance only). Same for meal templates.
- **Insulin `kind`** is `bolus | correction | basal`. For any insulin-on-board
  math, include only bolus+correction (rapid-acting); basal is a separate depot.

## Web routes (all defined in `web.py`)

- Pages: `GET /` (phone), `GET /desktop`, `GET /healthz`, `GET /manifest.webmanifest`, `GET /sw.js`
- Read APIs: `GET /api/{current,timeline,entries,stats,recent,foods,meal-templates}`
- Write APIs: `POST /api/{insulin,meal,foods,meal-templates}`;
  `PATCH`/`DELETE /api/{insulin,meal,foods,meal-templates}/{id}`
- The service worker (`sw.js`) is **network-first**, so code/template/static
  changes roll out on reload once deployed.

## Commands

```
sugardaddy serve    -c config.toml            # web app + glucose poller (uvicorn)
sugardaddy ingest   -c config.toml [--once]   # poller only
sugardaddy backfill -c config.toml --days 90  # one-time HA history seed
sugardaddy init-db  -c config.toml            # create schema and exit
sugardaddy report   -c config.toml [--days N] [--db PATH] [--json]
```

`report` is deterministic analysis only (TIR/GMI, variability/CV, per-day and
per-hour breakdowns, grouped low episodes, insulin summary, carb coverage,
post-meal responses). `--db` overrides the config's DB path so a copied DB can be
analysed off-box; units/targets/tz still come from the config.

## Configuration

One TOML (`config.toml`; template is `config.example.toml`). Sections:
`[librelink]`, `[database]`, `[web]` (host/port/timezone/units/target_low/high),
`[backfill]`. `config.py` uses dataclasses and `_known()` so an **unknown TOML
key fails loudly**. **Secrets are never in the TOML** — they come from env only:
`SUGARDADDY_LIBRE_EMAIL`, `SUGARDADDY_LIBRE_PASSWORD`, `SUGARDADDY_HA_TOKEN`
(backfill only). Storage is always mg/dL; `units` only affects display.

## Deployment

Docker on a self-hosted serve host (behind a trusted LAN/VPN; **do not put the
hostname/IP in the repo**). The image **bakes code in via `COPY`**, so any
code/template/static change needs an **image rebuild** — a restart alone won't
pick it up.

- Compose project + container + image are all named `sugardaddy`. Container
  listens on **8080 internally**; host port is `${SUGARDADDY_PORT:-8080}` from
  `docker/.env`. Data is a named volume `sugardaddy-data` mounted at `/data`;
  `config.toml` is bind-mounted read-only. Healthcheck hits `/healthz`.
- Deploy flow (run on the host, in the repo clone):
  ```
  git pull --ff-only && cd docker && docker compose up -d --build
  ```
  `deploy/install-server.sh` wraps this.
- **Back up the DB before any schema-changing deploy** (`docker cp
  sugardaddy:/data/sugardaddy.db <backup>`); schema migrations live in
  `db.init_db()` and run on startup.
- Verify after deploy: `curl :<port>/healthz` → `{"status":"ok","readings":N}`
  and container `Up (healthy)`.

## The review skill

`deploy/skills/sugardaddy-review/` is a Claude Code skill (installable per-machine
via `deploy/install-skill.sh` into `~/.claude/skills/`). It fetches the live DB
off the serve host, runs `report --json`, and writes a management-focused review
with trend-vs-last-run. Key boundaries:
- The **serve host/paths live only in a machine-local `connection.env`** (seeded
  from the tracked `connection.env.example`) — never committed.
- The review **`history/`** (contains glucose data) stays under `~/.claude`, never
  in the repo. So trends are per-machine.

## Goals / roadmap

- Near-term direction is in `docs/plans/insulin-awareness.md`: **"de-vibe the
  dose"** — calculate a concrete anchor (active IOB + trajectory nudge) to
  reconcile a dose against, shipping first as a *non-prescriptive* awareness
  prompt; a full IOB/ISF/ICR **bolus calculator** is an explicit *experimental
  stretch goal* (a cross-check to be questioned, never a directive).
- README "Roadmap / ideas": wearable/activity data (via Health Connect → HA),
  trend analysis & prediction, alerts, export.
- **Improving carb logging** is the recurring data-hygiene lever — most meals lack
  carbs, which blocks any carb-ratio analysis; call it out in reviews.

## Conventions & guardrails

- **Match surrounding code style** (naming, comment density, the "explain the
  *why*" docstring tone already in the package). Keep `analysis.py` functions
  pure and testable; put new deterministic maths there and cover it in `tests/`.
- **No internal network details in the repo.** Use RFC 5737 placeholders
  (`192.0.2.x`) in examples. **Leak-scan every commit** for internal
  hostnames/IPs before pushing.
- **Secrets are gitignored** (`docker/.env`, `docker/config.toml`, `config.toml`,
  `*.db`, `connection.env`); only `*.example` files are tracked. Never type the
  LibreLinkUp password into a shell (bash history).
- Commit to `master` (the serve host pulls it); keep commits scoped and
  leak-scanned. This is a personal project — deploy when the user asks.
