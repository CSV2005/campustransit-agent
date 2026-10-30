# Live vehicle positions (GTFS-Realtime)

Status of this feature on **28 October 2026**:

| Question | Answer |
|---|---|
| Is there an official, public BMTC GTFS-Realtime vehicle-positions feed? | **No.** BMTC streams AVL data from ~6,000 buses into IUDX, but the vehicle-position resource is *secure*: it needs consumer registration, not just a URL. |
| Does this app therefore show live buses? | **Only if you supply a feed you are authorised to read.** Out of the box the layer is empty and labelled `Unavailable`. |
| Is the reader implemented or just planned? | Implemented and tested: protobuf **and** GTFS-RT JSON, caching, staleness, error mapping, redaction, 24 backend tests and 9 browser checks over a real HTTP feed. |
| Are the tests proof that Bengaluru buses are tracked? | **No.** They prove the plumbing, using a locally served fixture feed. Nothing in the UI or the docs claims otherwise. |

Everything below is about that boundary: what the app will do the moment a credential
exists, and what it refuses to do until then.

---

## 1. What you see with no feed

Open the map and tick **Layers -> Live buses**. The strip under the layer switches reads:

> **Unavailable** - No authorised GTFS-Realtime vehicle feed is configured. BMTC does not
> publish an open, unauthenticated vehicle-positions feed; its realtime data is available
> through IUDX to approved consumers. Until a feed URL is supplied, the map shows no buses
> and this endpoint says so.

and `GET /api/transit/vehicle-positions` returns HTTP **200** with:

```json
{"available": false, "kind": "not_configured", "vehicles": [], "count": 0,
 "needs": "GTFS_RT_VEHICLE_URL (and GTFS_RT_AUTH_HEADER if your feed needs a token)",
 "reason": "No authorised GTFS-Realtime vehicle feed is configured. ..."}
```

That is deliberately a `200`, not a `502`: nothing failed. There is simply no source.
A `502` is reserved for *a feed was configured and could not be read*, so that a real
outage cannot hide behind the same word as an unfilled setting.

The scheduled routes, fares and metro lines are unaffected. They come from static GTFS and
are labelled as scheduled, which is what they are.

## 2. What a feed has to be

The reader accepts the two encodings the GTFS-Realtime spec allows:

* **protobuf** (`application/x-protobuf`) - the normal one, decoded with
  `gtfs-realtime-bindings`;
* **JSON** - the same `FeedMessage` shape, if your provider serves `?format=json`.

Per entity, it uses `vehicle.vehicle.{id,label}`, `vehicle.trip.{trip_id,route_id}` and
`vehicle.position.{latitude,longitude,bearing,speed}`, plus `vehicle.timestamp`. The spec
lets any of these be absent, so absence is handled rather than assumed:

| Missing | What the app does |
|---|---|
| `position` | entity is dropped (it cannot be placed) |
| `position.latitude/longitude` = `0,0` | dropped: a null-island fix is a broken GPS, not a bus in the Gulf of Guinea |
| `bearing` | no arrow drawn; the marker stays a circle |
| `speed` | `speed_kmh: null` |
| `timestamp` | `is_live: false` - a position without a time cannot be called current |
| `vehicle.label` | falls back to `vehicle.id`, then to "vehicle" in the tooltip |

Configure it with two variables (all five are documented in `.env.example`):

```powershell
GTFS_RT_VEHICLE_URL=https://your-feed.example.gov/gtfs-rt/vehicle-positions
GTFS_RT_AUTH_HEADER=Authorization: Bearer <token>   # optional; server-side only
GTFS_RT_TRIPUPDATES_URL=                            # optional, different feed type
GTFS_RT_POLL_INTERVAL_S=20                          # floor is 15
GTFS_RT_STALE_AFTER_S=120                           # floor is 30
```

`GTFS_RT_AUTH_HEADER` is one `Name: value` pair. It is parsed on the server, sent only
upstream, and redacted from every response and log line (it is in `redact.SECRET_ENV`
alongside the Google keys).

## 3. How to obtain an authorised feed

In order of how likely each is to work, with what was actually verified during this build:

### 3.1 IUDX (the real path for BMTC)

Bengaluru's transit data is published through the **India Urban Data Exchange**. BMTC's
realtime vehicle positions are there, from ~6,000 buses at roughly 10-second intervals,
as a **secure** resource - catalogue id `8a9c53c2-7291-454e-b173-2d9bfe893eb1` in the
instance browsed for this work.

1. Register as a consumer at <https://iudx.org.in> (organisation, use case, contact);
2. request access to the BMTC transport resource and accept its licence terms;
3. you receive a token or client credentials plus the resource URL;
4. put the URL in `GTFS_RT_VEHICLE_URL` and the credential in `GTFS_RT_AUTH_HEADER`,
   then restart the server (the reader is configured at import time).

Nothing in this repository automates steps 1-3, and it should not: those are agreements
between you and the data owner.

