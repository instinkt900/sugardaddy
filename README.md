# sugardaddy

A small, self-contained app for people using a **FreeStyle Libre** CGM: it
ingests your glucose readings over time, lets you log **insulin doses** and
**meals** from your phone, and gives you a desktop dashboard to review it all on
one timeline.

It talks **directly to LibreLinkUp** (the same sharing service the Home Assistant
Libre integration uses under the hood), so it needs no Home Assistant at runtime.
Everything runs on your own infrastructure.

> **Not a medical device.** sugardaddy is for personal record-keeping and
> *retrospective* analysis — spotting patterns to discuss with your care team. It
> must **not** be used for real-time dosing decisions.

## What it does

- **Ingests glucose** from LibreLinkUp on an interval and stores every reading
  (deduplicated) in a local SQLite database.
- **Two web UIs, one backend:**
  - **Phone** (`/`) — input-first: current reading + trend, a compact 24h chart,
    and fast one-tap logging of insulin and meals. The meal name field is a
    searchable dropdown of your recent + saved meals that prefills carbs and tags.
  - **Desktop** (`/desktop`) — review-first: a large interactive chart (glucose
    line + target band + dose/meal markers) with date-range selection, sortable
    tables with inline **add / edit / delete**, and analysis panels.
- **Known meals** — a library of reusable meal shortcuts (name + carbs + tags)
  for fast logging. Fully decoupled from history: logging copies the values into
  a snapshot, so editing or deleting a shortcut never changes entries you've
  already logged. Manage them on the desktop; use them from the phone.
- **Analyses** the timeline: time-in-range, average glucose + estimated GMI,
  high/low counts, and the 2-hour glucose response after each logged meal.
- **Seeds history** once from an existing Home Assistant install (optional), so
  your charts have depth from day one.

## How it works

```
        LibreLinkUp (Abbott)                     your LAN / VPN
   ┌───────────────────────────┐        ┌──────────────────────────────┐
   │ glucose readings (~5 min)  │ HTTPS  │ sugardaddy serve (Docker)     │
   │  · latest() + graph()      │ ─────► │  · poll → SQLite (dedup)      │
   └───────────────────────────┘        │  · FastAPI: phone + desktop   │
                                         │  · JSON API + analysis        │
   ┌───────────────────────────┐        │                               │
   │ Home Assistant (optional)  │  REST  │  phone:   http://host:8080/   │
   │  · recorder history        │ ──────►│  desktop: http://host:8080/…  │
   │  · ONE-TIME backfill only  │  seed  └──────────────────────────────┘
   └───────────────────────────┘
```

- The glucose source is behind a small `GlucoseSource` interface; LibreLinkUp is
  the default. Home Assistant is used *only* for the one-time backfill.
- Credentials live in the environment (Docker `.env`), never in the committed
  config or the database.
- Access is **LAN/VPN-only with no auth** by design — it trusts the network it
  binds to. Don't expose it directly to the internet.

## Example setup

Addresses below are placeholders (RFC 5737 documentation IPs) — substitute your
own. A flat LAN (optionally reached over a VPN when away) is all you need.

| piece | example | notes |
|-------|---------|-------|
| serve host | `192.0.2.20:8080` | any Docker-capable box on your LAN |
| Home Assistant | `192.0.2.10:8123` | optional, for the one-time history seed only |
| your phone / PC | — | on the LAN or VPN; open the URLs above |

## Repo layout

```
sugardaddy/
  cli.py         entrypoint: serve | ingest | backfill | init-db
  config.py      one TOML → typed config (secrets from env, never in TOML)
  constants.py   unit conversion, trend arrows, default target range
  models.py      typed rows (readings, doses, meals, known meals)
  db.py          SQLite schema + queries (UTC epoch, dedup on ts)
  source.py      GlucoseSource ABC + LibreLinkUpSource (pylibrelinkup)
  ingest.py      background poll loop (auth → latest()/graph() → store)
  backfill.py    one-shot HA history REST import
  analysis.py    time-in-range, GMI, high/low counts, post-meal response
  web.py         FastAPI app: phone + desktop routes, JSON API, /healthz
  templates/     Jinja: base, phone/, desktop/, partials/
  static/        vendored htmx + Chart.js, CSS, phone.js, desktop.js
config.example.toml   the whole app in one file
docker/               Dockerfile + compose + .env.example
deploy/install-server.sh
```

## Commands

Run via `sugardaddy <command>` (installed) or `python -m sugardaddy <command>`.

```bash
sugardaddy serve    -c config.toml            # web app + glucose poller
sugardaddy ingest   -c config.toml [--once]   # poller only (--once = sync + exit)
sugardaddy backfill -c config.toml --days 90  # one-time HA history seed
sugardaddy init-db  -c config.toml            # create the DB schema and exit
```
Add `-v` for debug logging.

## Setup — serve side (Docker)

