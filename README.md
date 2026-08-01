# Sugar Daddy

Sugar Daddy is a small self-hosted app for people who wear a **FreeStyle Libre**
continuous glucose monitor (CGM). It collects glucose readings over time. It also
records insulin doses, meals, and free-text notes. A desktop dashboard shows all
of it on one timeline.

The app reads glucose directly from **LibreLinkUp**, the Abbott sharing service.
It needs no Home Assistant at runtime. It runs on private hardware and keeps
every reading in a local SQLite file.

> ## ⚠️ Not a medical device
>
> Sugar Daddy keeps personal records and supports **retrospective** review. It is
> **not a medical device**. It gives **no medical advice**.
>
> Every number, chart, and report is **a source of data, not guidance**. The app
> describes what the record holds. It never states what to do about it.
>
> Do not use it for real-time treatment decisions. Take the patterns it shows to
> a qualified clinician, and let that person decide what they mean.

---

## Features

**Glucose ingest.** The app polls LibreLinkUp on a set interval. It writes each
reading to SQLite and drops duplicates. Storage is always UTC and mg/dL. The
display converts to the configured timezone and unit.

**Phone interface (`/`).** This screen is built for input. It shows the current
reading, the trend arrow, the active insulin, and where that insulin sits on its
action curve. Below sits a compact 12-hour chart and three log forms:

- **Insulin** — units, type (bolus, correction, or basal), time, optional note.
- **Meal** — the plate builder, described below.
- **Note** — free text and a time.

**Desktop interface (`/desktop`).** This screen is built for review. A large
chart plots the glucose line, the target band, the active-insulin curve, and
markers for doses, meals, and notes. Any date range works. Tables below the chart
add, edit, and delete rows in place. This screen also manages the food library
and the saved meals.

