"""Connection tests for every integration, and the state each result implies.

Two classes of check, and the difference is the whole point:

* **Free / local** -- Ollama's ``/api/tags``, the GTFS tables, the tile cache, the vector
  index. These run on every Settings page load because they cost nothing and they tell the
  truth. Nothing here leaves the machine except one loopback HTTP call.
* **Metered** -- Google Places/Geocoding/Routes, OpenRouteService, TomTom. These *never*
  run implicitly. ``POST /api/v1/integrations/{id}/test`` returns 409 unless the caller
  sends ``confirm_billable: true``, and the Settings button says what one probe costs.

Every probe returns the same shape, so the API and the UI do not branch on provider:

    {ok, state, detail, latency_ms, http_status, kind, billable_request}
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Callable

import httpx

from src.config import DB_PATH, PROCESSED
from src.services import http
from src.services import integrations as reg
from src.services.ollama_client import status as ollama_status
from src.services.redact import redact

TIMEOUT_S = 8.0
GOOGLE = "https://maps.googleapis.com/maps/api"
ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
ORS_BASE = "https://api.openrouteservice.org"
TOMTOM = "https://api.tomtom.com"

# Test coordinates: Cubbon Park to Majestic, about 3 km apart, inside the covered area.
BENGALURU_A = (12.9763, 77.5929)
BENGALURU_B = (12.9767, 77.5713)


class ProbeRefused(RuntimeError):
    """Raised when a billable probe was requested without explicit confirmation."""

    def __init__(self, integration_id: str, warning: str) -> None:
        super().__init__(warning)
        self.integration_id = integration_id
        self.warning = warning


# --------------------------------------------------------------------- local checks
def live_ollama() -> dict:
    st = ollama_status(timeout=0.8)
    ok = bool(st.get("available") and st.get("model_pulled"))
    raw = st.get("models") or []
    names = [m if isinstance(m, str) else (m or {}).get("name", "") for m in raw]
    return {"ok": ok, "detail": st.get("reason") or "model reachable",
            "models": [n for n in names if n][:6],
            "model": st.get("model")}


def live_gtfs() -> dict:
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        stops = con.execute("SELECT COUNT(*) FROM stops").fetchone()[0]
        routes = con.execute("SELECT COUNT(*) FROM routes").fetchone()[0]
        metro = con.execute(
            "SELECT COUNT(DISTINCT station_id) FROM metro_route_stops").fetchone()[0]
        con.close()
    except sqlite3.Error as exc:
        return {"ok": False, "detail": f"SQLite: {exc}"}
    ok = stops > 0 and routes > 0
    return {"ok": ok,
            "detail": (f"{stops:,} bus stops, {routes:,} bus routes, "
                       f"{metro:,} metro stations on a line")}


def live_tiles() -> dict:
    from web.api.routes_tiles import tile_stats
    st = tile_stats()
    served = st.get("served") or st.get("tiles") or 0
    return {"ok": True,
            "detail": f"proxy configured ({served:,} tiles cached locally)"}


def live_rag() -> dict:
    meta_path = PROCESSED / "index_meta.json"
    if not meta_path.exists():
        return {"ok": False, "detail": "no vector index built (data/processed/index_meta.json)"}
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError) as exc:
        return {"ok": False, "detail": f"unreadable index metadata: {exc}"}
    docs = meta.get("documents") or meta.get("docs") or meta.get("count")
    if not docs:
        return {"ok": False, "detail": "index metadata has no document count"}
    return {"ok": True, "detail": f"{docs} policy documents indexed"}


def live_gtfs_rt() -> dict:
    """Read the configured GTFS-Realtime feed once.

    Uses the same cache as the map, so testing from Settings does not add a poll. Reports
    how many vehicles the feed actually contains and how many are within the staleness
    window -- a feed that answers 200 with an empty body is not a working feed.
    """
    from src.services import gtfs_rt
    st = gtfs_rt.status()
    if not st.get("configured"):
        return {"ok": False, "detail": "no GTFS_RT_VEHICLE_URL configured"}
    if not st.get("ok"):
        return {"ok": False, "detail": f"{st.get('kind')}: {st.get('reason')}"}
    return {"ok": True, "detail": st.get("reason", "feed readable")}


LIVE_CHECKS: dict[str, Callable[[], dict]] = {
    "ollama": live_ollama,
    "gtfs_realtime": live_gtfs_rt,
    "gtfs_static": live_gtfs,
    "osm_tiles": live_tiles,
    "policy_rag": live_rag,
}


# ------------------------------------------------------------------- classification
def _state_from_http(status: int | None, provider_status: str = "", message: str = "") -> str:
    text = f"{provider_status} {message}".upper()
    if status is None:
        return reg.PROVIDER_UNAVAILABLE
    if status == 429 or any(k in text for k in ("OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT",
                                                "RESOURCE_EXHAUSTED", "QUOTA")):
        return reg.QUOTA_OR_BILLING
    if status in (401, 403) or any(k in text for k in ("REQUEST_DENIED", "PERMISSION_DENIED",
                                                       "API KEY NOT VALID", "API_KEY_INVALID",
                                                       "UNAUTHORIZED", "FORBIDDEN")):
        return reg.AUTH_FAILURE
    if status >= 500:
        return reg.PROVIDER_UNAVAILABLE
    return reg.PROVIDER_UNAVAILABLE


def _result(*, ok: bool, state: str, detail: str, latency_ms: int,
            http_status: int | None = None, billable: bool = False) -> dict:
    return {"ok": ok, "state": state, "detail": redact(detail)[:600],
            "latency_ms": latency_ms, "http_status": http_status,
            "billable_request": billable}


def _google_get(path: str, params: dict, *, provider: str, endpoint: str) -> dict:
    key = _server_key()
    q = {**params, "key": key}
    started = time.time()
    try:
        with httpx.Client(timeout=TIMEOUT_S) as c:
            r = c.get(f"{GOOGLE}/{path}", params=q)
        latency = int((time.time() - started) * 1000)
        try:
            body = r.json()
        except ValueError:
            body = {}
        pstatus = str(body.get("status") or "")
        message = str(body.get("error_message") or body.get("error") or "")[:300]
        ok = r.status_code == 200 and pstatus in ("OK", "ZERO_RESULTS")
        state = reg.READY if ok else _state_from_http(r.status_code, pstatus, message)
        detail = (f"Google answered {pstatus or r.status_code}"
                  + (f" - {message}" if message and not ok else ""))
        http.log_call(provider, endpoint, ok, r.status_code, latency,
                      "" if ok else detail)
        return _result(ok=ok, state=state, detail=detail, latency_ms=latency,
                       http_status=r.status_code, billable=True)
    except httpx.HTTPError as exc:
        latency = int((time.time() - started) * 1000)
        detail = f"transport error: {type(exc).__name__}: {exc}"
        http.log_call(provider, endpoint, False, None, latency, detail)
        return _result(ok=False, state=reg.PROVIDER_UNAVAILABLE, detail=detail,
                       latency_ms=latency, billable=True)


def _server_key() -> str:
    from src.services.providers import providers
    key = providers().server_key
    if not key:
        raise ProbeRefused("google_places", "GOOGLE_MAPS_SERVER_API_KEY is not set")
    return key


# ------------------------------------------------------------------------ probes
def probe_google_places() -> dict:
    # Same endpoint the app itself uses, so the test proves the code path, not a
    # different one. One request == one billable Autocomplete call.
    return _google_get("place/autocomplete/json",
                       {"input": "Majestic", "components": "country:in"},
                       provider="google_places", endpoint="probe_autocomplete")


def probe_google_geocoding() -> dict:
    return _google_get("geocode/json", {"address": "Bengaluru, Karnataka", "region": "in"},
                       provider="google_geocoding", endpoint="probe_geocode")


def _routes_probe(routing_preference: str, provider: str, endpoint: str) -> dict:
    key = _server_key()
    body = {
        "origin": {"location": {"latLng": {"latitude": BENGALURU_A[0],
                                           "longitude": BENGALURU_A[1]}}},
        "destination": {"location": {"latLng": {"latitude": BENGALURU_B[0],
                                                "longitude": BENGALURU_B[1]}}},
        "travelMode": "DRIVE",
        "routingPreference": routing_preference,
        "units": "METRIC",
    }
    started = time.time()
    try:
        with httpx.Client(timeout=TIMEOUT_S) as c:
            r = c.post(ROUTES, json=body,
                       headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters"})
        latency = int((time.time() - started) * 1000)
        try:
            data = r.json()
        except ValueError:
            data = {}
        routes = data.get("routes") or []
        err = data.get("error") or {}
        message = str(err.get("message") or data.get("error_message") or "")[:300]
        ok = r.status_code == 200 and bool(routes)
        if ok:
            metres = routes[0].get("distanceMeters")
            detail = (f"Routes API returned a {round((metres or 0) / 1000, 2)} km route "
                      f"({routing_preference})")
        else:
            detail = message or f"HTTP {r.status_code}"
        state = reg.READY if ok else _state_from_http(r.status_code,
                                                      str(err.get("status") or ""), message)
        http.log_call(provider, endpoint, ok, r.status_code, latency,
                      "" if ok else detail)
        return _result(ok=ok, state=state, detail=detail, latency_ms=latency,
                       http_status=r.status_code, billable=True)
    except httpx.HTTPError as exc:
        latency = int((time.time() - started) * 1000)
        detail = f"transport error: {type(exc).__name__}: {exc}"
        http.log_call(provider, endpoint, False, None, latency, detail)
        return _result(ok=False, state=reg.PROVIDER_UNAVAILABLE, detail=detail,
                       latency_ms=latency, billable=True)


def probe_google_routes() -> dict:
    return _routes_probe("TRAFFIC_UNAWARE", "google_routes", "probe_computeRoutes")


def probe_google_traffic() -> dict:
    return _routes_probe("TRAFFIC_AWARE_OPTIMAL", "google_routes", "probe_traffic")


def probe_openrouteservice() -> dict:
    """Health first (free), then one directions call to prove the key (consumes quota)."""
    started = time.time()
    try:
        with httpx.Client(timeout=TIMEOUT_S) as c:
            h = c.get(f"{ORS_BASE}/ors/v2/health")
        if h.status_code >= 500:
            return _result(ok=False, state=reg.PROVIDER_UNAVAILABLE,
                           detail=f"ORS health endpoint answered HTTP {h.status_code}",
                           latency_ms=int((time.time() - started) * 1000),
                           http_status=h.status_code, billable=False)
        key = _ors_key()
        with httpx.Client(timeout=TIMEOUT_S) as c:
            r = c.get(
                f"{ORS_BASE}/v2/directions/driving-car",
                params={"api_key": key,
                        "start": f"{BENGALURU_A[1]},{BENGALURU_A[0]}",
                        "end": f"{BENGALURU_B[1]},{BENGALURU_B[0]}"},
                headers={"Accept": "application/json"})
        latency = int((time.time() - started) * 1000)
        try:
            data = r.json()
        except ValueError:
            data = {}
        err = data.get("error")
        message = str(err.get("message") if isinstance(err, dict) else err or "")[:300]
        ok = r.status_code == 200 and bool(data.get("features"))
        state = reg.READY if ok else _state_from_http(r.status_code, "", message)
        detail = (f"directions returned {len(data.get('features') or [])} feature(s)"
                  if ok else (message or f"HTTP {r.status_code}"))
        http.log_call("openrouteservice", "probe_directions", ok, r.status_code, latency,
                      "" if ok else detail)
        return _result(ok=ok, state=state, detail=detail, latency_ms=latency,
                       http_status=r.status_code, billable=True)
    except ProbeRefused:
        raise
    except httpx.HTTPError as exc:
        latency = int((time.time() - started) * 1000)
        detail = f"transport error: {type(exc).__name__}: {exc}"
        http.log_call("openrouteservice", "probe_directions", False, None, latency, detail)
        return _result(ok=False, state=reg.PROVIDER_UNAVAILABLE, detail=detail,
                       latency_ms=latency, billable=True)


def _ors_key() -> str:
    import os
    key = (os.getenv("OPENROUTESERVICE_API_KEY") or "").strip()
    if not key:
        raise ProbeRefused("openrouteservice", "OPENROUTESERVICE_API_KEY is not set")
    return key


def probe_tomtom() -> dict:
    import os
    key = (os.getenv("TOMTOM_API_KEY") or "").strip()
    if not key:
        raise ProbeRefused("tomtom", "TOMTOM_API_KEY is not set")
    url = (f"{TOMTOM}/routing/1/calculateRoute/"
           f"{BENGALURU_A[0]},{BENGALURU_A[1]}:{BENGALURU_B[0]},{BENGALURU_B[1]}/json")
    started = time.time()
    try:
        with httpx.Client(timeout=TIMEOUT_S) as c:
            r = c.get(url, params={"key": key, "traffic": "true",
                                   "travelMode": "car"})
        latency = int((time.time() - started) * 1000)
        try:
            data = r.json()
        except ValueError:
            data = {}
        err = data.get("detailedError") or data.get("error") or {}
        message = str((err or {}).get("message") or "")[:300]
        ok = r.status_code == 200 and bool((data.get("routes") or []))
        state = reg.READY if ok else _state_from_http(r.status_code, "", message)
        if ok:
            summary = (data.get("routes") or [{}])[0].get("summary") or {}
            detail = (f"routing returned {round((summary.get('lengthInMeters') or 0) / 1000, 2)}"
                      f" km, {round((summary.get('travelTimeInSeconds') or 0) / 60)} min")
        else:
            detail = message or f"HTTP {r.status_code}"
        http.log_call("tomtom", "probe_calculateRoute", ok, r.status_code, latency,
                      "" if ok else detail)
        return _result(ok=ok, state=state, detail=detail, latency_ms=latency,
                       http_status=r.status_code, billable=True)
    except httpx.HTTPError as exc:
        latency = int((time.time() - started) * 1000)
        detail = f"transport error: {type(exc).__name__}: {exc}"
        http.log_call("tomtom", "probe_calculateRoute", False, None, latency, detail)
        return _result(ok=False, state=reg.PROVIDER_UNAVAILABLE, detail=detail,
                       latency_ms=latency, billable=True)


PROBES: dict[str, Callable[[], dict]] = {
    "google_places": probe_google_places,
    "google_geocoding": probe_google_geocoding,
    "google_routes": probe_google_routes,
    "google_traffic": probe_google_traffic,
    "openrouteservice": probe_openrouteservice,
    "tomtom": probe_tomtom,
}


# ------------------------------------------------------------------------- runner
def run_test(integration_id: str, *, confirm_billable: bool = False) -> dict:
    """Run one integration's test and persist the result.

    Raises :class:`ProbeRefused` when the probe would cost money and the caller has not
    confirmed, so the HTTP layer can answer 409 with the warning instead of quietly
    spending the user's quota.
    """
    defn = reg.CATALOG.get(integration_id)
    if not defn:
        raise KeyError(integration_id)
    if not defn.available_in_build:
        out = _result(ok=False, state=reg.UNSUPPORTED,
                      detail=defn.notes or "not supported in this build", latency_ms=0)
        reg.record_check(integration_id, state=out["state"], ok=False,
                         detail=out["detail"], kind="none")
        return {**out, "integration": reg.status(integration_id)}

    if not reg.is_configured(defn):
        # No credential: report it plainly. Raising ProbeRefused here would come back as
        # "would make a billable request", which is the wrong story and the wrong status.
        missing = [v for v in defn.env_vars if not reg.env_set(v)]
        detail = ("missing " + ", ".join(missing)) if missing else "nothing to test"
        # Deliberately not recorded as a "check": nothing was probed, and a stored row
        # would make the UI claim a test happened.
        out = _result(ok=False, state=reg.NOT_CONFIGURED, detail=detail, latency_ms=0)
        return {**out, "integration": reg.status(integration_id)}

    if defn.test_kind in ("billable", "quota") and not confirm_billable:
        warning = reg.test_warning(defn)
        raise ProbeRefused(integration_id, warning)

    live = LIVE_CHECKS.get(integration_id)
    if live is not None:
        started = time.time()
        res = live()
        latency = int((time.time() - started) * 1000)
        state = reg.READY if res.get("ok") else reg.PROVIDER_UNAVAILABLE
        return {**_result(ok=bool(res.get("ok")), state=state,
                          detail=res.get("detail", ""), latency_ms=latency),
                "integration": reg.status(integration_id)}

    probe = PROBES.get(integration_id)
    if probe is None:
        return _result(ok=False, state=reg.CONFIGURED_UNTESTED,
                       detail=defn.notes or "no automated test for this integration",
                       latency_ms=0) | {"integration": reg.status(integration_id)}

    out = probe()
    reg.record_check(integration_id, state=out["state"], ok=out["ok"],
                     detail=out["detail"], latency_ms=out["latency_ms"],
                     http_status=out["http_status"], kind=defn.test_kind)
    return {**out, "integration": reg.status(integration_id)}