1. Configure and add secrets:
   ```bash
   cp config.example.toml docker/config.toml   # edit [librelink].region, [web] tz/units
   cp docker/.env.example docker/.env          # LibreLinkUp email + password
   ```
   Use the **LibreLinkUp** account credentials (the follower account that already
   has access to your Libre data).
2. Build and run:
   ```bash
   bash deploy/install-server.sh          # or: cd docker && docker compose up -d --build
   ```
3. Verify, then open the UIs:
   ```bash
   curl -s http://localhost:8080/healthz
   ```
   Phone: `http://<host>:8080/` · Desktop: `http://<host>:8080/desktop`

## Setup — seed history from Home Assistant (optional, one-time)

If HA already holds months of Libre history, import it so the charts start deep:

1. Create a long-lived token in HA: **Profile → Security → Long-lived access tokens**.
2. Set `[backfill].ha_url` and `ha_entity` in `config.toml`, put the token in
   `docker/.env` as `SUGARDADDY_HA_TOKEN`.
3. Run it once inside the container:
   ```bash
   cd docker && docker compose run --rm sugardaddy backfill -c /app/config.toml --days 180
   ```
   HA stores AU sensors in mmol/L (converted to mg/dL on import); pass
   `--unit mg/dL` if your HA sensor is already in mg/dL.

## Common operations

**Change credentials** — edit `docker/.env`, then **recreate** the container (a
plain `restart` keeps the old env):
```bash
cd docker && docker compose up -d --force-recreate
```

**Redeploy code changes:**
```bash
cd docker && docker compose up -d --build
```

**Watch logs / back up the DB:**
```bash
docker compose logs -f
docker compose cp sugardaddy:/data/sugardaddy.db ./backup.db
```

## Configuration reference

`[librelink]`
| key | default | meaning |
|-----|---------|---------|
| `region` | `AU` | pylibrelinkup region (AU, EU, US, …) |
| `poll_interval_seconds` | 60 | seconds between latest-reading polls (min 15) |
| `patient_id` | — | only if the account follows more than one person |

Credentials: `SUGARDADDY_LIBRE_EMAIL` / `SUGARDADDY_LIBRE_PASSWORD` (env only).

`[database]`
| key | default | meaning |
|-----|---------|---------|
| `path` | `/data/sugardaddy.db` | SQLite file location |

`[web]`
| key | default | meaning |
|-----|---------|---------|
| `host` / `port` | `0.0.0.0` / 8080 | HTTP bind |
| `timezone` | `Australia/Sydney` | display tz (storage is UTC) |
| `units` | `mmol/L` | display unit (`mmol/L` or `mg/dL`); storage is mg/dL |
| `target_low` / `target_high` | 3.9 / 10.0 | time-in-range band, in display units |

`[backfill]` (one-time HA seed only)
| key | default | meaning |
|-----|---------|---------|
| `ha_url` | — | Home Assistant base URL |
| `ha_entity` | — | the glucose sensor entity id |

Token: `SUGARDADDY_HA_TOKEN` (env only).

## Roadmap / ideas

Not built yet — captured here so the direction is clear. All of these sit on top
of the same UTC timeline, so they're additive rather than rewrites.

- **Activity & wearable data (steps, heart rate, workouts).** Bring in
  Samsung Health / Galaxy Watch metrics to enrich analysis. Two feeds:
  - *One-time history* — import a Samsung Health data export (the CSVs it
    produces: daily steps, heart rate, exercise) via a `sugardaddy import-samsung`
    command. Good for deep retrospective history; not something to repeat weekly.
  - *Ongoing* — Samsung Health already syncs into Android **Health Connect**, and
    the Home Assistant Android companion app can expose Health Connect metrics as
    HA sensors. sugardaddy would then pull activity from HA the same way it can
    seed glucose from HA — no custom phone app, no Samsung developer approval.
  - Storage would be a small `activity` table (steps, heart-rate readings,
    workouts) on the shared timeline, joinable against glucose/meals/insulin.
- **Trend analysis & prediction.** With glucose + insulin + meals (+ activity)
  on one timeline, learn per-meal/per-time-of-day response patterns and surface
  likely highs/lows. Strictly decision-support for review with a clinician — not
  dosing advice.
- **Nice-to-haves.** Alerts/notifications on sustained highs/lows; optional auth
  if ever exposed beyond a trusted LAN/VPN; photo attachments on meals; CSV/export
  of the combined timeline.

Anything requiring live wearable data depends on the Health Connect → HA feed
above; the manual export path only makes sense as a one-off seed.

## Notes & limitations

- **LibreLinkUp is unofficial/reverse-engineered.** Abbott occasionally bumps a
  required app-version header, which can pause ingestion until `pylibrelinkup` is
  updated. Manual meal/dose logging is unaffected. Pin and update the dependency.
- Glucose granularity is whatever LibreLinkUp reports (~5 min); this is not
  real-time.
- Long-term history accumulates from first run (plus the optional HA seed); the
  live API only exposes ~12h (`graph`) to ~14 days (`logbook`).
