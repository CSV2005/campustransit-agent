# Final status — API discovery, key configuration, integration and testing

Companion to `docs/INTEGRATION_AUDIT.md` (what was found) and `docs/INTEGRATIONS.md` (how
to configure each provider). This page is the honest scoreboard: what runs, what was only
tested against mocks, what was tested against a live service, what is waiting for a
credential, what is blocked, and what was not built.

Six categories, no overlap. **"Mock-tested" never means "works with the real provider."**

---

## 1. Implemented and live-tested

Executed in this session against the real service, on this machine.

| Item | Evidence |
|---|---|
| Integration registry (14 integrations, 8 states) | `GET /api/v1/integrations` → 14 records; `pytest` 45 tests |
| Settings → Integrations UI | Chromium run: 13 cards, 13 setup links, 10 test buttons, 13 toggles |
| Billable-test guard | browser: clicking *Test (billable)* raised the confirm dialog; HTTP: **409** without `confirm_billable` |
| Enable/disable without editing `.env` | `PUT …/enabled` → `Not configured → Disabled → Not configured` |
| Startup config validation | log line at boot `config: 11 checks, 0 error(s), 0 warning(s)`; `GET /api/v1/config/health` |
| Request duration logging | `POST /api/chat -> 200 in 53570 ms` in the server log |
| Chat deadline + rules fallback | 53.6 s turn → `engine: rules`, `fallback_reason: "withheld: model quoted ₹1140…"` (the price gate, not the deadline, took that one) |
| Browser timeout fix | in-browser turn completed at **41.6 s** with the cap at 210 s; old cap was 45 s |
| Ollama path (local, no key) | `qwen2.5:0.5b`; `chat/health` → `available: true, model_pulled: true, budget_s: 150` |
| OSM tiles through the proxy | tiles served and decoded in the browser run |
| GTFS static (BMTC + BMRCL) | registry live check: `9,960 bus stops, 4,434 bus routes, 99 metro stations on a line` |
| Policy index | `733 policy documents indexed` |
| Worked example (§7) | Madavara → DSU: 11-leg plan, ₹28/₹6/₹12/₹23 BMTC fares from published slabs, KSRTC leg *not published*, metro line ruled out with a named reason |
| Secret redaction | registry JSON and Settings markup contain no key-shaped string (tested) |

### Live vehicle positions (new this round)

| Item | Evidence |
|---|---|
| GTFS-Realtime decoder (protobuf + JSON) | `tests/test_gtfs_rt.py` 24 tests; real `FeedMessage` bytes built with `gtfs-realtime-bindings` and parsed back |
| Staleness driven by the provider's timestamp | fixture with one 240 s-old vehicle → `live_count 2`, `stale_count 1`, marker drawn dashed |
| `GET /api/transit/vehicle-positions` | no feed → **200** `available:false, kind:not_configured`; broken feed → **502**; malformed bbox → **422** |
| One upstream call per interval, shared in flight | 5 sequential requests → 1 upstream call; 4 concurrent threads → 1 upstream call |
| "Live buses" map layer | Chromium, feed run: **9/9**, 3 markers, teal *Live* chip, "2 of 3 buses reporting within 120s, 1 delayed"; no-feed run: **9/9**, 0 markers, *Unavailable* |
| Token never exposed | `GTFS_RT_AUTH_HEADER` sent upstream, absent from the response body and from `/realtime-status`; source label is the hostname only |
| Registry honesty | `gtfs_realtime` sits at `not_configured` naming `GTFS_RT_VEHICLE_URL`; `bmtc_unofficial_api` sits at `unsupported` and is never called |

**Not a live BMTC test.** The feed behind those 9/9 runs is a locally served fixture
(`python -m http.server` over a generated `.pb`). It proves the decode, cache, staleness and
UI path; it does not prove that any Bengaluru bus was tracked, and the strip names
`127.0.0.1` as the source precisely so nobody has to guess.

## 2. Implemented and mock-tested only

Code path complete; exercised against stubbed provider responses, never against the real
provider — because no credential exists here (and, for Google, because testing it costs
money).

