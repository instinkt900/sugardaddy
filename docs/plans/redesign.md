# Plan: phone for the patient, desktop for the carer

**Status:** built on the `redesign` branch and running on a local staging
instance (see "Staging" below). Nothing here is on `master` until it has been
lived with.

## The problem, boiled down

The complaints were six. Under them are four causes:

1. **The phone is a stack of forms, not a remote control.** Every action is a
   form full of keyboard fields: units, a kind select, a datetime, a note. The
   tabs switch between forms. What the phone should be is a status screen with
   three big actions, and each action a tap-first flow where the keyboard is the
   fallback, not the default.

2. **The two surfaces are split by feature, not by role.** The phone can log a
   meal but cannot show what that meal asked for afterwards. The desktop can, so
   reviewing a meal means switching device. The rule from now on is: **the phone
   can see everything it can log, for today; the desktop owns everything over
   time and everything that configures the phone.**

3. **The food library has three jobs.** It is a quick-pick list for the regulars,
   a carb lookup table, and a dump for every name ever typed (carb-less lines are
   auto-registered as "pending" foods). Saved meals have the same problem: logging
   a named meal silently rewrites the saved meal of that name. **Logging is a
   record of what happened. Reuse is an explicit choice.** The two must never be
   the same action.

4. **Taps fire on touch-down, and overlays open under a finger that is still
   down.** The food suggestions pick on `pointerdown`, so the start of a scroll
   selects whatever is under it. The long-press editor opens while the finger is
   still on the glass, so the release lands on the sheet that just appeared.

The visuals are the fifth item and are separate: a new skin, done with the
restructure because every screen is touched anyway.

## Phone

### Home (no forms on it)

- **Glucose hero:** the big number, the arrow, "3 min ago", and a state word
  (In range / High / Low). The card is tinted by state.
- **Mini chart**, 12 h, with the time at its two ends.
- **Status line:** active insulin and when the last basal was logged.
- **Today:** one timeline of doses, meals and notes, newest first. **Tap** an
  entry to open its detail. Long-press goes away.
- **Bottom bar:** three big buttons: *Insulin*, *Food*, *Note*. Settings
  (reminders, desktop link, version) move behind a gear icon in the header.

### Insulin flow (full-screen sheet)

- Kind as a segmented control: Bolus / Correction / Basal.
- The amount as a big number with − and + buttons (0.5 u steps), plus **preset
  chips** for that kind (set on the desktop). Tapping the number opens the
  keypad for anything odd.
- Context under the amount: active insulin and current glucose. In Correction
  mode the correction half of the reference shows here too.
- Time is "Now" unless changed: chips for −15 min, −30 min, −1 h, or an exact
  time.
- Note is collapsed behind "Add note".

### Food flow (full-screen sheet, two steps)

**Step 1: what's on the plate.** It has:

- **Saved-meal tiles** and **favourite-food chips** (both marked on the
  desktop) at the top. One tap adds them.
- **Search**, which opens its own full-screen list with large rows. The list
  picks on a real tap, never on the start of a scroll. A name that is not in
  the library becomes a **one-off** line: it is logged with the meal and does
  **not** join the library unless "Keep in my foods" is on.
- **Quick carbs:** "something, about 40 g". You enter a number and an optional
  description, with no library entry. The item gets `tags = "estimate"` so
  analysis can tell an estimate from a counted value.
- The plate is listed with − / + count steppers and a running carb total.

**Step 2: dose and log.** It has:

- The plate summary, the meal type (guessed from the time of day; tap to
  change) and the time.
- The **bolus reference** panel, unchanged in what it says: stacked
  components, IOB beside the total and never taken off it, partial figures
  marked with an asterisk (see `insulin-awareness.md`).
- An **optional dose**, entered with the same stepper and chips as the Insulin
  flow. It is **never prefilled from the reference**. A prefilled number turns
  a cross-check into a directive. The panel shows the reference and you enter
  your own figure.
- One button: "Log meal", or "Log meal + 6 u" when a dose is entered. The
  button is the loudest thing on the screen, as before.

**Saved meals become read-only during logging.** Loading "Red Rooster" fills the
plate and marks it "from Red Rooster". If the plate is then changed, a quiet
"Also update the saved meal" switch appears, **off** by default. Logging no
longer upserts a saved meal by name. Saved meals are made and edited on the
desktop, or with that explicit switch.

### Entry detail (tap an entry on Home)

- **Dose:** the amount, kind, time and note, with Edit and Delete.
- **Meal:** the plate, the carbs, the insulin given with it, **the reference
  for that meal** (the glucose and IOB at the time, the same arithmetic as the
  desktop post-meal table), and the response so far (start, peak, change).
  Edit opens the plate in the same builder, and the reference recalculates
  live **against the meal's own moment**, not against now.

### Touch rules (applied everywhere)

- Pick and act on `click` (a completed tap), never on `pointerdown`.
- A sheet that opens ignores pointer input for its first ~350 ms. A backdrop
  closes the sheet only when the press **started and ended** on the backdrop.
- Destructive actions are never where a finger lands after opening a sheet.
  Delete sits at the bottom of a detail view and always asks to confirm.

## Desktop: the management console

The single long page splits into sections with a nav:

- **Overview:** the stats, the main chart and the daily intake (as now).
- **Logbook:** doses, meals and notes in their tables with inline editing. The
  meal editor now shows the reference **for existing meals too** (at the
  meal's moment), so "what did that plate ask for, and what does it ask for
  now that I've fixed the chips?" is answered in place.
- **Meals:** the post-meal response table.
- **Library:** foods (with a favourite star, use count and last used),
  saved meals (with a favourite star), and the **missing-carbs to-do**.
- **Phone:** dose presets (kind + units chips, seeded from history and the
  prescription) and a preview of the phone's shortcut set.

## Data changes (all additive migrations in `db.init_db`)

- `foods.favourite`, `meal_templates.favourite` (`INTEGER NOT NULL DEFAULT 0`).
- `settings` key/value table (JSON values): `phone.dose_presets`.
- `meal_items.tags = "estimate"` for quick-carb lines (the column exists but is
  unused).
- `GET /api/bolus-reference?carbs=N&meal_id=ID` evaluates against the meal's
  own moment (the nearest reading and the IOB from earlier doses) instead of
  now. It uses the same context rules as `analysis.post_meal_responses`.
- `GET /api/meal/{id}/insight` returns the meal's post-meal row: response,
  bolus and reference.
- The **missing-carbs to-do moves from foods to meal items.** It is grouped by
  name, and filling a name backfills the blank items with that name (`IS NULL`
  only, as before). Carb-less one-off lines no longer create library rows.
  Library foods that are already pending keep working as they do now.

## Visuals

The new design tokens follow the system light/dark setting. The look is calm,
with large, rounded numerals, generous spacing and soft surfaces instead of
outlined boxes. Glucose state is shown by colour on the hero card and nowhere
else, so colour keeps its meaning. The palette is defined once in
`style.css :root` and shared by both surfaces.

## Staging

`deploy/staging.sh` runs the branch on this machine from the gitignored
`.staging/` directory: a snapshot of the live DB plus a config with no
LibreLinkUp, notify or VictoriaMetrics sections, so it can't poll the account
or write anywhere but its own copy. Glucose is frozen at the snapshot time.
Refresh the snapshot with the same `sqlite3` backup used to make it, then
restart.