### 3.2 BMTC -> ONDC

As of 2026-10-12, press reporting has BMTC sharing realtime data onto ONDC in "advanced
stage"; Namma Yatri, BIAL and Tummoc were reported in talks on 2026-10-09, and no delivery
date was published. The metro (BMRCL) is already shared through ONDC participants - but
that is an *app-level* arrangement, not a documented public GTFS-RT URL a server can poll.
When a public feed URL is published, this app needs only section 2's two variables.

### 3.3 Mobility Database

`api.mobilitydatabase.org` indexes feeds worldwide, but it is **not anonymously queryable**:
an unauthenticated search during this build returned `Invalid GCIP ID token: empty token`.
Register, mint a token, and search for Bengaluru. A catalogue entry is a pointer, not a
licence - check the feed's own terms.

### 3.4 Static GTFS (already here)

Static GTFS gives routes, stops and schedules, and this app already ships it (9,960 stops,
4,434 routes). It contains **no** vehicle positions and cannot be made to. DULT's Transport
Data Hub (`tdh.dult-karnataka.com`) is where the zips come from; it does not serve realtime.

## 4. The unofficial BMTC API: examined, deliberately not wired

`https://bmtcmobileapi.karnataka.gov.in/WebAPI` is the backend of BMTC's own mobile app.
It answers unauthenticated JSON, and `SearchByRouteDetails_v4`
(POST `{routeid, servicetypeid:0}`, headers `lan: en`, `deviceType: WEB`) returns live
per-vehicle records under `up`/`down` -> `data[].vehicleDetails[]` with `vehicleid`,
`vehiclenumber`, `centerlat`, `centerlong`, `heading`, `eta` and `lastrefreshon`.

It is **listed in Settings as `Unsupported here`** and no code path calls it. Why:

* **The contract is not a contract.** No documentation, no versioning, no notice of
  change; the only public write-up is a third-party GitHub repository.
* **It is per-route, not a feed.** One request answers one route. A map that wants "buses
  near me" fans out into dozens of requests per refresh against a government server that
  never agreed to serve this app.
* **Permission is unverified.** The wrapper repo's own README says educational use; that is
  not authorisation for a deployed dashboard.
* **Six thousand buses are the point.** To see the city you would have to poll every route.
  That is exactly the load an AVL feed via IUDX exists to avoid.

An early draft of this feature included an opt-in `bmtc_unofficial` adapter. It was
removed rather than shipped disabled: an off-by-default scraper in a student project tends
to get switched on by the next person, and then the app is quietly reverse-engineering a
public body's endpoint.

**What would change this:** written permission (or a published contract) plus a rate limit
BMTC sets. The adapter would be ~80 lines: normalise `vehicleDetails[]` into the same
vehicle objects and let the existing cache, staleness and redaction do the rest.

## 5. Staleness: what "Live" is allowed to mean

The server computes liveness from the **provider's own timestamp** - never from when the
request was made, never from a schedule.

| Age of the vehicle's own `timestamp` | `is_live` | UI |
|---|---|---|
| <= `GTFS_RT_STALE_AFTER_S` (default 120 s) | true | teal marker, chip **Live** |
| > threshold | false | grey dashed marker, chip **Delayed data**, age shown |
| absent | false | treated as delayed, tooltip says "no timestamp" |

The strip text is generated from the payload, so it can read
`2 of 3 buses reporting within 120s, 1 delayed - feed.example.gov - updates every 20s`,
and a feed where nothing is fresh reads
`3 buses, none reporting within 120s - these are old positions, not current ones`.

The window is deliberate: a 60-second-old fix is normally fine for a bus that is stopped
in traffic, while a 10-minute-old one is a different thing entirely. Providers whose
vehicles report every 30 s should set `GTFS_RT_STALE_AFTER_S=90` or so; there is no
correct universal value, so it is a setting rather than a constant.

## 6. Polling rules the app enforces

| Rule | Why |
|---|---|
| Server cache >= 15 s (`GTFS_RT_POLL_INTERVAL_S`, floor enforced, not clamped silently - `poll_interval_s()` returns 15.0 for a value of 1) | the provider never sees a request storm from tab refreshes |
| Concurrent readers share one in-flight request (per-URL lock, re-check inside the lock) | ten tabs, one upstream call |
| Failed reads are cached for the same interval | a broken feed is not hammered while the UI shows the reason |
| Browser refreshes at the server's `refresh_after_s`, stops on tab hide, re-checks immediately on return | a background tab should not drain anything |
| No-feed response re-checks every 120 s; upstream failure backs off to 60 s | the loop does not spin on a condition it cannot fix |
| `GET /api/transit/vehicle-positions` sends `Cache-Control: no-store` | the payload is time-sensitive; the server cache is the only cache |

## 7. Endpoints

