"""FastAPI app: one backend, two UIs.

Phone UI (``/``)     — input-first, minimal, HTMX quick-logging.
Desktop UI (``/desktop``) — review-first, big charts + tables with full CRUD.

Both share the same JSON API and SQLite DB, so anything logged or edited on one
surface shows up on the other. The background glucose poller is started on app
startup so a single ``serve`` process does everything.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sugardaddy import __version__, notify
from sugardaddy.analysis import (
    daily_breakdown,
    daily_intake,
    day_coverage,
    day_window_start,
    post_meal_responses,
    smooth_glucose,
    summarize,
)
from sugardaddy.bolus import bolus_reference
from sugardaddy.config import Config, load_config
from sugardaddy.iob import active_activity, active_iob, activity_phase, is_rapid
from sugardaddy.constants import INSULIN_KINDS, MEAL_TYPES, to_display, trend_arrow
from sugardaddy.db import Database
from sugardaddy.ingest import start_background
from sugardaddy.models import (
    Food,
    InsulinDose,
    Meal,
    MealItem,
    MealTemplate,
    MealTemplateItem,
    Note,
)

log = logging.getLogger("sugardaddy.web")

_HERE = Path(__file__).parent
_DAY = 24 * 60 * 60

# Below this much of a day covered by CGM data, its average glucose is really an
# average of the hours the sensor was on — worth flagging rather than printing as
# a flat fact next to days that were covered end to end.
_THIN_DAY_COVERAGE = 0.7

# How old the latest reading may be before the *live* bolus reference stops
# correcting against it. Retrospective figures don't have this problem — they read
# the glucose that was actually there — but a correction computed off a reading
# from an hour ago is a guess wearing a decimal point, so past this the glucose
# half is dropped and the figure is marked incomplete instead.
_REF_STALE_SECONDS = 20 * 60


def _opt_num(v) -> float | None:
    """Parse an optional numeric field: blank/None -> None, else float or None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _num(v, default: float) -> float:
    n = _opt_num(v)
    return n if n is not None else default


def _opt_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_meal_items(raw) -> list[MealItem]:
    """Build MealItem snapshots from a JSON items array (unnamed lines dropped)."""
    items: list[MealItem] = []
    for it in raw or []:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        items.append(
            MealItem(
                name=name,
                count=_num(it.get("count"), 1) or 1,
                carbs_g=_opt_num(it.get("carbs_g")),
                calories=_opt_num(it.get("calories")),
                description=(it.get("description") or "").strip(),
                tags=(it.get("tags") or "").strip(),
                food_id=_opt_int(it.get("food_id")),
            )
        )
    return items


def _parse_template_items(raw) -> list[MealTemplateItem]:
    items: list[MealTemplateItem] = []
    for it in raw or []:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        items.append(
            MealTemplateItem(
                name=name,
                count=_num(it.get("count"), 1) or 1,
                carbs_g=_opt_num(it.get("carbs_g")),
                calories=_opt_num(it.get("calories")),
                food_id=_opt_int(it.get("food_id")),
            )
        )
    return items


def _tz(cfg: Config) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(cfg.web.timezone)
    except Exception:  # pragma: no cover - bad tz name / missing tzdata
        log.warning("unknown timezone %r; using UTC", cfg.web.timezone)
        return timezone.utc