The bold chart line is **smoothed glucose**. The app rejects outliers, then
averages over about half an hour. This removes the sensor wobble described under
[Known limitations](#known-limitations). The raw **sensor readings** stay behind
it in faint gray, because those are the real measurements. The legend toggles
either line. The "now" badge always reads the latest real reading.

**Composite meals.** A meal is a plate of **foods**, each with a count. One
sandwich, one juice, and two biscuits is a single meal. The app totals the carbs
and the calories.

**Food library.** Foods are reusable records with a name, a description, carbs,
and calories. Names are unique and ignore case. Saving a name again updates the
food that already holds it. Every device sees the same library.

**Saved meals.** These are named plates for fast logging. A saved meal links live
to the food library, so a correction to a food reaches every saved meal that uses
it. Logged history is a snapshot instead. Editing or deleting a food never
changes a meal that was already logged.

**Context notes.** A note is a time and some words. It captures what the other
records cannot: illness, exercise, travel, a bad night. Notes carry no structure
and no categories, because a form that asks for a classification first is a form
people stop filling in. Notes appear on the desktop chart, in the tables, and in
the reports, next to the day they explain.

**Analysis.** The app measures time in range, average glucose, estimated GMI, and
high and low counts. It also measures the 2-hour glucose response after each
logged meal.

**Daily intake table** (desktop). This table shows average glucose, carbs,
calories, and mealtime insulin per day, plus a per-day average. It excludes basal
from the insulin total, because one long-acting dose would hide the mealtime
doses. A `*` marks a day where some meals lack the figure, so the total is a
floor and not a fact. The same mark appears when the sensor did not cover the
whole day. This table keeps **its own range** (3, 7, or 30 days, or a typed day
count), separate from the chart. Days start at local midnight, so no row is a
part day. Today carries a **so far** tag and stays out of the average.

**Prescription record.** The config holds what a clinician prescribed, with the
date of the last review. The app never feeds these values into a calculation. It
reports logged basal doses against the prescribed dose. A report can then state
"prescribed 36 u, logged on 6 of 7 days", and express no opinion about the dose
itself.

**Basal reminder.** The app can send one kind of notification. It reports that no
**basal** dose has been logged for more than a day. It describes a gap in the
log. It never asks for a dose. This feature is off by default. See
[Notifications](#notifications).

**Experimental bolus reference.** When the config holds an insulin sensitivity
factor, the app runs a textbook formula against doses that were already decided.
It shows the result on the phone meal form, in the desktop post-meal table, and
in `report`. The formula is the thing under test, not the person dosing. It is
off unless someone sets `isf`. See
[`docs/plans/insulin-awareness.md`](docs/plans/insulin-awareness.md) for the full
reasoning and the safety boundary.

**History seed.** A one-time import from an existing Home Assistant install gives
the charts depth from the first day. This step is optional.

## How it works

```
   LibreLinkUp (Abbott)                  Home Assistant (optional)
   readings, about every 5 minutes       months of past readings
              │                                     │
              │ HTTPS poll                          │ REST, one-time seed
              ▼                                     ▼
   ┌──────────────────────────────────────────────────────────┐
   │  sugardaddy serve  (Docker, on a private LAN or VPN)     │
   │                                                          │
   │  poll ──► SQLite (UTC, mg/dL, deduplicated)              │
   │  FastAPI ──► phone UI  ·  desktop UI  ·  JSON API        │
   │  analysis ──► report (text or JSON), basal reminder      │
   └──────────────────────────────────────────────────────────┘
              │                                     │
              ▼                                     ▼
      phone browser (log)                  desktop browser (review)
```

- A small `GlucoseSource` interface hides where the readings come from.
  LibreLinkUp is the default. Home Assistant serves the one-time backfill only.
- Credentials live in the environment, through the Docker `.env` file. They never
  reach the tracked config or the database.
- The app has no authentication by design. It trusts the network it binds to.
  Run it on a LAN or a VPN. Do not expose it to the internet.

### Example network

The addresses below are RFC 5737 placeholders. Substitute real ones. A flat LAN
is enough, and a VPN covers access from outside.

| piece | example | notes |
|-------|---------|-------|
| serve host | `192.0.2.20:<port>` | any Docker-capable box on the LAN |
| Home Assistant | `192.0.2.10:8123` | optional, for the one-time history seed |
| phone or PC | — | on the LAN or the VPN, and opens the URLs above |

## Install and run (Docker)

1. Copy the config files and add the secrets:
   ```bash
   cp config.example.toml config.toml   # edit [librelink].region, [web] tz/units
   cp docker/.env.example docker/.env   # LibreLinkUp email and password
   ```
   Use the **LibreLinkUp** account credentials. That is the follower account with
   access to the Libre data.
2. Build the image and start the container:
   ```bash
   bash deploy/install-server.sh          # or: cd docker && docker compose up -d --build
   ```
3. Check that the app answers. The container always listens on 8080 inside, but
   `SUGARDADDY_PORT` sets the host port. Ask Docker for it rather than assuming
   it, or the check may reach a different service:
   ```bash
   cd docker && docker compose port sugardaddy 8080   # -> 0.0.0.0:<host-port>
   curl -s http://localhost:<host-port>/healthz       # -> {"status":"ok","readings":N}
   ```
   `install-server.sh` resolves the port and runs this check on its own. A reply
   without a `readings` count comes from something else on that port.
4. Open the interfaces. Phone: `http://<host>:<host-port>/`. Desktop:
   `http://<host>:<host-port>/desktop`.

The image bakes the code in with `COPY`. Any change to code, templates, or static
files needs a rebuild. A plain restart keeps the old build.

## Optional: seed history from Home Assistant

If Home Assistant already holds months of Libre history, import it once.

1. In Home Assistant, create a long-lived token under **Profile → Security →
   Long-lived access tokens**.
2. Set `[backfill].ha_url` and `ha_entity` in `config.toml`.
3. Put the token in `docker/.env` as `SUGARDADDY_HA_TOKEN`.
4. Run the backfill once, inside the container:
   ```bash
   cd docker && docker compose run --rm sugardaddy backfill -c /app/config.toml --days 180
   ```

Home Assistant stores Australian sensors in mmol/L, and the import converts them
to mg/dL. Pass `--unit mg/dL` when the sensor already reports mg/dL.

## Command line

Run `sugardaddy <command>` after a package install. Otherwise run
`python -m sugardaddy <command>`.

```bash
sugardaddy serve    -c config.toml            # web app + glucose poller
sugardaddy ingest   -c config.toml [--once]   # poller only (--once = sync, then exit)
sugardaddy backfill -c config.toml --days 90  # one-time Home Assistant history seed
sugardaddy init-db  -c config.toml            # create the SQLite schema, then exit
sugardaddy report   -c config.toml [--days N] # retrospective analysis (text or --json)
sugardaddy notify   -c config.toml [--dry-run] # push the basal reminder, if one is due
sugardaddy vapid-keys                         # mint a Web Push signing key
```

Add `-v` for debug logs.

### `report` — retrospective analysis

`report` reads the stored timeline over a window. The default window is 14 days.
It covers:

- time in range, average glucose, and estimated GMI
- glucose variability, as SD and CV
- a breakdown per day and per hour of the day
- context notes, grouped by day and printed ahead of the numbers
- discrete low episodes, where runs of below-range readings collapse into single
  events
- an insulin dose summary by kind
- basal doses per day, against the recorded prescription
- carb-logging coverage
- the 2-hour glucose response after each logged meal
- the experimental bolus reference, when the config holds an `isf`

The command crunches numbers. It makes no clinical judgment. Notes are the one
qualitative part, and the app prints them exactly as they were written.

```bash
sugardaddy report -c config.toml --days 7           # human-readable text
sugardaddy report -c config.toml --days 30 --json   # machine-readable JSON
sugardaddy report -c config.toml --db /tmp/copy.db  # analyze a different DB file
```

`--db` overrides the database path from the config. Units, target range, and
timezone still come from the config. This flag suits a copy of the live database,
pulled off the serve host for analysis somewhere else.

## Notifications

The app sends **one** kind of notification. It reports that no **basal** dose has
been logged for more than a day.

Basal is the dose that is easy to forget. It also stays invisible in the glucose
trace for hours, unlike a bolus. So it is the one event worth a reminder.

> The notification describes the **log**, not the body. It reports that no dose
> was recorded. It never asks for a dose. A dose that was taken but not logged
> stops the reminder as soon as someone logs it.

The app is its own push server. It holds a VAPID key pair and signs and encrypts
every message. No third-party notification service is involved, and no API key.
The browser push service, such as FCM on Android, relays a blob that it cannot
read. That relay is part of the browser and cannot be self-hosted. It still sees
nothing of the data.

### Setup

1. Mint a signing key:
   ```bash
   sugardaddy vapid-keys
   ```
2. Put the key in the server environment as `SUGARDADDY_VAPID_PRIVATE_KEY`. Under
   Docker, put it in `docker/.env`. Never put it in `config.toml`. Anyone who
   holds this key can push to the subscribed devices.
3. In `config.toml`, set `enabled = true` and a `subject` under `[notify]`.
4. Rebuild and restart the container:
   ```bash
   cd docker && docker compose up -d --build
   ```
5. Open the app on the phone and tap **Enable reminders**.

Two requirements come first:

- **HTTPS is mandatory.** A browser refuses to register a service worker over
  plain HTTP, and it never subscribes to push. Serve the app behind a reverse
  proxy with a real certificate.
- **On Android, install the app first** (Chrome ⋮ → *Add to Home screen*). Push
  to a plain browser tab is unreliable. iOS refuses it.

This command sends a test notification to every subscribed device, which proves
the whole chain works:

```bash
curl -sX POST http://<host>:<host-port>/api/push/test
```

### When the reminder fires

The clock starts at the **last logged basal dose**. The reminder fires
`basal_interval_hours` plus `basal_leniency_hours` after it. The defaults of 24
and 1 put it 25 hours later.

Leniency is a separate setting on purpose. It keeps 24 hours as the real
threshold and adds grace on top. A `basal_leniency_hours` of 3 waits until 27
hours.

The clock ties to the dose itself, so the reminder lands near the usual time of
day for basal. There is no quiet-hours setting to manage.

The first notification arrives once per missed dose. After that, `repeat_hours`
re-sends it every 30 minutes by default. Every re-send is silent and replaces the
previous notification instead of stacking. Swiping the reminder away therefore
buys time only until the next one.

The repeat window is short on purpose. A reminder is worth most in the first
hour, while "take the usual dose" is still the answer. Hours later the same
reminder asks a harder question, and the app must not answer that one. Set
`repeat_hours = 0` for a single notification.

Web Push reports nothing back, so the server cannot tell a dismissed notification
from an ignored one. The repeat runs on its own clock either way. Logging a basal
dose is the only thing that stops it, and that also re-arms it for the next day.

If no basal dose has **ever** been logged, the app stays silent. A fresh install
must not nag about a dose it has no evidence of.

A running server checks every `[notify] poll_seconds`. Set that to `0` to switch
the built-in check off and run `sugardaddy notify` from cron instead. Add
`--dry-run` to see what would go out without sending anything.

## Configuration

One TOML file describes the whole app. Copy `config.example.toml` to
`config.toml`. Secrets never live in this file. They come from the environment.

`[librelink]`
| key | default | meaning |
|-----|---------|---------|
| `region` | `AU` | pylibrelinkup region (AU, EU, US, and others) |
| `poll_interval_seconds` | 60 | seconds between polls for the latest reading. The minimum is 15. |
| `patient_id` | — | needed only when the account follows more than one person |

Credentials: `SUGARDADDY_LIBRE_EMAIL` and `SUGARDADDY_LIBRE_PASSWORD`, from the
environment only.

`[database]`
| key | default | meaning |
|-----|---------|---------|
| `path` | `/data/sugardaddy.db` | SQLite file location |

`[web]`
| key | default | meaning |
|-----|---------|---------|
| `host` and `port` | `0.0.0.0` and 8080 | HTTP bind inside the container |
| `timezone` | `Australia/Sydney` | display timezone. Storage is UTC. |
| `units` | `mmol/L` | display unit, `mmol/L` or `mg/dL`. Storage is mg/dL. |
| `target_low` and `target_high` | 3.9 and 10.0 | time-in-range band, in display units |

`[insulin]`
| key | default | meaning |
|-----|---------|---------|
| `dia_minutes` | 300 | duration of insulin action, for retrospective insulin-on-board context |
| `peak_minutes` | 75 | time to peak activity for the same curve |
| `isf` | unset | glucose drop from 1 unit. Setting it switches on the experimental bolus reference. |
| `icr` | unset | grams of carb covered by 1 unit |
| `target_bg` | target band midpoint | what a correction aims at |

The last three are for the account holder and a clinician to decide. The app
never infers or tunes them. It leaves the bolus reference off until `isf` exists,
because a confident number on a shaky sensitivity factor is worse than no number.

`[prescription]` (recorded fact, never a calculation input)
| key | default | meaning |
|-----|---------|---------|
| `reviewed` | — | ISO date the prescription was last set. Required once anything else is set. |
| `basal_product` and `rapid_product` | — | product names, for the report only |
| `basal_units` | — | the prescribed basal dose |
| `basal_timing` | — | free text, such as "evening" |
| `icr` and `isf` | — | ratios a clinician chose, recorded next to the `[insulin]` values in use |

An empty section means "not recorded". It never means "prescribed nothing".

`[notify]` (the basal reminder — see [Notifications](#notifications))
| key | default | meaning |
|-----|---------|---------|
| `enabled` | `false` | the on/off switch for all notifications |
| `subject` | — | contact URL for the push service, `mailto:…` or `https://…`. Required when enabled. |
| `poll_seconds` | 900 | seconds between checks. `0` switches the built-in check off. |
| `ttl_seconds` | 86400 | how long a push service holds a message for an offline phone |
| `basal_interval_hours` | 24 | the expected gap between logged basal doses |
| `basal_leniency_hours` | 1 | extra grace before the phone says anything |
| `repeat_hours` | 0.5 | how often to re-send a reminder that still stands. Fractions work. A value below `poll_seconds` cannot be honored. `0` says it once. |

Signing key: `SUGARDADDY_VAPID_PRIVATE_KEY`, from the environment only.

`[backfill]` (the one-time Home Assistant seed only)
| key | default | meaning |
|-----|---------|---------|
| `ha_url` | — | Home Assistant base URL |
| `ha_entity` | — | the glucose sensor entity id |

Token: `SUGARDADDY_HA_TOKEN`, from the environment only.

## Common operations

**Change credentials.** Edit `docker/.env`, then recreate the container. A plain
`restart` keeps the old environment.
```bash
cd docker && docker compose up -d --force-recreate
```

**Deploy code changes.** The image bakes the code in, so this rebuilds it.
```bash
cd docker && docker compose up -d --build
```

**Back up the database before a deploy that changes the schema.** Schema
migrations run at startup.
```bash
docker cp sugardaddy:/data/sugardaddy.db ./backup.db
```

**Watch the logs.**
```bash
cd docker && docker compose logs -f
```

## Repo layout

```
sugardaddy/
  cli.py         entrypoint: serve | ingest | backfill | init-db | report | notify
  config.py      one TOML → typed config (secrets from env, never in TOML)
  constants.py   unit conversion, trend arrows, default target range
  models.py      typed rows (readings, doses, notes, foods, meals, templates)
  db.py          SQLite schema and queries (UTC epoch, dedup on timestamp)
  source.py      GlucoseSource interface + LibreLinkUpSource (pylibrelinkup)
  ingest.py      background poll loop (auth → latest()/graph() → store)
  backfill.py    one-shot Home Assistant history import
  iob.py         insulin activity curve and insulin-on-board math
  bolus.py       the experimental bolus reference formula
  analysis.py    pure retrospective functions (no I/O, no clock, no config)
  report.py      the `report` command: window resolution, text and JSON output
  notify.py      Web Push app server and the basal reminder
  web.py         FastAPI app: phone and desktop routes, JSON API, /healthz
  templates/     Jinja: base, phone/, desktop/, partials/
  static/        vendored htmx and Chart.js, CSS, phone.js, desktop.js
config.example.toml   the whole app in one file
docker/               Dockerfile, compose file, .env.example
deploy/               install scripts and the Claude Code review skill
docs/plans/           design documents
tests/                plain-assert tests, runnable with bare python
tools/                small maintenance scripts
```

## Claude Code review skill (optional)

`deploy/skills/sugardaddy-review/` is a Claude Code skill. It fetches the live
database from the serve host, runs `report --json`, and writes a review aimed at
management of the condition. The review compares against the previous run and
keeps a local history, so trends stay visible. The skill does the fetch and the
interpretation. `sugardaddy report` still does the math.

Install it on any machine:

```bash
bash deploy/install-skill.sh
```

The script copies the skill into `~/.claude/skills/sugardaddy-review/`. It also
seeds a machine-local `connection.env`. Edit that file and point it at the serve
host:

```bash
$EDITOR ~/.claude/skills/sugardaddy-review/connection.env   # set SD_REVIEW_HOST
```

`connection.env` holds host names and paths. The review `history/` holds glucose
data. Both stay on one machine, so neither belongs in the repo. The repo tracks
`connection.env.example` alone. In Claude Code, ask for a review of the sugardaddy
data, or run `/sugardaddy-review`.

## Roadmap

None of this exists yet. It sits here so the direction stays clear. Every item
rides on the same UTC timeline, so each one adds to the app instead of rewriting
it.

- **Activity and wearable data** (steps, heart rate, workouts). Two feeds are
  possible. A one-time import of a Samsung Health export covers deep history. An
  ongoing feed can come through Android **Health Connect** and the Home Assistant
  companion app, which needs no custom phone app. Storage would be a small
  `activity` table that joins the shared timeline.
- **Trend analysis and prediction.** Glucose, insulin, meals, notes, and activity
  share one timeline. The app could learn response patterns per meal and per time
  of day, then flag likely highs and lows. This stays material for review with a
  clinician.
- **More dosing awareness.** The experimental bolus reference is the first step.
  The direction, the formulas, and the safety boundary are in
  [`docs/plans/insulin-awareness.md`](docs/plans/insulin-awareness.md).
- **Smaller ideas.** Alerts on sustained highs and lows. Optional authentication,
  if the app ever leaves a trusted network. Photo attachments on meals. CSV
  export of the combined timeline.

## Known limitations

- **LibreLinkUp is unofficial and reverse-engineered.** Abbott sometimes changes
  a required app-version header. Ingestion pauses until a new `pylibrelinkup`
  ships. Manual logging of meals and doses keeps working. Pin the dependency and
  update it when this happens.
- **The trace carries a wobble of about ±0.8 mmol/L that is not blood sugar.**
  Short excursions alternate up and down on a cycle of 25 to 35 minutes. The
  pattern matches the sensor's lag-compensating filter, plus local blood flow and
  pressure at the sensor site. It is real, but local to one patch of skin. Read
  the aggregates and the smoothed line rather than single points.
- **This is not real-time data.** Readings arrive about once a minute from
  LibreLinkUp, in whole mg/dL. History imported from Home Assistant is coarser,
  at about one reading every five minutes.
- **Carb figures are estimates.** They are as good as the food library and the
  plates behind them, and many meals carry no carb count at all. `report` states
  the coverage for this reason.
- **History starts at the first run**, plus the optional Home Assistant seed. The
  LibreLinkUp API exposes about 12 hours through `graph` and 14 days through
  `logbook`.
- **The app has no authentication.** It expects a trusted LAN or VPN.
