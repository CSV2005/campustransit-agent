# Integrations, credentials and provider selection

Everything optional in this project is listed here with the exact console URL, the exact
variable name, what it costs, and what happens when it is absent. Nothing on this page is
automated: **you create the key in the provider's own console**. This app never scrapes a
login, never bypasses MFA, and cannot obtain a credential for you.

Read this before enabling anything. One section — [What is billed](#what-is-billed) — is
the one to read twice.

---

## 1. The registry

Every integration is described once, in `src/services/integrations.py`, and rendered from
that record in Settings and at `GET /api/v1/integrations`. A provider has one of eight
states:

| State | Meaning | How it is reached |
|---|---|---|
| `not_configured` | a required variable is missing | derived from the environment |
| `configured_untested` | credentials present, no successful check in 24 h | derived |
| `ready` | last check passed inside 24 h, or the integration is verified locally on every page load | a **check**, never an env var |
| `disabled` | switched off in Settings | `integration_prefs` |
| `auth_failure` | the provider rejected the credential (401/403, `REQUEST_DENIED`, `PERMISSION_DENIED`) | last check |
| `quota_or_billing` | quota exhausted, billing disabled, or rate-limited (429, `OVER_QUERY_LIMIT`, `RESOURCE_EXHAUSTED`) | last check |
| `provider_unavailable` | transport error, timeout or 5xx | last check |
| `unsupported` | the provider cannot be used here at all | catalogue |

`ready` is deliberately hard to reach:

* a check result older than **24 hours** downgrades to `configured_untested` (a revoked key
  still looks fine in a week-old success);
* local integrations (Ollama, GTFS tables, tile proxy, policy index) are re-checked on every
  registry read, because that is cheap;
* browser keys can never be verified server-side, so the Google Maps JavaScript row stays
  `configured_untested` and says so.

Check results live in the SQLite table `integration_checks` (one row per integration).

---

## 2. Credential table

| Integration | Purpose | Variable(s) | Where to create it | Cost | Used by |
|---|---|---|---|---|---|
| Google Maps JavaScript API | optional basemap | `GOOGLE_MAPS_BROWSER_KEY`, `GOOGLE_MAPS_MAP_ID` | https://console.cloud.google.com/google/maps-apis/credentials | Dynamic Maps SKU, per 1 000 loads | `web/static/js/map.js` |
| Google Places API | destination autocomplete | `GOOGLE_MAPS_SERVER_API_KEY` | https://console.cloud.google.com/google/maps-apis/api-list | Autocomplete SKU, per 1 000 requests | `src/services/places.py` |
| Google Geocoding API | forward + reverse geocode | `GOOGLE_MAPS_SERVER_API_KEY` | same | per 1 000 requests | `src/services/places.py` |
| Google Routes API | road routes, alternatives, geometry | `GOOGLE_MAPS_SERVER_API_KEY` | same | per 1 000 requests, SKU depends on routing preference | `src/services/road_routing.py` |
| Google Routes (traffic) | live vs free-flow duration | `GOOGLE_MAPS_SERVER_API_KEY` | same | traffic-aware SKU | `src/services/traffic.py` |
| OpenStreetMap tiles | Leaflet basemap | *(none)* `OSM_TILE_URL`, `OSM_USER_AGENT` optional | https://operations.osmfoundation.org/policies/tiles/ | free, usage policy applies | `web/api/routes_tiles.py` |
| OpenRouteService | optional road routing | `OPENROUTESERVICE_API_KEY` | https://account.heigit.org/signup | free tier, rate-limited, **no live traffic** | `src/services/road_routing.py` |
| TomTom Routing | optional road routing **with** traffic | `TOMTOM_API_KEY` | https://developer.tomtom.com/user/register | metered monthly allowance | `src/services/road_routing.py`, `traffic.py` |
| BMTC + BMRCL GTFS | schedules, stops, metro lines | *(commit the extract; no key)* | https://github.com/Vonter/bmtc-gtfs | free | `src/db/`, `src/routing/` |
| GTFS-Realtime (vehicle positions) | live bus positions on the map | `GTFS_RT_VEHICLE_URL`, `GTFS_RT_AUTH_HEADER`, `GTFS_RT_POLL_INTERVAL_S`, `GTFS_RT_STALE_AFTER_S` | IUDX consumer registration — https://iudx.org.in (see `docs/GTFS_REALTIME.md` §3) | free, but rate-limited by the data owner | `src/services/gtfs_rt.py`, `web/api/routes_live.py` |
| GTFS-Realtime (trip updates) | arrival predictions | `GTFS_RT_TRIPUPDATES_URL` | same | free | decoded and served; not yet shown in the UI |
| BMTC unofficial WebAPI | — | *(deliberately unused)* | https://github.com/PratyushBalaji/BMTC-API | — | listed as `unsupported` by decision (`docs/GTFS_REALTIME.md` §4) |
| Ollama | local LLM | `OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT_S`, `CHAT_BUDGET_S` | https://ollama.com/download | free, local | `src/services/ollama_client.py` |
| Chroma | policy retrieval | *(built locally)* | https://docs.trychroma.com/ | free | `src/rag/` |
| Nominatim | — | *(deliberately unused)* | https://operations.osmfoundation.org/policies/nominatim/ | — | listed as `unsupported` by policy |

The full variable list, with defaults, is `.env.example`. Nothing in that file is required.

---

## 3. What is billed

| Probe / call | Billed? | Charged when |
|---|---|---|
| `POST /api/v1/integrations/google_places/test` | **yes**, 1 Autocomplete request | only after `{"confirm_billable": true}` |
| `google_geocoding/test` | **yes**, 1 Geocoding request | same |
| `google_routes/test` | **yes**, 1 computeRoutes (traffic-unaware SKU) | same |
| `google_traffic/test` | **yes**, 1 computeRoutes (traffic-aware SKU) | same |
| `tomtom/test` | **yes**, 1 calculateRoute against the allowance | same |
| `openrouteservice/test` | quota, not money | same |
| `ollama/test`, tile stats, GTFS counts, policy index | no | never |
| **Loading the Settings page** | **no** | never — the page only reads local state |

Without `confirm_billable: true` the test endpoint answers **409** and runs nothing. That is
tested (`tests/test_integrations.py::test_billable_test_refuses_without_confirmation`).

Google's own quotas and prices change; verify them at
https://developers.google.com/maps/documentation/routes/usage-and-billing and
https://cloud.google.com/maps-platform/pricing before enabling a meter. **Enabling billing
is a decision for the account holder.**

---

## 4. Creating, rotating and revoking a key

### Google (server key)

1. Pick or create a Cloud project: https://console.cloud.google.com/projectcreate
2. Enable **only** the APIs you need: Places API, Geocoding API, Routes API —
   https://console.cloud.google.com/google/maps-apis/api-list
3. Enable billing on the project. Without it every request returns `REQUEST_DENIED`
   (which this app will show as **Authentication failed**).
4. Create an API key: https://console.cloud.google.com/google/maps-apis/credentials
5. **Restrict it.** Server key: *API restrictions* → the three APIs above, and (optionally)
   IP restriction to your host. Do **not** add an HTTP-referrer restriction to a server key.
6. Put it in `.env`:
   ```powershell
   Add-Content -Path .env -Value "GOOGLE_MAPS_SERVER_API_KEY=AIza..."
   ```
7. Restart the server, then Settings → Integrations → **Test (billable)**.

### Google (browser key, only for the Google basemap)

Same console, a **second** key. Under *Application restrictions* choose **HTTP referrers**
and list your origins (`http://localhost:7860/*`, plus your deployed origin). Under *API
restrictions* choose **Maps JavaScript API** only. This key is public by design — the
referrer restriction is what protects it.

### OpenRouteService

https://account.heigit.org/signup → *Tokens* → create. Put it in `.env` as
`OPENROUTESERVICE_API_KEY`. Note the free tier limits at
https://openrouteservice.org/limitations/ and that ORS has **no live traffic**.

### TomTom

https://developer.tomtom.com/user/register → *Dashboard* → *Keys* → create. Put it in
`.env` as `TOMTOM_API_KEY`. Every call is metered against the monthly allowance.

### Rotating

Create the new key first, replace the line in `.env`, restart, press **Test**, and only then
delete the old key. That way the app never has a window with no working credential.

### Revoking

Delete the key in the console. The next test moves the row to **Authentication failed**, and
the app falls back: local index for search, GTFS for transit, Leaflet/OSM for the map,
`unavailable` for traffic. Nothing crashes.

---

## 5. Provider selection and fallback

Road routing is a **chain**, resolved from configuration — never from an `if` in a view:

```dotenv
# default: try Google, then ORS, then TomTom
ROAD_ROUTING_CHAIN=google_routes,openrouteservice,tomtom

# pin one provider and disable fallback (useful when comparing providers)
ROAD_ROUTING_PROVIDER=openrouteservice
```

A provider is skipped when it is not configured, switched off in Settings, or its last check
was `auth_failure` / `quota_or_billing`. Skipping is recorded in the response:

```json
"attempts": [
  {"provider": "google_routes", "ok": false, "ms": 412, "error": "HTTP 500: ..."},
  {"provider": "openrouteservice", "ok": true, "ms": 233, "error": ""}
]
```

Every result names its provider and only claims what that provider supports:

| Provider | `traffic_aware` | Geometry | Duration basis |
|---|---|---|---|
| Google Routes | `true` only when `TRAFFIC_AWARE_OPTIMAL` was requested | decoded polyline | provider |
| OpenRouteService | always `false` | GeoJSON LineString | static-network model |
| TomTom | `true` when `traffic=true` | points array | live + free-flow |
| none | `false` | straight line, `geometry_kind: "straight_line"` | 28 km/h assumption, `is_estimate: true` |

With no provider configured, driving returns `available: false`. The app does **not**
substitute a straight-line estimate unless a caller explicitly asks for one
(`road_routing.plan(..., allow_estimate=True)`).

Traffic panels follow the same rule: Google first (it returns per-segment speeds), then
TomTom (delay only, `speed_intervals: []`), then
`"Live traffic data unavailable"` with the reason.

---

## 6. Timeouts

Two nested limits, both configurable:

| Setting | Default | Meaning |
|---|---|---|
| `OLLAMA_TIMEOUT_S` | 120 | one model call |
| `CHAT_BUDGET_S` | 150 | a whole chat turn, checked between tool rounds |
| browser | server budget + 60 s | taken from `/api/chat` (`timeout_hint_ms`) |

When the budget runs out the reply is still an answer: `engine: "rules"` with
`fallback_reason: "deadline: the turn used its 150s budget after 2 tool call(s)…"`.
Raising the timeouts does not make a blocked request succeed; it changes how long you wait
before being told what happened. The per-endpoint client limits live in
`web/static/js/api.js` (`TIMEOUTS`), and only chat is long.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `not_configured` after editing `.env` | server not restarted (config is read at import) | restart the process |
| `auth_failure` with `REQUEST_DENIED` | billing not enabled, API not enabled, or key restricted to the wrong API | check the two console pages above |
| `quota_or_billing` | daily quota hit or billing disabled | raise the quota, or wait for the reset |
| `provider_unavailable` | DNS/proxy/timeout/5xx | `GET /api/v1/data-sources` shows the last calls with latency |
| Map is grey | browser key has a referrer restriction that excludes this origin | add `http://localhost:7860/*` |
| `configured_untested` forever on Maps JS | expected — a referrer-restricted key cannot be validated server-side | load the map and watch the browser console |
| Chat says `deadline:` | the model is slower than `CHAT_BUDGET_S` | use a smaller model, raise the budget, or ask a narrower question |

`GET /api/v1/config/health` lists every variable with `ok` / `warn` / `error` and never
echoes a value.
