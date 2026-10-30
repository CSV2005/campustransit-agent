"""FastAPI server for the CampusTransit live demo.

Serves a single-page app (no external assets, so it renders inside a sandboxed
iframe) and a JSON API the app calls.

Run:  uvicorn web.server:app --host 0.0.0.0 --port 7860
"""
from __future__ import annotations

# .env is read before anything imports src.config, because that module resolves OLLAMA_*,
# GOOGLE_* and the paths at import time. python-dotenv is optional: without it the
# process simply uses the real environment, which is what a container or a shell export
# gives you anyway.
try:                                    # pragma: no cover - trivial import shim
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:                     # pragma: no cover
    pass

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from src.agent import CampusTransitAgent
from src.services import ollama_client
from src.config import CHAT_BUDGET_S, DB_PATH, LLM_MODEL
from src.services import store
from src.services import config_check
from src.services.providers import providers
from src.services.redact import redact
from src.tools.plan_tool import ranked_itineraries
from src.tools.registry import tool_catalogue
from web.api.routes_integrations import router as integrations_router
from web.api.routes_meta import router as meta_router
from web.api.routes_spec import router as spec_router
from web.api.routes_plan import router as plan_router
from web.api.routes_state import router as state_router
from web.api.routes_live import router as live_router
from web.api.routes_tiles import router as tiles_router
from web.api.routes_tiles import tile_stats

ROOT = Path(__file__).resolve().parent.parent
INDEX = Path(__file__).resolve().parent / "index.html"

