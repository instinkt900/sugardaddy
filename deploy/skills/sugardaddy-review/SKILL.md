---
name: sugardaddy-review
description: >-
  Pull the live sugardaddy glucose database off the serve host and produce a
  retrospective glucose-management review — time-in-range, variability, dawn /
  time-of-day patterns, low episodes, insulin habits (dose timing and sizes),
  eating habits (meal timing, snacking, foods), and post-meal responses by
  food — with a comparison against the previous review. Also writes a
  standalone clinician-ready report (Markdown, HTML and PDF) for handing to a
  health professional. Use when the user asks to "review", "analyse", or "look
  at" their sugardaddy / CGM / glucose / insulin / meal data, to check how the
  numbers are trending, or to prepare something for their doctor.
---

# sugardaddy glucose review

This skill turns the live sugardaddy database into a management-focused review of
the data: what the glucose, meals and insulin show about patterns and habits.
The heavy number-crunching lives in the repo (`sugardaddy report`), so this skill
only **fetches**, **runs the report**, and **interprets** — including the trend
versus the last review.

Every run produces **two** things for **two different readers**:

1. **The chat review** — for the user, who knows the system. Trend-aware,
   conversational, comparing against the last run.
2. **The clinician report** — a standalone `.md` / `.html` / `.pdf` for a
   professional with no knowledge of the system and no time. Written to be
   skim-read and judged on its suggested experiments.

## What this system is for

The purpose of the whole system is to **collect data to better understand the
management of diabetes — not to guide it**. Guidance comes from the user's own
assessment and from professional experts. This review is retrospective: an
attempt to understand *behaviours and reactions* after the fact.

Hold that distinction everywhere. "Your glucose climbed for two hours after that
meal" is understanding. "So take more for it" is guidance, and is not this
skill's job — nor is it the job of any number the report emits.

## Guardrails — read first

- **This is not medical advice and not a medical device.** Report only what the
  data shows (patterns, timing, variability, coverage). Do not prescribe doses,
  ratios, or changes. Frame everything as observations and questions the user
  might raise with their own clinician. Say this explicitly in the output.
- **The `bolus_backtest` section is an experiment being graded, not a source.**
  It contains calculated dose figures. They are computed from `[insulin].isf` /
  `icr` in the config, which are user-set and may be rough placeholders rather
  than clinician-given values. Never quote a `suggested_units` figure as what the
  user should have taken, and never propose a new ISF/ICR value. See "The bolus
  reference" below for what you *may* say about it.
- **Connection details are machine-local, never in git.** The serve host and
  paths live in `connection.env` next to this file — an untracked, per-machine
  file. Only `connection.env.example` (placeholders) is committed. Do not paste a
  real host into the repo, this file, or the output.
- **Health data stays local.** The review `history/` under this skill dir holds
  glucose data — the JSON runs and the generated clinician report in all three
  formats (`.md`, `.html`, `.pdf`). It lives only under `~/.claude` and must never
  be committed. Never write a generated report into the repo clone.
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
   episodes, and carb-logging coverage. Also trend the **habit** figures where the
   older file has them: basal `clock.spread_min`, correction count, meals per day,
   median overnight fast. Habits move slower than TIR, so a shift in one of these
   is usually the more meaningful change. An older run predating a section simply
   has no delta — say so rather than treating it as a change. If there is no prior
   file at all, say so — this is the baseline.

5. **Save this run** for next time. Ask the user for today's date if you don't
   have it, then write the JSON to
   `"$SKILL_DIR/history/report-<YYYYMMDD-HHMM>.json"`.
   (Do not fabricate a timestamp.)

6. **Write the chat review** — the conversational output described under "Output
   shape" below. This is the primary response to the user.

7. **Write the clinician report** as well, every run. It is a *different document
   for a different reader*, not a copy of the chat review — see "The clinician
   report" below for its rules and section order. Write it to
   `"$SKILL_DIR/history/clinician-report-<YYYYMMDD>.md"`.

8. **Render the clinician report** to HTML and PDF:
   ```bash
   python3 "$SKILL_DIR/render_report.py" "$SKILL_DIR/history/clinician-report-<YYYYMMDD>.md"
   ```
   The script is stdlib-only and drives headless Chrome for the PDF. If it reports
   the PDF failed, the HTML beside it is still complete — tell the user to open
   that and print to PDF from the browser. Do not install anything to fix it.
   Tell the user the paths of all three files at the end.

9. **Clean up** the local temp:
   ```bash
   rm -f /tmp/sd_live.db
   ```

