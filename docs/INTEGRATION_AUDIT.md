# Integration audit — CampusTransit Agent

**Date:** 2026-10-28 · **Scope:** audit only; no behaviour changed by this document.
**Method:** read the tree, the entry points, every module that touches an external
provider, the frontend call sites and the existing tests; ran the suites; reproduced the
reported chat timeout.

---

## 1. What the repository actually is

```
campustransit-agent/
├── run.py                     launcher (uvicorn web.server:app, prints LAN URL)
├── app.py                     legacy Streamlit UI (not the web app; still present)
├── web/
│   ├── server.py              FastAPI app + chat, map, tiles  (10 routes)
│   ├── index.html             single-page shell, inline CSS, no build step
│   ├── static/js/{api,app,views,map}.js    plain ES5-ish JS, window-scoped modules
│   └── api/
│       ├── routes_spec.py     the brief §10 contract (24 routes)
│       ├── routes_plan.py     /api/v1 planning surface
│       ├── routes_meta.py     /api/v1 capabilities, data-sources, coverage
│       ├── routes_state.py    saved routes / history / preferences
│       └── routes_tiles.py    OSM tile proxy (disk cache, TTL, 4 000-tile cap)
├── src/
│   ├── agent.py               campus agent facade (LLM loop + rules fallback)
│   ├── rules_agent.py         deterministic planner (~1 200 lines)
│   ├── routing/               graph, dijkstra/astar/rrt, fares
│   ├── tools/                 registry of 15 tools
│   └── services/
│       ├── providers.py       capability flags from env
│       ├── places.py          local index + Google Places/Geocoding merge
│       ├── trip_modes.py      walking + Google Routes driving
│       ├── traffic.py         Google Routes traffic-aware corridor reading
│       ├── http.py            retry + cache + api_call_log
│       └── ollama_client.py   typed Ollama HTTP client
├── data/processed/transit.db  SQLite, 37 tables (GTFS BMTC + BMRCL, OSM index, fares)
└── tests/                     pytest (104) + jsdom (16) + Playwright (37)
```

**Stack confirmed:** FastAPI 0.1xx, Uvicorn, httpx, SQLite, Leaflet 1.9.4 (vendored),
vanilla JS. **Streamlit is a leftover dependency** (`app.py`) — the served UI is not
Streamlit and never was in this phase.

---

## 2. Frontend → backend calls (verified call sites)

| Frontend call (`web/static/js/api.js`) | Route | File |
|---|---|---|
| `health()` | `GET /api/health` | server.py |
| `chat()` | `POST /api/chat` | server.py |
| `chatHealth()` | `GET /api/chat/health` | server.py |
| `planContract()` | `POST /api/routes/plan` | routes_spec.py |
| `planRich()` | `POST /api/v1/routes/plan` | routes_plan.py |
| `traffic()` | `GET /api/v1/traffic` | routes_spec.py |
| `autocomplete()` | `GET /api/v1/places/autocomplete` | routes_plan.py |
| `geocode()` / `reverseGeocode()` | `GET /api/locations/{geo,reverse-geocode}` | routes_spec.py |
| `lines()`, `ksrtc()`, `stations()`, `nearbyStops()` | `/api/transit/*` | routes_spec.py |
| `saved()`, `history()`, `prefs()` | `/api/v1/*` | routes_state.py |
| `mapConfig()`, `basemap()`, `tileStats()` | `/api/map/*` | server.py |
| `capabilities()`, `dataSources()` | `/api/v1/*` | routes_meta.py |

No frontend call targets a route that does not exist. **No CORS middleware is configured** —
correct today (same origin), a gap if the UI is ever served from another host.

---

## 3. Integration status, as found

