# Sugar Daddy

Sugar Daddy is a small, self-contained app for people who use a **FreeStyle
Libre** CGM. It ingests your glucose readings over time. You log **insulin
doses** and **meals** from your phone. A desktop dashboard shows all of it on
one timeline.

The app connects **directly to LibreLinkUp**, the same sharing service that the
Home Assistant Libre integration uses. It needs no Home Assistant at runtime.
Everything runs on your own infrastructure.

> **Not a medical device.** Sugar Daddy keeps personal records and supports
> *retrospective* analysis. Use it to find patterns to discuss with your care
> team. Do not use it for real-time dosing decisions.

## What it does

- **Glucose ingest** — the app reads LibreLinkUp at a set interval. It stores
  every reading in a local SQLite database and skips duplicates.
- **Two web UIs, one backend:**
  - **Phone** (`/`) — built for input. It shows the current reading and trend,
    and it refreshes automatically. Below that sit a compact 24h chart and a
    fast insulin form. The **meal plate builder** takes foods from the library
    or from ad-hoc text, each with a count, then logs the whole plate. To
    prefill the plate in one tap, load a **saved meal**.
  - **Desktop** (`/desktop`) — built for review. A large interactive chart plots
    the glucose line, the target band, and markers for doses and meals. Pick any
    date range. Sort the tables, and add, edit, or delete rows inline. Analysis
    panels sit alongside. This UI also manages the food library and saved meals.
- **Composite meals** — a meal is a plate of **foods**, each with a count, for
  example 1 sandwich, 1 juice, and 2 biscuits. The app totals the carbs and
  calories.
- **Food library** — reusable foods with a name, description, carbs, and
  calories. Add, edit, and delete them freely, and every device sees the same
  library. Names are unique and ignore case. If you save a name again, the app
  updates the food that already has it.
- **Saved meals** — named plates for fast logging, with **Update** and **Save as
  new**. A saved meal links live to the food library, so a correction to a food
  reaches every saved meal that uses it. History is a snapshot. If you edit or
  delete a food or a saved meal, the meals you already logged never change.
- **Analysis** of the timeline — time in range, average glucose, estimated GMI,
  and high and low counts. It also measures the 2-hour glucose response after
  each logged meal.
- **Daily intake table** (desktop) — carbs, calories, and mealtime insulin per
  day, with a per-day average. Basal is excluded from the insulin total, because
  one long-acting dose would hide the mealtime doses. A `*` marks a day where not
  every meal carries the figure, so the total is a floor and not a fact. This
  table keeps **its own range** (3d, 7d, 30d, or a day count you type), separate
  from the chart. Days always start at local midnight, so no row is a part day.
  Today is tagged **so far** and stays out of the average.