## The clinician report

Every run also produces a document the user can hand to a clinician who has never
seen this system. Assume that reader has **two minutes and no context**. They must
be able to learn what the system is, see the control figures, and judge whether
the suggested experiments are sensible — without reading the whole thing.

Write it with the **`ste-writing` skill in STE-flavored mode** (invoke that skill
and follow it). Short sentences, active voice, one idea per sentence, no
semicolons, no contractions. Two deliberate departures from that skill:

- **Keep British/Australian spelling** (`hypoglycaemia`, not `hypoglycemia`). STE
  mandates American spelling. This document goes to an Australian clinician, so
  the house style wins. Everything else in the skill applies.
- **Leave the experiments table in ordinary prose.** Splitting those cells into
  short sentences breaks the cause-and-effect pairing that makes the table
  readable at a glance.

### Section order

1. **How the system collects this data** — bullets. The CGM and that readings are
   automatic. That the user logs insulin and meals by hand. That the analysis is
   fixed arithmetic, not a model or a dose calculator. The purpose of the system.
   **That the record is complete** (see the guardrail below). Units and target
   range.
2. **Overall control** — headline metrics table with a *target* column so the
   reader can judge each figure without knowing the norms. Then by-day and
   by-time-of-day tables. Mark part days at the window edges with a footnote.
   Follow with one short summary sentence naming the main deficit.
3. **Observations from the data** — bullets grouped under glucose pattern,
   insulin (including dose timing and dose sizes), hypoglycaemia, and meals
   (including meal timing, snacking and the foods with the largest responses).
   Facts only. Bold the finding, not the commentary. Give each habit finding its
   count and window — a clinician cannot weigh "inconsistent" but can weigh
   "9 basal doses spread across 21:10 to 02:30".
4. **Suggested changes and experiments** — a table: *question from the data* →
   *suggested experiment* → *what to watch*. This is the section the clinician
   will judge the report by, so every row must trace to a specific finding above.
   Order by likely impact. **No dose, ratio, rate or timing number appears here**
   — the guardrail at the top of this file applies with full force.
5. **Data quality and limits** — split into strengths and limits. Always name:
   that carb figures are estimates, the window length and any part days, and how
   exercise, illness, stress, alcohol and sleep are covered — see the notes rule
   below, and do not keep saying they are not captured at all. Name the insulin
   products from `prescription` — and if that section is absent, list the missing
   products as a limit instead.

### Prescription and basal adherence

`report` emits a `prescription` block and a `basal_adherence` block. Both are
recorded fact, not calculation — treat them accordingly.

- When `prescription.configured` is true, put the products and the prescribed
  basal dose in section 1, **always with the `reviewed` date beside them**. Never
  print a prescription without its age. If the date is more than a year old, say
  so — that is worth a clinician's attention on its own.
- `basal_adherence` gives per-local-day dose counts. State it as adherence
  ("prescribed 36 u, logged on 6 of 7 days"). Days marked `partial` are window
  edges — never report those as missed doses.
- `days_with_multiple` matters as much as `days_with_none`. Two half doses in a
  day is not the same event as one full one.
- When `prescription.configured` is false, say the prescription is not recorded
  and keep the day counts. Do not guess an expected dose from the mode of the
  logged ones — an inferred target is exactly the kind of number this app must
  not invent.
- `matches_insulin_section` tells you whether the experimental bolus reference is
  running on the clinician's own ratios or the user's placeholders. It stays
  false until both `icr` and `isf` appear in `[prescription]` *and* equal the
  `[insulin]` values. **While it is false, the exclusion of `bolus_backtest` from
  the clinician report stands.**

### Clinician-report guardrails

- **Exclude `bolus_backtest` entirely, and say that you excluded it.** Handing a
  clinician calculated dose figures derived from placeholder ISF/ICR invites them
  to read those figures as validated. State in the limits section that the report
  omits an experimental calculation running on unvalidated placeholder values.
