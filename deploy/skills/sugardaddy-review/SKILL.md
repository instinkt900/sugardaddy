---
name: sugardaddy-review
description: >-
  Pull the live sugardaddy glucose database off the serve host and produce a
  retrospective glucose-management review — time-in-range, variability, dawn /
  time-of-day patterns, low episodes, insulin behaviour, and post-meal
  responses — with a comparison against the previous review. Use when the user
  asks to "review", "analyse", or "look at" their sugardaddy / CGM / glucose /
  insulin / meal data, or to check how the numbers are trending.
---

# sugardaddy glucose review

This skill turns the live sugardaddy database into a management-focused review of
the data: what the glucose, meals and insulin show about patterns and habits.
The heavy number-crunching lives in the repo (`sugardaddy report`), so this skill
only **fetches**, **runs the report**, and **interprets** — including the trend
versus the last review.

## Guardrails — read first

- **This is not medical advice and not a medical device.** Report only what the
  data shows (patterns, timing, variability, coverage). Do not prescribe doses,
  ratios, or changes. Frame everything as observations and questions the user
  might raise with their own clinician. Say this explicitly in the output.
- **Connection details are machine-local, never in git.** The serve host and
  paths live in `connection.env` next to this file — an untracked, per-machine
  file. Only `connection.env.example` (placeholders) is committed. Do not paste a
  real host into the repo, this file, or the output.
- **Health data stays local.** The review `history/` under this skill dir holds
  glucose data; it lives only under `~/.claude` and must never be committed.
- Delete the temp DB copies (local and on the server) when done.

## Environment

Connection details are read at run time from `connection.env` in this skill's
directory. Load it before the commands below:

```bash
SKILL_DIR="$HOME/.claude/skills/sugardaddy-review"
set -a; . "$SKILL_DIR/connection.env"; set +a
```

That defines: `SD_REVIEW_HOST` (ssh target for the Docker serve host),
`SD_REVIEW_CONTAINER` (container name), `SD_REVIEW_DB_IN_CONTAINER` (DB path
inside the container), and `SD_REVIEW_REPO` (local clone of the sugardaddy repo).
Inside the repo, the analyser is `.venv/bin/python -m sugardaddy` and the config
is `config.toml` in the repo root (supplies units, target range, timezone).

If `connection.env` is missing, tell the user to run `deploy/install-skill.sh`
from their repo clone (or copy `connection.env.example` to `connection.env`) and
fill it in. If the host is unreachable or the container name has changed, stop
and ask the user rather than guessing.

## Steps

1. **Pick the window.** Default to `--days 14`. If the user names a period
   ("last week", "since Monday", "this month"), translate it to a day count. As
   the dataset grows, prefer a bounded window so a good recent run isn't diluted
   by old history.

2. **Fetch a fresh copy of the live DB** (SQLite is safe to copy hot):
   ```bash
   ssh "$SD_REVIEW_HOST" "docker cp $SD_REVIEW_CONTAINER:$SD_REVIEW_DB_IN_CONTAINER /tmp/sd_live.db"
   scp -q "$SD_REVIEW_HOST:/tmp/sd_live.db" /tmp/sd_live.db
   ssh "$SD_REVIEW_HOST" 'rm -f /tmp/sd_live.db'   # clean the server temp
   ```

3. **Run the report as JSON** from the repo (the config supplies units, target
   range and timezone; `--db` points at the copy):
   ```bash
   cd "$SD_REVIEW_REPO"
   .venv/bin/python -m sugardaddy report -c config.toml --db /tmp/sd_live.db --days 14 --json
   ```
   (Drop `--json` for a quick eyeball of the formatted text version.)

4. **Load the previous review for comparison.** List `"$SKILL_DIR/history/"` and
   read the most recent `report-*.json` (if any). Compute deltas on the headline
   metrics: time-in-range, average / GMI, CV, below-range %, number of low
   episodes, and carb-logging coverage. If there is no prior file, say so — this
   is the baseline.

5. **Save this run** for next time. Ask the user for today's date if you don't
   have it, then write the JSON to
   `"$SKILL_DIR/history/report-<YYYYMMDD-HHMM>.json"`.
   (Do not fabricate a timestamp.)

6. **Clean up** the local temp:
   ```bash
   rm -f /tmp/sd_live.db
   ```

## What to look for when interpreting

Read the JSON, don't re-derive the maths. Focus the write-up on management:

- **Headline + trend.** TIR vs the ~70% aim, average/GMI, and **CV** (>36% =
  high variability). Lead with how each moved since the last review.
- **Time-of-day (`hourly`).** A steady early-morning climb (~03:00–07:00) is a
  dawn-rise signature; flat overnight lows or a post-lunch afternoon peak show
  up here too. Call out the worst and best hours.
- **Low episodes (`low_episodes`).** These are grouped events, not raw readings.
  For any episode, look at what preceded it in `insulin` / `post_meal` — a
  late-evening correction stacked on a meal bolus carrying a low into sleep is
  the highest-consequence pattern to flag.
- **Insulin behaviour (`insulin`).** Corrections outnumbering meal boluses
  suggests chasing highs after the fact rather than covering carbs up front.
- **Meals (`post_meal` + `carb_coverage`).** Late, large peaks (+2h still high)
  point at fast-carb foods or dose timing. Low `carb_coverage` is worth naming
  every time — without carb counts no ratio analysis is possible, so improving
  logging is the concrete lever that unlocks deeper future reviews.

## Suggestions / talking points

After the patterns, include a short **"Talking points"** section: a handful of
suggestions for better control that the user can agree with or decline based on
their own professional and personal knowledge. Treat it like a review assistant
handing someone an agenda — not a prescription.

Rules for this section:
- **Anchor every point to something in this data.** Each suggestion names the
  observed pattern it comes from (the dawn rise, the afternoon highs, the
  bedtime-correction low, late meal peaks, correction-vs-bolus ratio, carb
  coverage). No generic diabetes tips.
- **Frame as a lever + a question, not an instruction.** e.g. "The 3–7am climb
  looks like a dawn pattern — worth asking whether overnight basal is holding
  through the back half of the night?" rather than "increase your basal."
- **Never give numbers to change.** No specific doses, ratios, correction
  factors, basal rates, or timing amounts. Suggest *what to discuss/adjust and
  watch*, not *by how much*.
- **Make each point declinable.** Phrase so the user can reasonably say "no, I
  already know why that is" — they hold knowledge the data doesn't.
- **Order by likely impact**, and keep it to ~3–6 points.
- Distinguish signal from artefact where you can (e.g. some fast-carb "meals"
  are clearly hypo treatments, not choices to change).

These are still observations and prompts for the user and their clinician — the
non-medical-advice guardrail applies to this section as much as the rest.

## Output shape

Lead with a short headline (overall control + the single most important thing
that changed or needs attention), then a compact metrics table with trend arrows
vs last review, then the patterns as short bullets, then the **Talking points**
section above, and finish with the non-medical-advice reminder and one or two
data-hygiene suggestions (e.g. carb logging). Keep it tight and
management-focused, not a data dump.