- **Basal reminder** — the app can send your phone one notification. It tells you
  when no **basal** dose has been logged for more than a day. It reports a gap in
  the log. It never tells you to take a dose. This feature is off by default. See
  [Notifications](#notifications).
- **History seed** — import once from an existing Home Assistant install. This
  step is optional, and it gives your charts depth from day one.

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

- A small `GlucoseSource` interface hides the source of the readings.
  LibreLinkUp is the default. The app uses Home Assistant *only* for the
  one-time backfill.
- Credentials live in the environment, in the Docker `.env` file. They never
  reach the committed config or the database.
- Access is LAN or VPN only, with no auth by design. The app trusts the network
  it binds to. Do not expose it directly to the internet.

## Example setup

The addresses below are placeholders from RFC 5737. Substitute your own. A flat
LAN is all you need, and you can reach it over a VPN when you are away.

| piece | example | notes |
|-------|---------|-------|
| serve host | `192.0.2.20:8080` | any Docker-capable box on your LAN |
| Home Assistant | `192.0.2.10:8123` | optional, for the one-time history seed |
| your phone or PC | — | on the LAN or VPN, and it opens the URLs above |

## Repo layout

```
sugardaddy/
  cli.py         entrypoint: serve | ingest | backfill | init-db
  config.py      one TOML → typed config (secrets from env, never in TOML)
  constants.py   unit conversion, trend arrows, default target range
  models.py      typed rows (readings, doses, foods, meals + items, templates)
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

Run `sugardaddy <command>` if you installed the package. Otherwise run
`python -m sugardaddy <command>`.

```bash
sugardaddy serve    -c config.toml            # web app + glucose poller
sugardaddy ingest   -c config.toml [--once]   # poller only (--once = sync, then exit)
sugardaddy backfill -c config.toml --days 90  # one-time HA history seed
sugardaddy init-db  -c config.toml            # create the DB schema, then exit
sugardaddy report   -c config.toml [--days N] # retrospective analysis (text or --json)
sugardaddy notify   -c config.toml [--dry-run] # push the basal reminder, if one is due
sugardaddy vapid-keys                         # mint a Web Push signing key
```

Add `-v` to get debug logs.

### `report` — retrospective analysis

`report` reads the stored timeline over a window. The default window is 14 days.
It covers:

- time in range, average glucose, and estimated GMI
- glucose variability, as SD and CV
- a breakdown per day and per hour of the day
- discrete low episodes, where contiguous below-range runs collapse into single
  events
- an insulin dose summary by kind
- carb-logging coverage
- the 2-hour glucose response after each logged meal

The command crunches numbers only. It makes no clinical judgments.

```bash
sugardaddy report -c config.toml --days 7           # human-readable text
sugardaddy report -c config.toml --days 30 --json   # machine-readable JSON
sugardaddy report -c config.toml --db /tmp/copy.db  # analyze a different DB file
```

`--db` overrides the database path from the config. Units, target range, and
timezone still come from the config. Use it to analyze a copy of the live
database that you copied from the serve host.

### `sugardaddy-review` skill (Claude Code, optional)

`deploy/skills/sugardaddy-review/` is a Claude Code skill. It fetches the live
DB from the serve host, runs `report --json`, and writes a management-focused
review. The review compares against the previous run and keeps a local history,
so trends stay visible over time. The skill does the fetch and the
interpretation. `sugardaddy report` still does the math.

Install it on any machine you use:

```bash
bash deploy/install-skill.sh
```

The script copies the skill into `~/.claude/skills/sugardaddy-review/`. It also
seeds a machine-local `connection.env`. Edit that file and point it at your
serve host:

```bash
$EDITOR ~/.claude/skills/sugardaddy-review/connection.env   # set SD_REVIEW_HOST
```

`connection.env` holds your host and paths, and the review `history/` holds
glucose data. Both stay per-machine, so never commit either one. The repo tracks
`connection.env.example` alone. In Claude Code, ask to "review my sugardaddy
data", or run `/sugardaddy-review`.

## Setup — serve side (Docker)

1. Copy the config and add your secrets:
   ```bash
   cp config.example.toml config.toml   # edit [librelink].region, [web] tz/units
   cp docker/.env.example docker/.env   # LibreLinkUp email + password
   ```
   Use the **LibreLinkUp** account credentials. That is the follower account
   that already has access to your Libre data.
2. Build the image and start the container:
   ```bash
   bash deploy/install-server.sh          # or: cd docker && docker compose up -d --build
   ```
3. Check that the app answers:
   ```bash
   curl -s http://localhost:8080/healthz
   ```
4. Open the UIs. Phone: `http://<host>:8080/` · Desktop:
   `http://<host>:8080/desktop`

## Setup — seed history from Home Assistant (optional, one-time)

If HA already holds months of Libre history, import it so the charts start deep.

1. In HA, create a long-lived token under **Profile → Security → Long-lived
   access tokens**.
2. Set `[backfill].ha_url` and `ha_entity` in `config.toml`.
3. Put the token in `docker/.env` as `SUGARDADDY_HA_TOKEN`.
4. Run the backfill once inside the container:
   ```bash
   cd docker && docker compose run --rm sugardaddy backfill -c /app/config.toml --days 180
   ```

HA stores AU sensors in mmol/L, and the import converts them to mg/dL. If your
HA sensor already reports mg/dL, pass `--unit mg/dL`.

## Notifications

The app sends **one** notification: a reminder that no **basal** dose has been
logged for more than a day.

Basal is the dose that is easy to forget. Unlike a bolus, it also stays invisible
in the glucose trace for hours. So it is the one thing worth a nudge.

> The notification is about your **log**, not your body. It says that no dose was
> recorded. It never says to take one. If you took the dose and forgot to log it,
> log it and the reminder stops.

The app is its own push server. It holds a VAPID key pair. It signs and encrypts
every message itself. No third-party notification service is involved, and no API
key. The browser push service (FCM on Android) only relays a blob that it cannot
read. That relay is part of the browser, so you cannot self-host it. But nothing
about your data is visible to it.

### Setup

1. Mint a signing key:
   ```bash
   sugardaddy vapid-keys
   ```
2. Put the key in the server environment as `SUGARDADDY_VAPID_PRIVATE_KEY`. For
   the Docker deployment, put it in `docker/.env`. Never put it in
   `config.toml`. Anyone who holds this key can push to your devices.
3. In `config.toml`, set `enabled = true` and a `subject` under `[notify]`.
4. Rebuild and restart the container. The image bakes in the code, so a plain
   restart is not enough:
   ```bash
   cd docker && docker compose up -d --build
   ```
5. On the phone, open the app and tap **Enable reminders**.

Two requirements come first:

- **HTTPS is mandatory.** A browser does not register a service worker over plain
  HTTP, and it never subscribes to push. Serve the app behind a reverse proxy
  with a real certificate.
- **On Android, install the app first** (Chrome ⋮ → *Add to Home screen*). Push
  to a plain browser tab is unreliable. iOS refuses it.

To prove the chain works, send a test notification to every subscribed device:

```bash
curl -sX POST http://<host>:8080/api/push/test
```

### When the reminder fires

The clock starts at your **last logged basal dose**. The reminder fires
`basal_interval_hours` plus `basal_leniency_hours` after it. With the defaults,
24 + 1, that is 25 hours later.

Leniency is a separate setting on purpose. Keep 24 hours as the real threshold,
and give yourself as much grace as you want on top. Raise
`basal_leniency_hours` to 3 to wait until 27 hours.

Because the clock is tied to the dose itself, the reminder lands near the time of
day you usually take basal. There is no quiet-hours setting to manage.

You get one notification per missed dose. After that, `repeat_hours` re-sends it
every 30 minutes by default. Every re-send is silent, and it replaces the previous
notification instead of stacking. So swiping the reminder away only buys you until
the next one.

The repeat is short on purpose. A reminder is worth most in the first hour, while
"just take the usual dose" is still the answer. Hours later the same reminder asks
a harder question that the app must not answer for you. Set `repeat_hours = 0` to
be told once and no more.

The server cannot tell whether you dismissed a notification or ignored it, because
Web Push reports nothing back. The repeat therefore runs on its own clock either
way. Logging a basal dose is the only thing that stops it, and that also re-arms
it for the next day.

Log a basal dose and the reminder resets for the next day. If no basal dose has
**ever** been logged, the app stays silent. A fresh install must not nag you about
a dose it has no evidence you take.

A running server checks every `[notify] poll_seconds`. Set that to `0` to switch
the check off, and run `sugardaddy notify` from cron instead. Add `--dry-run` to
see what would be sent without sending anything.

## Common operations

**Change credentials** — edit `docker/.env`, then recreate the container. A
plain `restart` keeps the old env.
```bash
cd docker && docker compose up -d --force-recreate
```

**Redeploy code changes:**
```bash
cd docker && docker compose up -d --build
```

**Watch logs, and back up the DB:**
```bash
docker compose logs -f
docker compose cp sugardaddy:/data/sugardaddy.db ./backup.db
```

## Configuration reference

`[librelink]`
| key | default | meaning |
|-----|---------|---------|
| `region` | `AU` | pylibrelinkup region (AU, EU, US, …) |
| `poll_interval_seconds` | 60 | seconds between polls for the latest reading. The minimum is 15. |
| `patient_id` | — | set this only if the account follows more than one person |

Credentials: `SUGARDADDY_LIBRE_EMAIL` and `SUGARDADDY_LIBRE_PASSWORD`, from the
environment only.

`[database]`
| key | default | meaning |
|-----|---------|---------|
| `path` | `/data/sugardaddy.db` | SQLite file location |

`[web]`
| key | default | meaning |
|-----|---------|---------|
| `host` and `port` | `0.0.0.0` and 8080 | HTTP bind |
| `timezone` | `Australia/Sydney` | display tz. Storage is UTC. |
| `units` | `mmol/L` | display unit, `mmol/L` or `mg/dL`. Storage is mg/dL. |
| `target_low` and `target_high` | 3.9 and 10.0 | time-in-range band, in display units |

`[notify]` (the basal reminder — see [Notifications](#notifications))
| key | default | meaning |
|-----|---------|---------|
| `enabled` | `false` | the on/off switch for all notifications |
| `subject` | — | contact URL for the push service, `mailto:…` or `https://…`. Required when enabled. |
| `poll_seconds` | 900 | seconds between checks. `0` switches the in-process check off. |
| `ttl_seconds` | 86400 | how long a push service holds a message for a phone that is offline |
| `basal_interval_hours` | 24 | the expected gap between logged basal doses |
| `basal_leniency_hours` | 1 | extra grace before the phone says anything |
| `repeat_hours` | 0.5 | how often to re-send a reminder that still stands. Fractions are fine, and a value below `poll_seconds` cannot be honoured. `0` = say it once. |

Signing key: `SUGARDADDY_VAPID_PRIVATE_KEY`, from the environment only.

`[backfill]` (one-time HA seed only)
| key | default | meaning |
|-----|---------|---------|
| `ha_url` | — | Home Assistant base URL |
| `ha_entity` | — | the glucose sensor entity id |

Token: `SUGARDADDY_HA_TOKEN`, from the environment only.

## Roadmap / ideas

None of this is built yet. It sits here so the direction stays clear. All of it
rides on the same UTC timeline, so each item adds to the app instead of
rewriting it.

- **Activity and wearable data (steps, heart rate, workouts).** Bring in Samsung
  Health and Galaxy Watch metrics to give the analysis more to work with. There
  are two feeds:
  - *One-time history* — import a Samsung Health export through a new
    `sugardaddy import-samsung` command. The export is a set of CSVs for daily
    steps, heart rate, and exercise. This path suits deep retrospective history.
    It is not something to repeat weekly.
  - *Ongoing* — Samsung Health already syncs into Android **Health Connect**,
    and the Home Assistant Android companion app can expose Health Connect
    metrics as HA sensors. Sugar Daddy would then read activity from HA, the
    same way it can seed glucose from HA. That needs no custom phone app and no
    Samsung developer approval.
  - Storage would be a small `activity` table for steps, heart-rate readings,
    and workouts. It joins against glucose, meals, and insulin on the shared
    timeline.
- **Trend analysis and prediction.** Glucose, insulin, meals, and activity share
  one timeline. From that, the app can learn response patterns per meal and per
  time of day, then flag likely highs and lows. This stays decision-support for
  review with a clinician. It is not dosing advice.
- **Insulin-on-board and dosing awareness ("de-vibe the dose").** Calculate a
  concrete anchor to check a dose against, right where you log the insulin, so
  it is not decided on vibes. It ships as a glanceable, non-prescriptive nudge:
  active IOB plus trajectory, such as "you may be more sensitive right now" or
  "you are running high and still climbing". A fuller IOB/ISF/ICR **bolus
  calculator** is a stretch goal, built for academic interest and as one more
  data point to reconcile against — "it says 12 u, I feel 6 — why?" — never a
  directive. The full write-up, the formulas, and the safety boundary are in
  [`docs/plans/insulin-awareness.md`](docs/plans/insulin-awareness.md).
- **Nice-to-haves.** Alerts on sustained highs and lows. Optional auth, if the
  app ever goes beyond a trusted LAN or VPN. Photo attachments on meals. CSV
  export of the combined timeline.

Live wearable data depends on the Health Connect to HA feed above. The manual
export only makes sense as a one-off seed.

## Notes and limitations

- **The notification badge** (`sugardaddy/static/icons/icon-badge-96.png`) is the
  small icon Android draws in the status bar. Android keeps only the alpha
  channel and paints the result white, so this file is the bare droplet on
  transparency. A full-colour icon there shows as a plain white square. Rebuild it
  from the app icon with `python tools/make_badge_icon.py` (needs
  `pip install pillow`).

- **LibreLinkUp is unofficial and reverse-engineered.** Abbott occasionally
  bumps a required app-version header, which pauses ingestion until a new
  `pylibrelinkup` ships. Manual meal and dose logging keeps working. Pin the
  dependency, and update it when this happens.
- Glucose granularity is whatever LibreLinkUp reports, about 5 minutes. This is
  not real-time data.
- Long-term history accumulates from the first run, plus the optional HA seed.
  The live API exposes about 12h through `graph` and 14 days through `logbook`.
```