- **An absent entry means a missed event, not a missed log.** The user maintains
  the record as authoritative and re-runs the report after fixing any gap. So
  state gaps as findings with consequences ("the patient missed a basal dose on
  <date>"), never as ambiguity ("it cannot be determined whether..."). Say this
  convention explicitly in section 1 so the clinician knows absence is evidence.
  Keep it separate from timestamp precision, which *is* a real limit.
- **Context notes belong in the report, as the patient's own account.** Where a
  note bears on a figure the clinician is reading, put it beside that figure
  ("the patient recorded a fever on 30 and 31 July" next to those days). Summarise
  rather than transcribe, and leave out anything that does not bear on the data —
  this is free text the user wrote for themselves, not a statement prepared for a
  clinician. In the limits section, say plainly how much is there: notes cover
  exercise, illness and stress **only on the days one was written**, so an
  unnoted day is unknown, not uneventful. With no notes at all in the window,
  keep naming those factors as uncaptured.
- Distinguish hypo treatment from uncovered intake when classifying meals with no
  bolus. Check the starting glucose and the distance to the nearest low episode.
  Do not assume — the two look identical in the totals and mean opposite things.
- The report describes **the patient**, not "you". It is a handover document.

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
  `carb_coverage.partial` counts plates where only *some* items were carbed: the
  total understates the meal, so treat those meals' carb figures as soft.

### Habits — insulin, food, and what follows

Four sections exist for behaviour rather than control, and they are the point of
the review as much as TIR is. Totals cannot show any of this: two weeks with the
same units and the same carbs can be two completely different fortnights.

**`insulin_timing`** — per kind (`basal` / `bolus` / `correction`):
- `clock` is the *tightest window* holding every dose of that kind, computed
  circularly so a 23:40 and a 00:10 read as 30 minutes apart, not 23 hours.
  `spread_min` is the habit's looseness: a basal at 22:00 ± 15 min and one
  scattered over five hours give the same weekly units and are not the same
  behaviour. For a once-daily basal, a wide spread is the single most useful
  behavioural finding in this section — a dose drifting later each day shortens
  and lengthens its own coverage.
- `units` (mean/median/min/max) shows whether a kind is one standard dose or a
  range. A basal with a min well under its median usually means a half dose or a
  mistyped entry — say which you think it is and why, and never suggest a size.
- `by_band` (morning / midday / evening / night) localises a correction habit.
  Corrections piling into one band is a different story from corrections
  scattered evenly, and points at that band's meals or basal coverage.

**`meal_timing`** — the shape of the eating day:
- `per_day` and `by_type` counts: how many plates, and whether they are tagged.
  Untagged plates land in `unspecified`; a large `unspecified` count is a
  logging-hygiene note, not a finding about eating.
- `intervals_hours` are the gaps *within* a local day. A short median with a high
  meal count is grazing; a long median with few plates is distinct meals. Read
  `min` beside the snack count before calling anything grazing.
- `overnight_fast_hours` is the last plate of a day to the first of the next —
  the one long stretch the trace gets to settle. A short or shrinking overnight
  break is worth naming, especially beside a poor `hourly` overnight profile.
  Gaps across a day with no meals logged are excluded, so this never reports a
  hole in the record as a fast.
- `by_type` carries each type's own `clock` window. Late or drifting dinners show
  here, and they are the most common driver of an overnight pattern.

**`meal_response_groups`** — the same post-meal rows pooled four ways, so a
pattern no single meal shows becomes visible:
- `by_food` groups every item seen at least `min_occurrences` times, worst mean
  rise first. This is the "which foods spike me" question, and the answer is
  **association only**: a plate is several foods at once, and the same food
  recurs beside different doses, different starting glucose and different days.
  Name a food as *worth watching*, never as proven. Say the n out loud — three
  sightings is a lead, ten is a pattern. `foods_below_threshold` counts the
  one-offs that were not pooled; they are still individual rows in `post_meal`.
- `by_meal_type` and `by_time_of_day` answer which *occasion* runs hot, which is
  usually more actionable than any single food.
- `by_bolus_timing` buckets each meal by where its rapid dose landed relative to
  the plate: `pre-bolus` (≥5 min before), `with meal` (±5 min), `after eating`
  (>5 min after), `no rapid dose` (nothing within 45 min). The mean peak rise per
  bucket is the closest thing in the report to a controlled comparison of the
  user's own habits — but the buckets are not randomised, so confounding is
  everywhere (people pre-bolus the meals they know are big). Report the
  difference and the confound in the same breath.
- Each `post_meal` row now carries `meal_type`, `items` (the actual plate),
  `carbs_complete` and `bolus_lag_min`, so any grouping above can be traced back
  to the meals that made it.

**`daily_intake`** — carbs, calories and rapid insulin per local day, each with a
`*_complete` flag. Use it for the intake trend across the window and for the
carbs-to-insulin relationship day by day. **Never total or average a column whose
flag is false** without saying it is a floor, not a figure.

Rules for this whole area:
- **Describe behaviour, do not moralise about food.** "The three highest mean
  rises are all bread-based" is an observation. "Cut down on bread" is diet
  advice, which is outside this skill exactly as dose advice is. Foods are
  neutral; what varies is how they were covered and when they were eaten.
- **Habit findings need a count and a window.** "Basal ranged 21:10–02:30 across
  9 doses" beats "your basal timing is inconsistent".
- **Separate a snack from a hypo treatment.** A small fast-carb plate at 03:00 is
  probably a treated low, not a habit to change — check the starting glucose and
  the nearest `low_episodes` entry before classifying it.
- **Say when the sample is too thin.** Under ~5 meals in a bucket, or under ~3
  sightings of a food, report the figure as provisional or leave it out.
- **Good habits get named too.** The user asked for both. A tight basal window, a
  consistent overnight break, meals that peak under +3, a pre-bolus habit that is
  clearly working — call these out explicitly and say what is holding them up.
  A review that only lists problems gets read once.

### Context notes (`notes`)

Free text the user logged against a time — illness, exercise, travel, a bad
night. Grouped by local day on the **same day key as `daily`**, so read them side
by side: a day sitting well outside the week's pattern with "vomiting bug, ate
nothing" against it is explained, and reporting it as a control failure would be
wrong. Read the notes **before** the per-day and hourly tables, and let them
inform what you call a pattern.

They are the only qualitative input in the report, so they need their own care:

- **Absent notes mean nothing.** The "an absent entry means a missed event"
  convention is about insulin and meals, where the user maintains the log as
  authoritative. It does **not** extend here. A week with no notes is a week
  nobody wrote anything down in — never conclude from an empty `notes` that the
  patient was well, unstressed, or inactive.
- A note is **the user's own account**, not a measurement. Attribute it ("the
  patient recorded a fever on 30 July"), and never upgrade it to a clinical
  finding or a diagnosis.
- Use a note to **temper** a reading of the data, not to manufacture causation.
  "The two highest days both carry illness notes" is fair. "Illness caused the
  rise" is not — one note beside one bad day is a coincidence you cannot rule on.
- Do not treat notes as a data source to total or trend. There is no arithmetic
  here and none should be invented.

### The bolus reference (`bolus_backtest`, and `ref` on `post_meal` rows)

Present only when an ISF is configured; `available: false` means skip it
entirely. It replays a textbook formula against doses the user already decided
on. **The calculator is on trial here, not the user's dosing.**

What you may do with it:
- Report **agreement drift**: how `mean_abs_delta`, `mean_signed_delta` and
  `within_1u_percent` moved since the last review. That is a fact about the
  model, and it is the main reason the section exists.
- Note **which component** drives a systematic gap — carb cover vs correction —
  since that localises *why* model and human disagree. `iob_units` is context
  beside the figure, not a term in it: the reference never deducts active insulin,
  so a positive Δ on a dose with insulin already on board may be the user
  correctly accounting for a depot the formula deliberately leaves to them.
- Observe that disagreement concentrates somewhere (e.g. large meals, or doses
  with insulin already active) as a **pattern worth understanding**.

What you must not do:
- Do not present `suggested_units` as the dose that should have been given, in
  any phrasing. The user's decisions are the reference point, not the formula's.
- Do not recommend an ISF or ICR value, or a direction to move one. If the model
  is systematically off, that is an observation to take to a clinician.
- Do not read the `n_full_inputs` figure as an accuracy claim when it is small —
  under ~10 fully-logged doses, say the sample is too thin to interpret.
- Ignore events flagged with `missing` (partial inputs); they are already
  excluded from the agreement stats and mean nothing on their own.

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
  watch*, not *by how much*. This includes the configured ISF/ICR and anything
  in `bolus_backtest` — a calculated figure is still a number to change.
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
vs last review, then the patterns as short bullets, then **Habits**, then the
**Talking points** section above, and finish with the non-medical-advice reminder
and one or two data-hygiene suggestions (e.g. carb logging). Keep it tight and
management-focused, not a data dump.

**Habits** is its own section, and it is what the user comes back for. Split it
into **working** and **watch**, in that order, and keep each to a handful of
lines. Draw it from the four habit sections above (insulin timing and sizes,
eating pattern, pooled responses, daily intake). Every line carries the figure it
came from. The bar for inclusion is that the user would not already know it — a
habit is only worth a line if seeing it written down is news. Where a habit and a
number connect, say so ("the two evening lows both follow a correction inside
three hours of the meal bolus"); where they merely coincide, say that instead.