log = logging.getLogger("campustransit")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Say out loud, once, what this process is configured to do -- and what it is not.

    Problems are logged, never raised: a missing Google key is a supported configuration
    here, and refusing to boot because an *optional* provider is absent would be worse than
    starting and saying "Places is off, the local index is used instead".
    """
    result = config_check.log_summary(log)
    if result["warnings"]:
        log.warning("config: %d warning(s); see GET /api/v1/config/health",
                    result["warnings"])
    yield


app = FastAPI(title="CampusTransit Agent", version="2.0", lifespan=lifespan,
              description="Bengaluru bus + metro journey planner with saved routes, "
                          "travel history and a provider-honest traffic panel.")
for _r in (plan_router, state_router, meta_router, spec_router, tiles_router,
           integrations_router, live_router):
    app.include_router(_r)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")
_agent: CampusTransitAgent | None = None
_basemap: dict | None = None


@app.middleware("http")
async def timing_and_redaction(request: Request, call_next):
    """One log line per request: method, path, status, duration, provider outcome.

    The duration is the number that was missing when chat "timed out": with it, a 90 s turn
    is visible in the log as a 90 s turn instead of an unexplained client abort.
    """
    started = time.time()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = int((time.time() - started) * 1000)
        log.exception("%s %s -> 500 in %d ms", request.method,
                      redact(request.url.path), elapsed)
        raise
    elapsed = int((time.time() - started) * 1000)
    if request.url.path.startswith("/api"):
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        log.log(level, "%s %s -> %d in %d ms", request.method,
                redact(request.url.path), response.status_code, elapsed)
        response.headers["X-Response-Time-Ms"] = str(elapsed)
    return response


class ChatProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_student: StrictBool = True
    travel_days: int = Field(default=24, strict=True, ge=0, le=31)
    needs_ac: StrictBool = False
    is_woman: StrictBool = False
    airport: StrictBool = False


class ChatIn(BaseModel):
    message: str
    profile: ChatProfile | None = None
    # Optional conversation turns, [{role, content}]. Trimmed server-side to the last six
    # so a long session cannot blow past a small local model's context window.
    history: list[dict] | None = None


def agent() -> CampusTransitAgent:
    global _agent
    if _agent is None:
        _agent = CampusTransitAgent()
    return _agent


EXAMPLE_QUESTIONS = [
    "Best route and pass from Jayanagar to DSU Harohalli?",
    "I spend Rs 60 a day on buses, 24 days a month. Is a monthly pass worth it?",
    "What documents do I need for a BMTC student bus pass?",
    "Can women travel free on AC buses under Shakti?",
    "How do I get from Majestic to Whitefield by metro?",
    "Is the student pass free this year?",
    "What is the KSRTC fare to Kanakapura?",
    "Route from BTM Layout to DSU",
]


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX.read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    a = agent()
    counts = {}
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        for table in ["stops", "routes", "trip_count", "route_stops", "metro_stops",
                      "places", "ksrtc_corridor"]:
            try:
                if table == "trip_count":
                    counts["trips"] = con.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
                else:
                    counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        con.close()
    meta = {}
    meta_path = ROOT / "data" / "processed" / "index_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
    return {
        "engine": a.engine,
        "llm_reason": a.llm_reason,
        "llm_model": LLM_MODEL,
        "counts": counts,
        "vector_index": meta,
        "tools": [t["name"] for t in tool_catalogue()],
    }


@app.get("/api/examples")
def examples() -> dict:
    return {"examples": EXAMPLE_QUESTIONS}


@app.get("/api/tools")
def tools() -> dict:
    return {"tools": tool_catalogue()}


# --------------------------------------------------------------------- map API
@app.get("/api/map/config")
def map_config() -> dict:
    """What the browser should draw, and what to fall back to.

    Three renderers, tried in this order:

      ``google`` — real Google basemap; only when a referrer-restricted browser key exists
      ``osm``    — Leaflet + OpenStreetMap raster tiles *proxied through this server*
                   (``/api/tiles/...``), because the preview iframe has no network of its
                   own and cannot reach tile.openstreetmap.org directly
      ``svg``    — the self-contained GTFS map, which always works because it is drawn
                   from data already in the page

    ``renderer`` is a preference, not a promise: the client probes the first tile and
    falls back down the list if it fails. ``MAP_PROVIDER`` pins the choice.
    """
    cfg = providers()
    key = cfg.browser_key
    pref = (store.get_prefs().get("map_provider") or cfg.map_provider or "auto").lower()
    order = {"auto": ["google", "osm", "svg"],
             "google": ["google", "osm", "svg"],
             "osm": ["osm", "svg"],
             "svg": ["svg"]}.get(pref, ["google", "osm", "svg"])
    chain = [r for r in order if r != "google" or key]
    if not chain:
        chain = ["svg"]
    primary = chain[0]
    return {
        "renderer": primary,
        "fallback_chain": chain[1:],
        "provider_preference": pref,
        "browser_key": key,          # referrer-restricted, public by design
        "tile_url": "/api/tiles/{z}/{x}/{y}.png",
        "tile_probe": "/api/tiles/11/1447/940.png",
        "max_zoom": 19,
        "default_center": {"lat": 12.9716, "lng": 77.5946},
        "default_zoom": 11,
        "attribution": ("Google Maps" if primary == "google" else
                        "\u00a9 OpenStreetMap contributors" if primary == "osm" else
                        "Transit map drawn from BMTC GTFS + BMRCL GTFS stop geometry"),
        "attribution_url": ("https://www.google.com/intl/en_in/help/terms_maps/"
                            if primary == "google"
                            else "https://www.openstreetmap.org/copyright"),
        "server_key_exposed": False,
        "note": ("The Google server key never reaches the browser. Fares never come "
                 "from Google in any configuration."),
    }


@app.get("/api/map/basemap")
def map_basemap() -> dict:
    """Static geometry for the map background: metro lines and a stop density field.

    Cached in memory — it is derived from static GTFS and never changes at runtime.
    """
    global _basemap
    if _basemap is not None:
        return _basemap
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    lines = []
    for r in con.execute("SELECT route_id, route_short_name FROM metro_routes"):
        pts = con.execute("""
            SELECT s.stop_name, CAST(s.stop_lat AS REAL) lat, CAST(s.stop_lon AS REAL) lon,
                   COALESCE(s.service_status,'operational') status
            FROM metro_route_stops m JOIN metro_stops s ON s.stop_id = m.station_id
            WHERE m.route_id = ? AND m.direction_id = '0'
            ORDER BY m.seq""", (r["route_id"],)).fetchall()
        if not pts:
            continue
        lines.append({
            "id": r["route_id"],
            "name": r["route_short_name"] or r["route_id"],
            "status": pts[0]["status"],
            "stations": [{"name": p["stop_name"], "lat": p["lat"], "lng": p["lon"],
                          "status": p["status"]} for p in pts],
        })
    stops = [{"lat": r["lat"], "lng": r["lon"]} for r in con.execute("""
        SELECT CAST(stop_lat AS REAL) lat, CAST(stop_lon AS REAL) lon
        FROM stops WHERE stop_lat IS NOT NULL AND stop_lon IS NOT NULL
        ORDER BY stop_id LIMIT 1200""")]
    con.close()
    _basemap = {"metro_lines": lines, "bus_stops": stops}
    return _basemap


class RouteIn(BaseModel):
    origin: str
    destination: str
    is_student: bool = True


@app.post("/api/route")
def route(body: RouteIn) -> JSONResponse:
    """Ranked itineraries with map geometry.

    Geometry is the sequence of GTFS stops the path actually traverses — not a straight
    line between endpoints, and not a Google polyline. Fares come from the published
    tables via `price_path`.
    """
    if not body.origin.strip() or not body.destination.strip():
        return JSONResponse({"ok": False, "itineraries": [],
                             "warnings": ["Both origin and destination are required."]},
                            status_code=400)
    try:
        out = ranked_itineraries(body.origin.strip(), body.destination.strip())
    except Exception as exc:                      # never 500 a map request
        return JSONResponse({"ok": False, "itineraries": [],
                             "warnings": [f"Routing error: {type(exc).__name__}: {exc}"]},
                            status_code=200)
    return JSONResponse(out)


@app.get("/api/map/tiles/stats")
def tiles_stats() -> dict:
    return tile_stats()


@app.post("/api/chat")
async def chat(body: ChatIn) -> JSONResponse:
    """Conversational answers, grounded in the same tools the UI uses.

    The model is tried first when one is available; the deterministic rules planner always
    answers if it is not. A failure never becomes a 5xx — the reply carries `engine`,
    `provider` and, when the model was skipped or its answer was withheld, a
    `fallback_reason` that says why.

    Two timeouts, deliberately nested:

    * the model gets ``CHAT_BUDGET_S`` (default 150 s) of wall clock for the whole turn,
      checked between tool rounds, and the rules engine answers if it runs out;
    * the browser waits ``budget + 60 s`` (``timeout_hint_ms``), so the server always has
      the chance to answer before the client gives up. Before this, the client aborted at
      45 s while the server kept generating, and every retry queued behind the last one.

    The call runs in a worker thread (``asyncio.to_thread``) because ``ollama_client`` is
    synchronous httpx and a 90 s generation must not block other requests.
    """
    if not body.message.strip():
        return JSONResponse({"error": "empty message"}, status_code=400)
    history = [h for h in (body.history or [])
               if isinstance(h, dict) and h.get("role") == "user"][-6:]
    started = time.time()
    profile = body.profile.model_dump(exclude_unset=True) if body.profile else {}
    try:
        out = await asyncio.to_thread(agent().chat, body.message.strip(),
                                      profile, history)
    except Exception as exc:                       # never 500 a conversation
        log.exception("chat failed")
        out = {"engine": "rules", "provider": "rules", "answer":
               "The assistant could not answer that request. The planner and the data are "
               "unaffected — try a route or fare question.",
               "fallback_reason": redact(f"{type(exc).__name__}: {exc}"), "trace": []}
    out.setdefault("provider", "rules")
    out["server_ms"] = int((time.time() - started) * 1000)
    out["budget_s"] = out.get("budget_s") or CHAT_BUDGET_S
    out["timeout_hint_ms"] = int((out["budget_s"] + 60) * 1000)
    out["llm"] = ollama_client.status(timeout=0.8)
    if out.get("fallback_reason"):
        out["fallback_reason"] = redact(out["fallback_reason"])
    return JSONResponse(out)


@app.get("/api/chat/health")
def chat_health() -> dict:
    """Is a local model actually usable right now, and if not, what should be run?

    Cheap (one /api/tags call) and honest: it distinguishes "Ollama is not running" from
    "Ollama is running but the model is not pulled", because those need different fixes.
    """
    st = ollama_client.status()
    st["ok"] = bool(st.get("available") and st.get("model_pulled"))
    st["fallback"] = "rules" if not st["ok"] else None
    # What the browser should wait for, derived from the server's own budget rather than
    # guessed in JavaScript.
    st["budget_s"] = CHAT_BUDGET_S
    st["timeout_hint_ms"] = int((CHAT_BUDGET_S + 60) * 1000)
    return st
