# CampusTransit — architecture summary & implementation plan

Written after inspecting the existing repository, before any code was added.

## What exists today (verified, not assumed)

| Layer | Reality |
|---|---|
| Frontend | `web/index.html` — one 602-line file, inline CSS/JS, **no framework**. Plus `web/static/js/map.js` (441 lines, persistent map object, SVG + Google backends) and `app.js`. |
| Backend | `web/server.py` — FastAPI, **8 endpoints**: `/`, `/api/health`, `/api/examples`, `/api/tools`, `/api/map/config`, `/api/map/basemap`, `/api/route`, `/api/chat`. |
| Database | SQLite `data/processed/transit.db`, 73 MB, **31 tables**. 9,960 stops · 4,434 routes · 1,540,686 trip_stops · 205,354 route segments · 504 metro stations · 30 official fare slabs. |
| Datasets | D1 BMTC GTFS, D2 BMRCL GTFS + Pink Line, D3 OSM extracts, D4 KSRTC corridor CSV. All imported and verified. |
| Fares | BMRCL official chart (14.02.2025), verified 6,806/6,806 against the GTFS matrix. BMTC GTFS fare stages + published distance slabs. |
| AI | `src/agent.py` (Ollama → deterministic fallback), `src/rules_agent.py` (1,136-line deterministic planner), Chroma 733 docs. |
| Routing | `src/routing/` — Dijkstra, A*, RRT over a corridor graph; `src/tools/plan_tool.py` exposes them. |
| Tests | `tests/test_map_api.py` (9), `tests/test_map_dom.mjs` (16). Eval: 41 cases. |
| Env | `.env.example` — LLM, embeddings, paths, `GOOGLE_MAPS_BROWSER_KEY`. |

## Decisions

**Keep the single-HTML frontend.** Spec §2 says preserve unless migration is a clear win. A
React/Vite migration would add a node_modules build step that does not survive this
environment, and would discard a map object whose persistence contract is already covered by
16 passing DOM tests. The file is extended in place into the dashboard layout.

**Add a three-provider map layer** rather than hard-coding one provider (spec §4F):

| Provider | Needs | Real geography | Chosen when |
|---|---|---|---|
| `osm` | nothing (vendored Leaflet + public tiles) | yes | tiles load |
| `google` | `GOOGLE_MAPS_BROWSER_KEY` | yes | key present |
| `svg` | nothing | yes (GTFS stop geometry) | no network |

The browser probes one tile at load. This matters here: the in-app preview iframe has **no
network**, so a tile map would render blank there while working fine in a normal browser.
The SVG renderer is the honest fallback, not a decoration.

**New code goes in new modules.** `src/services/` for providers and domain logic,
`web/api/` for routers mounted onto the existing app. Nothing existing is rewritten.

**Where a credential is missing, the API says so.** Google Places/Routes/Geocoding return
`status: "unavailable"` with a reason — never a fabricated route, fare or traffic delay.
Traffic without a Google key is reported as unavailable, and the departure recommendation
falls back to GTFS schedule times with the basis stated.

## Plan

1. Schema: `saved_routes`, `travel_history`, `data_source_status`, `api_call_log`.
2. `src/services/` — provider config, geocoding, places, Google Maps/Routes, traffic, fare
   breakdown, route planning (spec §11 response shape).
3. `web/api/` — config, locations, routes, traffic, transit, saved routes, history.
4. Frontend — sidebar, header, chat, route tabs/cards, traffic panel, map filters.
5. Tests with mocked HTTP; no test touches a paid API.
6. README + `.env.example`.

Starting implementation.

---

# As built (2026-10-27)

The plan above was followed, with one change forced by measurement: the tile probe.
The preview iframe has no network, but the **backend** does, so tiles are now proxied
through `/api/tiles/{z}/{x}/{y}.png` and cached on disk. The browser talks only to our own
origin, so OpenStreetMap is usable where it previously was not. `svg` remains the last
rung of the fallback chain.

## What exists now

```
src/db/extra_schema.py     6 additive tables (37 total in transit.db)
src/services/
  providers.py             capability detection, one cached answer per process
  http.py                  httpx + retry + TTL cache + api_call_log
  places.py                inverted-index autocomplete (0.03-1.2 ms) + Google merge
  planner.py               plan(), 5 objectives, fare breakdown, departure advice
  traffic.py               Google Routes live + GTFS scheduled supply
  store.py                 saved_routes / history / preferences / sources
  source_audit.py          seed() offline, probe() hits each URL for real
web/api/
  schemas.py               Pydantic request/response models, extra="forbid"
  routes_plan.py           /api/v1/routes/*, /places/*, /traffic
  routes_state.py          /api/v1/saved-routes, /history, /preferences
  routes_meta.py           /api/v1/capabilities, /data-sources, /coverage
  routes_tiles.py          /api/tiles/{z}/{x}/{y}.png
web/static/js/
  api.js                   fetch wrapper -> {ok, data, error, status}
  views.js                 render functions + markdown + autocomplete binding
  app.js                   controller, navigation, all user actions
  map.js                   one persistent map; google | osm | svg backends
web/static/vendor/leaflet.js   vendored, no CDN
web/static/css/app.css         one stylesheet, no external fonts
```

## Deviations from the plan, and why

| Planned | Built | Reason |
|---|---|---|
| one tile provider chosen at config time | preference chain verified at runtime by probing a real tile | the server cannot know whether the *browser* can load a tile |
| traffic panel with congestion by hour | live traffic (absent) + scheduled supply (real) | the GTFS import has no departure times, so an hourly curve cannot be derived |
| `least_walking` via existing profiles | added `walk` weight to `PROFILES` and to the walk-edge cost | the original three profiles had no walking term at all |
| React/Vite frontend | single HTML page + 4 ES5-ish modules | a `node_modules` build step does not survive this environment, and the brief permits either |

## Verification

`pytest tests/` 43 passed · `node tests/test_map_dom.mjs` 16/16 ·
`node tests/browser_smoke.mjs` 22/22 in real Chromium · `run_eval` 41/41.