| Item | Covered by |
|---|---|
| Google Places probe → `ready` / `auth_failure` / `quota_or_billing` / `provider_unavailable` | `test_google_places_probe_maps_provider_status`, `…handles_a_non_json_body`, `…transport_error…` |
| Google Geocoding forward, with ambiguity list | `test_forward_geocode_surfaces_ambiguity` |
| Google Geocoding reverse | `test_reverse_geocode_uses_google_when_configured` |
| Google Routes probe (`computeRoutes`) | `test_routes_probe_reports_ready_with_a_route` |
| Traffic-aware routing SKU probe | probe shares `_routes_probe`, unit-covered for the unaware path |
| OpenRouteService probe: free health short-circuit, quota-consuming directions | `test_ors_health_down_short_circuits_before_using_quota` |
| TomTom probe + auth classification | `test_tomtom_auth_failure_is_classified` |
| Road-routing chain: order, fall-through, skip-on-auth-failure, pinning | 4 chain tests |
| Provider labelling on driving itineraries | `test_drive_trip_names_its_provider` |
| Encoded polyline decoding | `test_polyline_decoder_round_trip` (Google's documented sample) |
| Chat deadline → rules answer with `deadline:` reason | `test_deadline_produces_a_rules_answer_and_a_reason` |
| Per-round timeout shrinks to the remaining budget | `test_each_model_round_gets_only_the_remaining_budget` |

## 3. Waiting for a credential (no code work left)

| Provider | Variable | What happens the moment it is set | Cost |
|---|---|---|---|
| Google Places | `GOOGLE_MAPS_SERVER_API_KEY` | autocomplete merges Google over the local index | per 1 000 requests |
| Google Geocoding | same key | forward + reverse geocoding (ambiguity list included) | per 1 000 requests |
| Google Routes | same key | road routes, alternatives, real polylines | per 1 000 requests |
| Google traffic | same key | live vs free-flow duration in the traffic panel | traffic-aware SKU |
| Google Maps JS | `GOOGLE_MAPS_BROWSER_KEY` (referrer-restricted) | map renderer switches from Leaflet to Google | Dynamic Maps SKU |
| OpenRouteService | `OPENROUTESERVICE_API_KEY` | ORS joins the routing chain (never traffic-aware) | free tier |
| TomTom | `TOMTOM_API_KEY` | TomTom joins the chain **and** can answer the traffic panel | metered allowance |

Every one of these has a documented console URL in `docs/INTEGRATIONS.md` §2 and a
placeholder in `.env.example`. **None of them can be created by this app** — that is a
deliberate design decision, not an omission.

## 4. Blocked (not solvable by configuration)

| Item | Why | What the app does |
|---|---|---|
| An authorised BMTC GTFS-Realtime feed | none is published: the IUDX resource is secure (consumer registration), and ONDC sharing was "advanced discussions" as of 2026-10-12 | reader is built and tested; with no URL the layer says *Unavailable*; nothing is simulated |
| BMTC unofficial WebAPI as a shortcut | undocumented, per-route, unlicensed for this use | state `unsupported`; documented in `docs/GTFS_REALTIME.md` §4 with what would change the decision |
| Nominatim autocomplete | the public instance forbids per-keystroke querying (1 req/s, no autocomplete) | state `unsupported`, with the policy URL; OSM data is a baked extract instead |

## 5. Not implemented (stated, not hidden)

- **GTFS-Realtime trip updates in the UI.** The feed type is decoded and served at
  `/api/transit/trip-updates`, but no live ETA is shown on screen: mixing a delay with a
  scheduled time without proving the vehicle is on *your* trip would be worse than the
  scheduled time it replaced.
- **Google Places session tokens.** Each autocomplete call is billed individually; a session
  token would reduce the cost when a prediction is followed by Details. Not wired.
- **Automatic geocode disambiguation UI.** The API returns `candidates` and `ambiguous`;
  the plan form still takes the first candidate unless the traveller edits the text.
- **TomTom route geometry on the map.** The points array is parsed and returned; the map
  draws Google/ORS geometries through the same path, and TomTom has not been exercised
  against a live key.
- **Per-provider cost accounting.** `api_call_log` records every attempt with latency and
  status; it does not estimate spend.

## 6. Known limitations carried forward

- **`balanced` still ranks an unpriced itinerary above a priced one** for some corridors
  (e.g. Nelamangala). Documented, unchanged.
- **`qwen2.5:0.5b` is weak at tool selection.** It answered "no metro line called Majestic"
  for a metro-line question; the grounding gates caught it on the pass question instead.
  Use 1.5b/3b where RAM allows.
- **Fare verification is unchanged**: metro from the BMRCL chart (primary), BMTC passes
  from secondary sources, KSRTC per-trip fares partially published and `null` otherwise.
- **Live buses depend on a feed nobody has yet.** Every part except the URL is done; the
  map states that plainly rather than filling the gap.

---

## What this round changed, in one paragraph

An integration registry that cannot say "ready" from an environment variable; a Settings
panel that names the variable, links the official console, shows the last check and refuses
to run a billable probe without consent; a road-routing chain (Google → ORS → TomTom) that
is configured rather than hard-coded and labels every result with the provider that produced
it; secret redaction on the way out; startup validation that reports shapes, never values;
and a timeout fix with measurements — because the old 45 s client cap was aborting 36–54 s
turns that the server went on to finish.

## Reproduce the key results

```powershell
python -m pytest tests -q                 # 172 passed, 1 skipped
python -m pytest tests/test_gtfs_rt.py -q # 24 passed, no network
node tests/test_map_dom.mjs               # 16/16
node tests/test_integrations_dom.mjs      # 14/14
python run.py                             # then, in another window:
Invoke-RestMethod http://localhost:7860/api/v1/integrations | Select-Object -Expand summary
Invoke-RestMethod http://localhost:7860/api/transit/vehicle-positions   # available:false, with the reason
```

Full browser run (needs Chromium and a live server):

```powershell
node tests/browser_integrations.mjs       # 15/15, includes the slow-turn check
node tests/browser_smoke.mjs              # 37/37
node tests/browser_live.mjs http://localhost:7860   # 9/9, no feed -> Unavailable
node tests/browser_live.mjs http://localhost:7862   # 9/9, fixture feed -> markers + Live
```