```
GET /api/transit/vehicle-positions?limit=800&route=500D&bbox=12.8,77.4,13.1,77.8
GET /api/transit/trip-updates?limit=200
GET /api/transit/realtime-status
```

`vehicle-positions` returns `available`, `kind`, `source` (hostname only - never the URL,
which may carry a token in its query string), `feed_format`, `feed_timestamp`,
`feed_age_s`, `fetched_at`, `cached`, `latency_ms`, `count`, `total_in_feed`,
`live_count`, `stale_count`, `median_age_s`, `stale_after_s`, `refresh_after_s` and
`vehicles[]`.

Status codes:

| Code | Meaning |
|---|---|
| 200 | either a decoded feed, or `available: false` with `kind: not_configured` |
| 422 | `bbox` malformed, out of range, or inverted |
| 502 | a feed is configured but unreadable: transport error, non-200, or a body that is not GTFS-RT. `kind` says which: `auth_failure` (401/403), `quota_or_billing` (429), `provider_unavailable` (5xx, transport, decode) |

`trip-updates` uses the same rules and returns `updates[]` with trip/route/vehicle ids,
`delay_s` and up to ten `stop_time_updates`. **It is not surfaced in the UI yet** - see
limitations.

`realtime-status` is the cheap call Settings uses: `{configured, ok, kind, reason,
vehicles, live, feed_timestamp, feed_age_s, latency_ms, cached}`.

## 8. Testing without a feed

Two paths, both offline from the app's point of view:

**Backend (pytest).** `tests/test_gtfs_rt.py` builds real `FeedMessage` objects with the
`gtfs-realtime-bindings` encoder and feeds the serialised bytes through the real decoder
with `httpx.Client` monkeypatched. It asserts decoding, missing-field handling, staleness,
route/bbox filters, one-call-per-interval, concurrency, 502 mapping and token redaction.

```powershell
cd "E:\campustransit--ai agent\campustransit-agent"
python -m pytest tests/test_gtfs_rt.py -q
```

**End to end over HTTP (the run that produced the screenshots).** A fixture feed is
generated with a *current* timestamp and served locally; a second app instance points at it.

```powershell
cd "E:\campustransit--ai agent\campustransit-agent"
python tests\make_fixture_feed.py --out C:\temp\feed\vp.pb
Start-Process -WindowStyle Hidden python -ArgumentList '-m','http.server','8099','--bind','127.0.0.1' `
  -WorkingDirectory C:\temp\feed
$env:GTFS_RT_VEHICLE_URL = "http://127.0.0.1:8099/vp.pb"
$env:LLM_ENABLED = "off"
python -m uvicorn web.server:app --host 0.0.0.0 --port 7862
# separate window:
node tests\browser_live.mjs http://localhost:7862   # 9/9, markers + Live chip
node tests\browser_live.mjs http://localhost:7860   # 9/9, 0 markers + Unavailable chip
```

`tests/make_fixture_feed.py` prints `DEMO DATA -- fabricated positions, not BMTC.` on every
run. If you point the app at it, the strip will say `127.0.0.1`, which is the honest label:
a local fixture, not a bus.

Two runs are needed because the *negative* case is the requirement: with no feed configured
the map must draw nothing and say why. A test that only checks the happy path would pass
just as well if the app invented positions.

## 9. Security notes

* The feed URL and `GTFS_RT_AUTH_HEADER` are server-side only; neither appears in
  `/api/config/public`, in the HTML, or in `localStorage`.
* `source` in every response is the **hostname**, not the URL - query-string tokens (a
  common pattern: `?token=...`) would otherwise be published in the JSON.
* `GTFS_RT_AUTH_HEADER` and the IUDX client credentials are in `redact.SECRET_ENV`, so a
  provider echoing them in an error body still cannot leak them into a response or a log.
* `GET /api/v1/config/health` checks the *shape* of the realtime settings (http vs https,
  interval floor, placeholder-looking header) and never prints their values.

## 10. Limitations

* **No feed is supplied, and none can be simulated quietly.** The fixture generator is a
  test tool, labelled DEMO DATA, and it is not wired into the app.
* **No ETAs from Trip Updates.** The feed type is decoded and exposed at the API, but the
  UI does not yet mix live delays into scheduled times. Doing it badly - showing a delay
  against a trip that is not actually the one you would catch - would be worse than the
  scheduled time it would replace.
* **No history.** Nothing is written to SQLite. Positions are read, labelled and discarded.
* **No clustering.** At `limit=800` a city view is fine; a 6,000-vehicle feed would need
  marker clustering or a bbox-driven request, which is a UI change nobody has needed yet.
* **No vehicle-to-route matching against static GTFS.** `route_id` is passed through as the
  feed sends it; if a provider uses internal ids that do not match the GTFS `route_id`,
  the tooltip shows the provider's id rather than a guessed route number.
* **Bearing is drawn raw.** No smoothing, because smoothing is an inference about where a
  bus is going, and the map should show what the provider sent.