def create_app(config_path: str, *, start_ingest: bool = True) -> FastAPI:
    cfg = load_config(config_path)
    db = Database(cfg.database.path)
    db.init_db()
    tz = _tz(cfg)

    # Derived once at startup: "" means notifications are off or misconfigured,
    # which the phone's toggle reads as "unavailable" rather than as an error.
    vapid_public_key = notify.public_key(cfg)
    if cfg.notify.enabled and not vapid_public_key:
        log.warning("[notify] is enabled but no usable VAPID key — push is disabled")

    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app = FastAPI(title="Sugar Daddy", version=__version__)
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):
        # Force the browser to revalidate static assets so JS/CSS updates always
        # take effect on reload (cheap 304s via ETag). Avoids stale-cache confusion.
        resp = await call_next(request)
        if request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    # --- time helpers ----------------------------------------------------

    def now_epoch() -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def parse_local(ts_str: str | None) -> int:
        """Parse a datetime-local string (local tz) to UTC epoch; blank = now."""
        if not ts_str:
            return now_epoch()
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return int(dt.timestamp())

    def local_str(ts: int, fmt: str = "%d/%m/%y %H:%M") -> str:
        """Display stamp for tables/lists — day-first, matching the AU locale the
        app is set up for. Distinct from local_input(), which must stay ISO."""
        return datetime.fromtimestamp(ts, tz).strftime(fmt)

    def local_input(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%dT%H:%M")

    # --- serialization ---------------------------------------------------

    def range_from_query(request: Request, default_span: int = _DAY) -> tuple[int, int]:
        """Resolve the display window from `from`/`to`, or from `hours`.

        `hours=N` means "the last N hours", ending at the server's now. It exists
        for the phone, whose chart is a fixed window with no picker: asking for a
        span keeps the arithmetic on the side that owns the clock, so a device
        running a few minutes off doesn't pin the x axis away from its own data.
        The desktop, which has a picker and genuinely means *those* instants,
        keeps sending explicit `from`/`to`.
        """
        now = now_epoch()
        hours = _opt_num(request.query_params.get("hours"))
        if hours:
            span = int(max(1.0, min(hours, 24 * 365)) * 3600)
            return now - span, now
        try:
            end = int(request.query_params.get("to", now))
            start = int(request.query_params.get("from", end - default_span))
        except ValueError:
            start, end = now - default_span, now
        return start, end

    def dose_json(d: InsulinDose) -> dict:
        return {
            "id": d.id,
            "t": d.ts_utc * 1000,
            "ts_utc": d.ts_utc,
            "local": local_str(d.ts_utc),
            "input": local_input(d.ts_utc),
            "units": d.units,
            "kind": d.kind,
            "note": d.note,
        }

    def note_json(n: Note) -> dict:
        return {
            "id": n.id,
            "t": n.ts_utc * 1000,
            "ts_utc": n.ts_utc,
            "local": local_str(n.ts_utc),
            "input": local_input(n.ts_utc),
            "text": n.text,
        }

    def meal_item_json(i: MealItem) -> dict:
        return {
            "id": i.id,
            "food_id": i.food_id,
            "name": i.name,
            "description": i.description,
            "carbs_g": i.carbs_g,
            "calories": i.calories,
            "count": i.count,
            "tags": i.tags,
        }

    def meal_json(m: Meal) -> dict:
        return {
            "id": m.id,
            "t": m.ts_utc * 1000,
            "ts_utc": m.ts_utc,
            "local": local_str(m.ts_utc),
            "input": local_input(m.ts_utc),
            "name": m.name,
            "meal_type": m.meal_type,
            "note": m.note,
            "label": m.label,
            "total_carbs": m.total_carbs,
            "total_calories": m.total_calories,
            "items": [meal_item_json(i) for i in m.items],
        }

    def food_json(f: Food) -> dict:
        return {
            "id": f.id,
            "name": f.name,
            "description": f.description,
            "carbs_g": f.carbs_g,
            "calories": f.calories,
        }

    def meal_template_json(t: MealTemplate) -> dict:
        return {
            "id": t.id,
            "name": t.name,
            "items": [
                {
                    "id": i.id,
                    "food_id": i.food_id,
                    "name": i.name,
                    "carbs_g": i.carbs_g,
                    "calories": i.calories,
                    "count": i.count,
                }
                for i in t.items
            ],
        }

    def recent_context() -> dict:
        start, end = now_epoch() - _DAY, now_epoch()
        doses = [dose_json(d) for d in reversed(db.doses_between(start, end))]
        meals = [meal_json(m) for m in reversed(db.meals_between(start, end))]
        notes = [note_json(n) for n in reversed(db.notes_between(start, end))]
        return {"doses": doses, "meals": meals, "notes": notes, "units": cfg.web.units}

    def current_context() -> dict:
        now = now_epoch()
        # Current rapid-acting insulin-on-board from doses inside the action
        # window. Independent of glucose, so it is reported even with no reading.
        recent = db.doses_between(now - cfg.insulin.dia_minutes * 60, now)
        iob = active_iob(
            recent,
            now,
            dia_minutes=cfg.insulin.dia_minutes,
            peak_minutes=cfg.insulin.peak_minutes,
        )
        # "Where on the ride" the aggregate insulin action is, for a compact phone
        # tile — a % of the batch's peak action rate plus a rising/falling label.
        phase = activity_phase(
            recent, now, dia_minutes=cfg.insulin.dia_minutes, peak_minutes=cfg.insulin.peak_minutes
        )
        ctx = {
            "iob": round(iob, 1),
            "iob_dose_count": sum(1 for d in recent if is_rapid(d)),
            "activity_pct": round(phase[0] * 100) if phase else None,
            "activity_dir": {1: "rising", -1: "falling", 0: "peak"}[phase[1]] if phase else None,
        }

        r = db.latest_reading()
        if r is None:
            ctx["has_reading"] = False
            return ctx
        mins = round((now - r.ts_utc) / 60)
        ctx.update(
            {
                "has_reading": True,
                "value": to_display(r.value_mgdl, cfg.web.units),
                "units": cfg.web.units,
                "trend": trend_arrow(r.trend),
                "minutes_ago": mins,
                "in_range": cfg.target_low_mgdl <= r.value_mgdl <= cfg.target_high_mgdl,
                "is_low": r.value_mgdl < cfg.target_low_mgdl,
                "is_high": r.value_mgdl > cfg.target_high_mgdl,
            }
        )
        return ctx

    # --- pages -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def phone(request: Request):
        return templates.TemplateResponse(
            request,
            "phone/index.html",
            {
                "cfg": cfg,
                "current": current_context(),
                "recent": recent_context(),
                "kinds": INSULIN_KINDS,
                "meal_types": MEAL_TYPES,
                "now_input": local_input(now_epoch()),
                "version": __version__,
            },
        )

    @app.get("/desktop", response_class=HTMLResponse)
    def desktop(request: Request):
        return templates.TemplateResponse(
            request,
            "desktop/dashboard.html",
            {
                "cfg": cfg,
                "kinds": INSULIN_KINDS,
                "meal_types": MEAL_TYPES,
                "now_input": local_input(now_epoch()),
                "version": __version__,
            },
        )

    # --- JSON API --------------------------------------------------------

    @app.get("/healthz", response_class=JSONResponse)
    def healthz():
        return {"status": "ok", "readings": db.reading_count()}

    # --- PWA (installable web app) --------------------------------------

    _MANIFEST = {
        "name": "Sugar Daddy",
        "short_name": "Sugar Daddy",
        "description": "Glucose, insulin and meal logging",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#12141a",
        "theme_color": "#12141a",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
    }

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse(_MANIFEST, media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        # Served from root so its scope covers the whole app (a SW under /static/
        # could only control /static/). no-cache so updates roll out on reload.
        return FileResponse(
            _HERE / "static" / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/favicon.ico")
    def favicon():
        # Browsers request /favicon.ico from the root on their own, whatever the
        # <link> tags say, so answer there as well as from /static/.
        return FileResponse(
            _HERE / "static" / "icons" / "favicon.ico",
            media_type="image/x-icon",
        )

    @app.get("/api/current")
    def api_current():
        return current_context()

    @app.get("/api/bolus-reference")
    def api_bolus_reference(request: Request):
        """EXPERIMENTAL live reference for the meal currently being built.

        The same `bolus.bolus_reference` arithmetic the desktop post-meal table
        replays retrospectively, evaluated against *now* instead: current glucose,
        current rapid-acting IOB, and whatever carbs the phone's plate adds up to
        so far. It answers "what does the formula make of this plate?" so the user
        can reconcile it against the dose they were already going to give — it is
        not, and must never be worded as, an instruction to give that amount.

        `carbs` is optional: an empty plate still yields the correction half, which
        `missing` flags as incomplete so a small figure can't read as "no dose
        needed" when the food simply hasn't been entered yet.

        Gated on a configured ISF exactly like the retrospective surfaces —
        `enabled: false` is the whole answer when there is none, and the phone
        hides its panel on that alone rather than showing a guessed number.
        """
        if cfg.isf_mgdl is None:
            return {"enabled": False}

        now = now_epoch()
        doses = db.doses_between(now - cfg.insulin.dia_minutes * 60, now)
        iob = active_iob(
            doses,
            now,
            dia_minutes=cfg.insulin.dia_minutes,
            peak_minutes=cfg.insulin.peak_minutes,
        )
        r = db.latest_reading()
        minutes_ago = round((now - r.ts_utc) / 60) if r else None
        stale = r is None or (now - r.ts_utc) > _REF_STALE_SECONDS
        ref = bolus_reference(
            bg_mgdl=None if stale else r.value_mgdl,
            target_mgdl=cfg.bolus_target_mgdl,
            isf_mgdl_per_unit=cfg.isf_mgdl,
            icr_g_per_unit=cfg.insulin.icr,
            carbs_g=_opt_num(request.query_params.get("carbs")),
            iob_units=iob,
        )
        return {
            "enabled": True,
            "units": cfg.web.units,
            # The inputs behind the figure, so the panel can show what it read
            # rather than just what it concluded.
            "glucose": to_display(r.value_mgdl, cfg.web.units) if r else None,
            "minutes_ago": minutes_ago,
            "glucose_stale": stale,
            "target": to_display(cfg.bolus_target_mgdl, cfg.web.units),
            "ref": ref.as_dict(),
        }

    @app.get("/api/timeline")
    def api_timeline(request: Request):
        start, end = range_from_query(request)
        readings = db.readings_between(start, end)
        # Active-insulin curve across the window: aggregate rapid-acting IOB
        # sampled on a regular grid. Pull doses from a DIA before the start so the
        # value is correct at the left edge; cap the grid at ~500 points so long
        # ranges stay light. Shares iob.active_iob with the rest of the app.
        dia = cfg.insulin.dia_minutes
        peak = cfg.insulin.peak_minutes
        iob_doses = db.doses_between(start - dia * 60, end)
        step = max(300, (end - start) // 500)
        iob = []
        activity = []  # rate of insulin action (u/hr), the derivative of IOB
        t = start
        while t <= end:
            ms = t * 1000
            iob.append({"t": ms, "v": round(active_iob(iob_doses, t, dia_minutes=dia, peak_minutes=peak), 2)})
            activity.append(
                {"t": ms, "v": round(active_activity(iob_doses, t, dia_minutes=dia, peak_minutes=peak) * 60, 3)}
            )
            t += step
            # The stride rarely divides the window evenly; land a final sample on
            # `end` so the curves reach the right edge instead of stopping short.
            if t > end and t - step < end:
                t = end
        return {
            "units": cfg.web.units,
            "target_low": cfg.web.target_low,
            "target_high": cfg.web.target_high,
            # The resolved window, so charts can pin their x axis to it rather
            # than letting it drift with whatever data happens to exist.
            "from": start * 1000,
            "to": end * 1000,
            "glucose": [
                {"t": r.ts_utc * 1000, "v": to_display(r.value_mgdl, cfg.web.units)}
                for r in readings
            ],
            # Optional trend line with the sensor's ~30-minute ringing filtered
            # out. Shipped alongside the raw series rather than instead of it —
            # the smoothed line is easier to read but it is derived, and the
            # readings are what actually happened.
            "smoothed": [
                {"t": p["ts_utc"] * 1000, "v": p["value"]}
                for p in smooth_glucose(readings, cfg.web.units)
            ],
            "doses": [dose_json(d) for d in db.doses_between(start, end)],
            "meals": [meal_json(m) for m in db.meals_between(start, end)],
            "notes": [note_json(n) for n in db.notes_between(start, end)],
            "iob": iob,
            "activity": activity,
        }

    @app.get("/api/entries")
    def api_entries(request: Request):
        start, end = range_from_query(request)
        return {
            "doses": [dose_json(d) for d in reversed(db.doses_between(start, end))],
            "meals": [meal_json(m) for m in reversed(db.meals_between(start, end))],
            "notes": [note_json(n) for n in reversed(db.notes_between(start, end))],
        }

    @app.get("/api/stats")
    def api_stats(request: Request):
        start, end = range_from_query(request)
        readings = db.readings_between(start, end)
        meals = db.meals_between(start, end)
        # IOB at a meal can draw on a dose taken up to a DIA before it, so widen
        # the dose window past the display range's start.
        doses = db.doses_between(start - cfg.insulin.dia_minutes * 60, end)
        summary = summarize(readings, cfg.target_low_mgdl, cfg.target_high_mgdl, cfg.web.units)
        post_meal = post_meal_responses(
            readings,
            meals,
            cfg.web.units,
            doses,
            dia_minutes=cfg.insulin.dia_minutes,
            peak_minutes=cfg.insulin.peak_minutes,
            # Experimental bolus reference — absent from the payload unless an
            # ISF is configured, which is what keeps the UI columns hidden.
            isf_mgdl=cfg.isf_mgdl,
            icr=cfg.insulin.icr,
            target_mgdl=cfg.bolus_target_mgdl,
        )
        # Stamp each row here rather than in analysis.py (which stays pure and
        # tz-free), so this table reads in the *configured* timezone like the
        # insulin/meal tables instead of whatever the browser happens to be in.
        for row in post_meal:
            row["local"] = local_str(row["ts_utc"])
        return {"summary": summary.as_dict(), "post_meal": post_meal}

    @app.get("/api/daily")
    def api_daily(request: Request):
        """Per-day intake on its own whole-day window, in local days.

        Deliberately *not* driven by the chart's range picker. That window is an
        arbitrary span ending at now, so it cuts its oldest day in half — and half
        a day of food in a column next to whole days doesn't read as clipped, it
        reads as a light day. Here the window always starts at local midnight, so
        every row is a whole day except today, which is flagged as still running
        and left out of the average.
        """
        try:
            days = int(request.query_params.get("days", 7))
        except ValueError:
            days = 7
        days = max(1, min(days, 365))  # a year of rows is already unreadable
        now = now_epoch()
        start = day_window_start(now, tz, days)
        rows = daily_intake(db.meals_between(start, now), db.doses_between(start, now), tz)
        # Glucose for the same days comes from daily_breakdown rather than a second
        # rollup here: it already buckets by local day with the same key, so the two
        # line up by construction (pinned by a test) and there is no new maths to
        # get wrong. Only the average is used; the rest of its stats stay for report.
        readings = db.readings_between(start, now)
        glucose = {
            d["day"]: d
            for d in daily_breakdown(
                readings,
                cfg.target_low_mgdl,
                cfg.target_high_mgdl,
                cfg.web.units,
                tz,
            )
        }
        coverage = day_coverage(readings, tz)
        today = datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d")
        for row in rows:
            # Day-first label, stamped here for the same reason as `local` above —
            # analysis.py stays free of display formatting.
            row["label"] = datetime.strptime(row["day"], "%Y-%m-%d").strftime("%a %d/%m")
            row["in_progress"] = row["day"] == today
            g = glucose.get(row["day"])
            row["glucose_avg"] = g["avg"] if g else None
            row["reading_count"] = g["n"] if g else 0
            row["glucose_coverage"] = round(coverage.get(row["day"], 0.0), 3)
            # A day the sensor only half-covered gives an average that isn't
            # comparable to a full one, so it gets the same "*" as a partial carb
            # total. Today is exempt: it is short by definition and already says so.
            row["glucose_complete"] = (
                row["in_progress"] or row["glucose_coverage"] >= _THIN_DAY_COVERAGE
            )
        return {
            "days": days,
            "units": cfg.web.units,
            "from": start * 1000,
            "to": now * 1000,
            "rows": rows,
        }

    # --- create (phone HTMX + desktop) ----------------------------------

    def _wants_partial(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    async def _form_or_json(request: Request) -> dict:
        """Accept either a JSON body or an HTML form post (foods can be created
        from the desktop table via FormData or the phone via JSON)."""
        if request.headers.get("content-type", "").startswith("application/json"):
            return await request.json()
        return dict(await request.form())

    @app.post("/api/insulin")
    def create_insulin(
        request: Request,
        units: float = Form(...),
        kind: str = Form("bolus"),
        ts: str = Form(""),
        note: str = Form(""),
    ):
        kind = kind if kind in INSULIN_KINDS else "bolus"
        dose = InsulinDose(ts_utc=parse_local(ts), units=units, kind=kind, note=note)
        dose.id = db.add_dose(dose)
        if _wants_partial(request):
            return _recent_partial(request)
        return dose_json(dose)

    @app.post("/api/meal")
    async def create_meal(request: Request):
        """Log a composite meal (plate of snapshot items) from a JSON body:
        ``{ts, name, note, items:[{food_id,name,carbs_g,calories,count,...}]}``."""
        body = await request.json()
        name = (body.get("name") or "").strip()
        meal = Meal(
            ts_utc=parse_local(body.get("ts")),
            name=name,
            meal_type=(body.get("meal_type") or "").strip(),
            note=(body.get("note") or "").strip(),
            items=_parse_meal_items(body.get("items")),
        )
        meal.id = db.add_meal(meal)
        # A named meal is also saved to the library — created, or updated by name.
        if name:
            db.upsert_meal_template(name, _parse_template_items(body.get("items")))
        return meal_json(db.get_meal(meal.id))

    @app.post("/api/note")
    def create_note(
        request: Request,
        text: str = Form(...),
        ts: str = Form(""),
    ):
        """Log a free-text context event. Posted as a form from the phone's Note
        tab, so it answers with the recent partial there and JSON elsewhere —
        the same shape as the insulin post."""
        text = text.strip()
        if not text:
            # A blank note records nothing but the time, which is worse than no
            # row at all. HTMX leaves the list alone on a 4xx.
            return JSONResponse({"error": "text required"}, status_code=400)
        note = Note(ts_utc=parse_local(ts), text=text)
        note.id = db.add_note(note)
        if _wants_partial(request):
            return _recent_partial(request)
        return note_json(note)

    @app.get("/api/recent", response_class=HTMLResponse)
    def api_recent(request: Request):
        """The recent-entries partial, so the phone can refresh it after a
        JSON meal POST (insulin still gets the partial from its HTMX post)."""
        return _recent_partial(request)

    def _recent_partial(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "partials/recent.html", {"recent": recent_context()}
        )

    # --- edit / delete (desktop JSON) -----------------------------------

    @app.patch("/api/insulin/{dose_id}")
    async def update_insulin(dose_id: int, request: Request):
        body = await request.json()
        fields = {}
        if "ts" in body:
            fields["ts_utc"] = parse_local(body["ts"])
        for k in ("units", "kind", "note"):
            if k in body:
                fields[k] = body[k]
        ok = db.update_dose(dose_id, **fields)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.delete("/api/insulin/{dose_id}")
    def delete_insulin(dose_id: int):
        ok = db.delete_dose(dose_id)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.patch("/api/meal/{meal_id}")
    async def update_meal(meal_id: int, request: Request):
        body = await request.json()
        fields = {}
        if "ts" in body:
            fields["ts_utc"] = parse_local(body["ts"])
        if "name" in body:
            fields["name"] = (body["name"] or "").strip()
        if "meal_type" in body:
            fields["meal_type"] = (body["meal_type"] or "").strip()
        if "note" in body:
            fields["note"] = (body["note"] or "").strip()
        items = _parse_meal_items(body["items"]) if "items" in body else None
        ok = db.update_meal(meal_id, items=items, **fields)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.delete("/api/meal/{meal_id}")
    def delete_meal(meal_id: int):
        ok = db.delete_meal(meal_id)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.patch("/api/note/{note_id}")
    async def update_note(note_id: int, request: Request):
        body = await request.json()
        fields = {}
        if "ts" in body:
            fields["ts_utc"] = parse_local(body["ts"])
        if "text" in body:
            # Same guard as the create: emptying a note leaves a row that records
            # a time and nothing else. Deleting it is the way to get rid of it.
            text = (body["text"] or "").strip()
            if not text:
                return JSONResponse({"error": "text required"}, status_code=400)
            fields["text"] = text
        ok = db.update_note(note_id, **fields)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.delete("/api/note/{note_id}")
    def delete_note(note_id: int):
        ok = db.delete_note(note_id)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    # --- foods (library) ------------------------------------------------

    @app.get("/api/foods")
    def list_foods():
        return [food_json(f) for f in db.list_foods()]

    @app.post("/api/foods")
    async def create_food(request: Request):
        body = await _form_or_json(request)
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        food = Food(
            name=name,
            description=(body.get("description") or "").strip(),
            carbs_g=_opt_num(body.get("carbs_g")),
            calories=_opt_num(body.get("calories")),
        )
        # add_food upserts by name — return the stored (possibly merged) row.
        return food_json(db.get_food(db.add_food(food)))

    @app.patch("/api/foods/{food_id}")
    async def update_food(food_id: int, request: Request):
        body = await request.json()
        fields = {}
        if "name" in body and body["name"].strip():
            other = db.get_food_by_name(body["name"])
            if other and other.id != food_id:
                return JSONResponse(
                    {"error": "a food with that name already exists"}, status_code=409
                )
            fields["name"] = body["name"].strip()
        if "description" in body:
            fields["description"] = (body["description"] or "").strip()
        if "carbs_g" in body:
            fields["carbs_g"] = _opt_num(body["carbs_g"])
        if "calories" in body:
            fields["calories"] = _opt_num(body["calories"])
        ok = db.update_food(food_id, **fields)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.delete("/api/foods/{food_id}")
    def delete_food(food_id: int):
        ok = db.delete_food(food_id)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    # --- meal templates (saved meals) -----------------------------------

    @app.get("/api/meal-templates")
    def list_meal_templates():
        return [meal_template_json(t) for t in db.list_meal_templates()]

    @app.post("/api/meal-templates")
    async def create_meal_template(request: Request):
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        # Upsert by name so re-saving a name updates the existing saved meal.
        tid = db.upsert_meal_template(name, _parse_template_items(body.get("items")))
        return {"id": tid, "name": name}

    @app.patch("/api/meal-templates/{template_id}")
    async def update_meal_template(template_id: int, request: Request):
        body = await request.json()
        name = body.get("name") if "name" in body else None
        if name is not None and name.strip():
            other = db.get_meal_template_id_by_name(name)
            if other and other != template_id:
                return JSONResponse(
                    {"error": "a saved meal with that name already exists"}, status_code=409
                )
        items = _parse_template_items(body["items"]) if "items" in body else None
        ok = db.update_meal_template(template_id, name=name, items=items)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.delete("/api/meal-templates/{template_id}")
    def delete_meal_template(template_id: int):
        ok = db.delete_meal_template(template_id)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    # --- push subscriptions ----------------------------------------------

    @app.get("/api/push/key")
    def push_key():
        """The applicationServerKey a browser needs in order to subscribe.

        503 rather than an empty key so the phone can tell "push isn't set up on
        this server" apart from "this browser can't do push"."""
        if not vapid_public_key:
            return JSONResponse({"error": "push not configured"}, status_code=503)
        return {"key": vapid_public_key}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request):
        body = await request.json()
        endpoint = (body.get("endpoint") or "").strip()
        keys = body.get("keys") or {}
        p256dh = (keys.get("p256dh") or "").strip()
        auth = (keys.get("auth") or "").strip()
        # Without both keys the push service could only ever deliver an empty
        # wake-up, so an incomplete subscription is worth rejecting outright.
        if not (endpoint and p256dh and auth):
            return JSONResponse({"error": "malformed subscription"}, status_code=400)
        sub_id = db.add_subscription(
            endpoint, p256dh, auth, (body.get("label") or "").strip(), now_epoch()
        )
        return {"id": sub_id}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(request: Request):
        body = await request.json()
        ok = db.delete_subscription((body.get("endpoint") or "").strip())
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.post("/api/push/test")
    async def push_test():
        """Send a throwaway notification to every subscribed device — the quickest
        way to prove the whole chain (VAPID signing, push service, service worker)
        works without waiting for a basal to actually go unlogged."""
        if not vapid_public_key:
            return JSONResponse({"error": "push not configured"}, status_code=503)
        payload = {
            "title": "Sugar Daddy",
            "body": "Test notification — push is working.",
            "url": "/",
            "tag": "sugardaddy-test",
            "renotify": True,
        }
        # Blocking network I/O, one request per subscription: off the event loop.
        return await asyncio.to_thread(notify.send_to_all, db, cfg, payload, now_epoch())

    # --- lifecycle -------------------------------------------------------

    if start_ingest:
        @app.on_event("startup")
        def _startup():
            if cfg.librelink.email and cfg.librelink.password:
                start_background(cfg, db)
                log.info("glucose ingestion started")
            else:
                log.warning(
                    "no LibreLinkUp credentials (SUGARDADDY_LIBRE_EMAIL/PASSWORD) — "
                    "glucose ingestion disabled; manual logging still works"
                )
            # Deliberately independent of ingestion: the basal reminder reads only
            # the dose log, so it works with no glucose feed at all.
            if vapid_public_key and cfg.notify.poll_seconds > 0:
                notify.start_background(cfg, db, tz)
                log.info(
                    "basal reminder started (checking every %ds)", cfg.notify.poll_seconds
                )

    app.state.config = cfg
    app.state.db = db
    return app


def run_serve(config_path: str) -> int:
    import uvicorn

    cfg = load_config(config_path)
    app = create_app(config_path)
    log.info("serving on http://%s:%d (phone: / , desktop: /desktop)", cfg.web.host, cfg.web.port)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level="warning")
    return 0
