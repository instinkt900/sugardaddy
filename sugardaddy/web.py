"""FastAPI app: one backend, two UIs.

Phone UI (``/``)     — input-first, minimal, HTMX quick-logging.
Desktop UI (``/desktop``) — review-first, big charts + tables with full CRUD.

Both share the same JSON API and SQLite DB, so anything logged or edited on one
surface shows up on the other. The background glucose poller is started on app
startup so a single ``serve`` process does everything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sugardaddy import __version__
from sugardaddy.analysis import post_meal_responses, summarize
from sugardaddy.config import Config, load_config
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
)

log = logging.getLogger("sugardaddy.web")

_HERE = Path(__file__).parent
_DAY = 24 * 60 * 60


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

    def local_str(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
        return datetime.fromtimestamp(ts, tz).strftime(fmt)

    def local_input(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%dT%H:%M")

    # --- serialization ---------------------------------------------------

    def range_from_query(request: Request, default_span: int = _DAY) -> tuple[int, int]:
        now = now_epoch()
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
        return {"doses": doses, "meals": meals, "units": cfg.web.units}

    def current_context() -> dict:
        r = db.latest_reading()
        if r is None:
            return {"has_reading": False}
        mins = round((now_epoch() - r.ts_utc) / 60)
        return {
            "has_reading": True,
            "value": to_display(r.value_mgdl, cfg.web.units),
            "units": cfg.web.units,
            "trend": trend_arrow(r.trend),
            "minutes_ago": mins,
            "in_range": cfg.target_low_mgdl <= r.value_mgdl <= cfg.target_high_mgdl,
            "is_low": r.value_mgdl < cfg.target_low_mgdl,
            "is_high": r.value_mgdl > cfg.target_high_mgdl,
        }

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

    @app.get("/api/current")
    def api_current():
        return current_context()

    @app.get("/api/timeline")
    def api_timeline(request: Request):
        start, end = range_from_query(request)
        readings = db.readings_between(start, end)
        return {
            "units": cfg.web.units,
            "target_low": cfg.web.target_low,
            "target_high": cfg.web.target_high,
            "glucose": [
                {"t": r.ts_utc * 1000, "v": to_display(r.value_mgdl, cfg.web.units)}
                for r in readings
            ],
            "doses": [dose_json(d) for d in db.doses_between(start, end)],
            "meals": [meal_json(m) for m in db.meals_between(start, end)],
        }

    @app.get("/api/entries")
    def api_entries(request: Request):
        start, end = range_from_query(request)
        return {
            "doses": [dose_json(d) for d in reversed(db.doses_between(start, end))],
            "meals": [meal_json(m) for m in reversed(db.meals_between(start, end))],
        }

    @app.get("/api/stats")
    def api_stats(request: Request):
        start, end = range_from_query(request)
        readings = db.readings_between(start, end)
        meals = db.meals_between(start, end)
        summary = summarize(readings, cfg.target_low_mgdl, cfg.target_high_mgdl, cfg.web.units)
        return {
            "summary": summary.as_dict(),
            "post_meal": post_meal_responses(readings, meals, cfg.web.units),
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

    # --- lifecycle -------------------------------------------------------

    if start_ingest:
        @app.on_event("startup")
        def _startup():
            if not (cfg.librelink.email and cfg.librelink.password):
                log.warning(
                    "no LibreLinkUp credentials (SUGARDADDY_LIBRE_EMAIL/PASSWORD) — "
                    "glucose ingestion disabled; manual logging still works"
                )
                return
            start_background(cfg, db)
            log.info("glucose ingestion started")

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