| Integration | Code present | Live-tested | Verdict |
|---|---|---|---|
| **Google Maps JavaScript API** | `map.js` has a full `google.maps` backend; `providers.google_public()` ships the browser key + map id | **never** (no key) | implemented, unverified |
| **Google Places API** | `places.google_autocomplete()` + `place_details()`, merged into `places.search()` with a `providers_used` list | **never** | implemented, unverified |
| **Google Geocoding API** | `places.geocode()` → Google first, local index fallback, error attached as `google_error` | **never** | implemented, unverified |
| **Google Routes API** | `trip_modes.drive_trip()` + `traffic.road_traffic()` (TRAFFIC_AWARE_OPTIMAL, `TRAFFIC_ON_POLYLINE`) | **never** | implemented, unverified; unit-tested against stubs only |
| **OpenStreetMap raster tiles** | `routes_tiles.py`: identifying UA + Referer, 7-day disk TTL, 4 000-tile cap, no bulk prefetch | **yes** (thousands of tiles served) | working, policy-compliant |
| **Nominatim** | not used at runtime (OSM data is a baked extract) | n/a | correctly absent — avoids the usage-policy trap |
| **OpenRouteService** | **nothing** | — | missing |
| **TomTom** | **nothing** | — | missing |
| **BMTC/BMRCL GTFS** | ingested into SQLite (4 434 routes, 9 960 stops, 206 metro stops) | **yes** | working |
| **GTFS-Realtime** | no feed configured; reported `unavailable`, never simulated | n/a | correctly absent |
| **Ollama (local LLM)** | `ollama_client.py` + tool loop + 3 grounding gates | **yes** — `qwen2.5:0.5b` and `1.5b` pulled and exercised | working |
| **Policy retrieval (Chroma)** | 733 documents indexed | **yes** | working |

### Configuration auditing

- Keys are read **only** in `src/services/providers.py`; nothing else touches `os.getenv`
  for credentials. Good.
- `GOOGLE_MAPS_API_KEY` and `GOOGLE_MAPS_SERVER_KEY` are accepted as legacy aliases.
- The server key never reaches the browser: `google_public()` whitelists `browser_key`,
  `map_id`, `map_provider`, `maps_js`. There is **no test asserting that**, so a future
  edit could leak it silently.
- No redaction helper exists; provider errors are passed through to the UI verbatim.
  Google error messages can echo the request URL, which contains `key=`.

### Capability reporting, as found

`providers.capabilities()` returns a flat `{key, label, available: bool, reason, needs,
docs}` per integration. That cannot express the eight states the brief asks for, carries no
env-var name or setup URL, and has no test hook. The Settings screen renders exactly those
four fields — no setup links, no "test connection", no disable switch.

---

## 4. Why previous chat requests timed out — root cause

**`web/static/js/api.js` line 9: `const TIMEOUT_MS = 45000`, applied to every call.**
`API.chat()` passes no override.

Measured local inference on this machine (2 vCPU, no GPU):

| Model | Question | Server time |
|---|---|---|
| qwen2.5:0.5b | "Which metro line serves Majestic?" | 11–32 s |
| qwen2.5:1.5b | metro question, 1 tool round | 64 s |
| qwen2.5:1.5b | pass question, 1 tool round | 76 s |
| qwen2.5:3b | — | killed by the OOM reaper on this box |

So on anything slower than a warm 0.5b, **the browser aborts at 45 s while the server is
still generating**. The user sees `Could not reach the server: The request timed out.`

Three aggravating factors, all confirmed in code:

1. **No server-side wall-clock budget.** `MAX_TOOL_ROUNDS = 6` × `OLLAMA_TIMEOUT_S = 120`
   = a theoretical 720 s turn. The client cannot know that; the server cannot stop early.
2. **The abort is client-side only.** Nothing cancels the generation, so a retry stacks a
   second generation on the same single-threaded Ollama queue — making the next attempt
   *slower*, which is why it felt like an infinite loop.
3. **No request-duration logging.** Nothing in the server log says which request took 90 s.

Everything else ruled out: Ollama connectivity was fine (probe passes in ~1 s); the route
planner is 0.5–3 s; there is no blocking geocode inside the chat path; response
serialization is clean; the frontend already calls the right port (same-origin relative
URLs).

**Not the cause, checked and excluded:** uvicorn keep-alive, Pydantic validation, the agent
singleton, and `httpx` timeouts (all default to 120 s in `ollama_client`).

