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
from sugardaddy.constants import INSULIN_KINDS, to_display, trend_arrow
from sugardaddy.db import Database
from sugardaddy.ingest import start_background
from sugardaddy.models import InsulinDose, KnownMeal, Meal

log = logging.getLogger("sugardaddy.web")

_HERE = Path(__file__).parent
_DAY = 24 * 60 * 60


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
    app = FastAPI(title="sugardaddy", version=__version__)
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

    def meal_json(m: Meal) -> dict:
        return {
            "id": m.id,
            "t": m.ts_utc * 1000,
            "ts_utc": m.ts_utc,
            "local": local_str(m.ts_utc),
            "input": local_input(m.ts_utc),
            "carbs_g": m.carbs_g,
            "description": m.description,
            "tags": m.tags,
            "note": m.note,
        }

    def known_meal_json(k: KnownMeal) -> dict:
        return {"id": k.id, "name": k.name, "carbs_g": k.carbs_g, "tags": k.tags}

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
        "name": "sugardaddy",
        "short_name": "sugardaddy",
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
    def create_meal(
        request: Request,
        description: str = Form(""),
        carbs_g: str = Form(""),
        tags: str = Form(""),
        ts: str = Form(""),
        note: str = Form(""),
    ):
        carbs = float(carbs_g) if carbs_g.strip() else None
        meal = Meal(
            ts_utc=parse_local(ts),
            carbs_g=carbs,
            description=description,
            tags=tags,
            note=note,
        )
        meal.id = db.add_meal(meal)
        if _wants_partial(request):
            return _recent_partial(request)
        return meal_json(meal)

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
        if "carbs_g" in body:
            c = body["carbs_g"]
            fields["carbs_g"] = float(c) if c not in ("", None) else None
        for k in ("description", "tags", "note"):
            if k in body:
                fields[k] = body[k]
        ok = db.update_meal(meal_id, **fields)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.delete("/api/meal/{meal_id}")
    def delete_meal(meal_id: int):
        ok = db.delete_meal(meal_id)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    # --- known meals (input shortcuts) ----------------------------------

    @app.get("/api/known-meals")
    def list_known_meals():
        return [known_meal_json(k) for k in db.list_known_meals()]

    @app.get("/api/meal-suggestions")
    def meal_suggestions():
        """Autocomplete source for the meal name field: saved shortcuts first,
        then recently logged meal names not already covered by a shortcut. Each
        item carries carbs/tags for prefill; known_id is set only for shortcuts."""
        out: list[dict] = []
        seen: set[str] = set()
        for k in db.list_known_meals():
            key = k.name.strip().lower()
            seen.add(key)
            out.append({"name": k.name, "carbs_g": k.carbs_g, "tags": k.tags, "known_id": k.id})
        for m in db.recent_meal_names():
            key = m["name"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": m["name"], "carbs_g": m["carbs_g"], "tags": m["tags"], "known_id": None})
        return out

    @app.post("/api/known-meals")
    def create_known_meal(
        name: str = Form(...),
        carbs_g: str = Form(""),
        tags: str = Form(""),
    ):
        name = name.strip()
        if not name:
            return JSONResponse({"error": "name required"}, status_code=400)
        carbs = float(carbs_g) if carbs_g.strip() else None
        km = KnownMeal(name=name, carbs_g=carbs, tags=tags.strip())
        km.id = db.add_known_meal(km)
        return known_meal_json(km)

    @app.patch("/api/known-meals/{known_id}")
    async def update_known_meal(known_id: int, request: Request):
        body = await request.json()
        fields = {}
        if "name" in body and body["name"].strip():
            fields["name"] = body["name"].strip()
        if "carbs_g" in body:
            c = body["carbs_g"]
            fields["carbs_g"] = float(c) if c not in ("", None) else None
        if "tags" in body:
            fields["tags"] = body["tags"].strip()
        ok = db.update_known_meal(known_id, **fields)
        return JSONResponse({"ok": ok}, status_code=200 if ok else 404)

    @app.delete("/api/known-meals/{known_id}")
    def delete_known_meal(known_id: int):
        ok = db.delete_known_meal(known_id)
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