---

## 5. Gaps to close (the work plan)

| # | Gap | Fix |
|---|---|---|
| 1 | 45 s frontend cap vs 30–90 s turns | per-endpoint timeouts; chat timeout derived from the server's own budget; elapsed-time indicator; server answers *before* the client gives up |
| 2 | No server-side turn budget | wall-clock deadline per chat turn, checked between tool rounds, graceful rules fallback with a stated reason |
| 3 | No structured timing logs | middleware logging method, path, status, duration, and provider outcome — never a key |
| 4 | Capabilities are a boolean per provider | full integration registry: id, provider, purpose, setup URL, env vars, credential requirement, optional flag, 8 states, restrictions |
| 5 | No setup links, test buttons or enable/disable in Settings | render from the registry; test endpoint with safe vs. explicitly-billable checks |
| 6 | ORS and TomTom absent | real adapters + an explicit, configurable provider chain for road routing |
| 7 | No secret-redaction guarantee | central redactor + tests that assert no key value appears in any API response |
| 8 | Google paths untested | mocked contract tests for Places/Geocoding/Routes including REQUEST_DENIED, OVER_QUERY_LIMIT and malformed bodies |

Items 1–3 are the timeout bug. Items 4–8 are the integration work the brief asks for.

---

## Appendix — status after the work (2026-10-28)

The gaps in §5 are closed. Resolution, with the evidence:

| # | Gap | Outcome |
|---|---|---|
| 1 | 45 s frontend cap vs 30–90 s turns | per-endpoint timeouts; chat cap comes from the server (`timeout_hint_ms`); measured turns 29 s / 36.5 s / 41.6 s / 53.6 s |
| 2 | No server-side turn budget | `CHAT_BUDGET_S` (150 s) checked between tool rounds; expiry → rules answer + `deadline:` reason |
| 3 | No timing logs | one line per `/api` request with status and ms |
| 4 | Boolean capabilities | `src/services/integrations.py`: 13 integrations, 8 states, setup URLs, env names, costs, restrictions |
| 5 | No setup links / tests / toggles | Settings renders the registry; `POST …/test` (409 unless billable confirmed), `PUT …/enabled` |
| 6 | ORS and TomTom absent | real adapters + configured chain with `attempts`/`skipped` and per-provider quality flags |
| 7 | No redaction guarantee | `src/services/redact.py`, applied to registry, errors and logs; asserted in tests |
| 8 | Google paths untested | mocked contract tests for Places, Geocoding (forward + reverse, ambiguity), Routes, plus transport/non-JSON/quota/auth cases |

See `docs/FINAL_STATUS.md` for the six-category scoreboard (implemented / mock-tested /
live-tested / awaiting credentials / blocked / not implemented).

---

## Addendum: live vehicle positions (28 Oct 2026)

| # | Gap found by the audit | Resolution |
|---|---|---|
| 9 | The catalogue listed GTFS-Realtime as `unsupported`, which read as "cannot be done" when the truth was "no feed is published" | split in two: `gtfs_realtime` is now *implemented and unconfigured* (names `GTFS_RT_VEHICLE_URL`), and the reverse-engineered `bmtc_unofficial_api` is `unsupported` **by decision**, with the reasoning recorded in `docs/GTFS_REALTIME.md` §4 |
| 10 | Nothing in the product could ever show a vehicle, so "live" was an untested word | reader + endpoint + layer + 24 backend tests + 9 browser checks, all against locally served fixtures |
| 11 | A feed token would have ridden in a URL and leaked into logs | `GTFS_RT_AUTH_HEADER` added to `redact.SECRET_ENV`; `source` in responses is the hostname only |
| 12 | An integrator could poll a provider into the ground | >= 15 s server cache, one in-flight request, hidden-tab stop, 60 s failure backoff, 120 s no-feed re-check |

The audit's own rule held: nothing was upgraded to `ready` because a variable existed. With
no feed the state is `not_configured`, the map is empty, and the strip says why.
