"""Road routing with an explicit, configurable provider chain.

Three providers can produce a road route, and they are not interchangeable:

| provider | geometry | traffic | cost |
|---|---|---|---|
| Google Routes | encoded polyline | live, when asked | billed per request |
| OpenRouteService | GeoJSON LineString | **none** (static OSM network) | free tier, rate-limited |
| TomTom | points array | live, when asked | metered |

So the chain is **resolved once, from configuration**, not chosen by an ``if`` buried in a
view, and every result carries the provider that produced it plus the quality flags that
provider can honestly support. A caller never sees an ORS duration labelled traffic-aware,
and it never sees a straight line labelled a road route.

Order comes from ``ROAD_ROUTING_CHAIN`` (comma separated, default
``google_routes,openrouteservice,tomtom``). ``ROAD_ROUTING_PROVIDER`` pins a single
provider and disables fallback, which is what the Settings screen exposes for debugging.
An integration switched off in Settings is skipped even if it is in the chain, and one
whose registry state is ``auth_failure`` / ``quota_or_billing`` is skipped too, because a
revoked key should not sit in front of a working one.
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

from src.services import http
from src.services import integrations as reg
from src.services.redact import redact

LABELS = {
    "google_routes": "Google Routes",
    "openrouteservice": "OpenRouteService",
    "tomtom": "TomTom",
}
ATTRIBUTION = {
    "google_routes": ("Google Maps", "https://www.google.com/intl/en_in/help/terms_maps/"),
    "openrouteservice": ("openrouteservice.org | OpenStreetMap contributors",
                         "https://openrouteservice.org/terms-of-service/"),
    "tomtom": ("TomTom", "https://www.tomtom.com/legal/"),
}
TRAFFIC_CAPABLE = {"google_routes", "tomtom"}
DEFAULT_CHAIN = "google_routes,openrouteservice,tomtom"
TIMEOUT_S = 10.0
GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
TOMTOM_URL = ("https://api.tomtom.com/routing/1/calculateRoute/"
              "{o_lat},{o_lon}:{d_lat},{d_lon}/json")


class RoadRoutingError(RuntimeError):
    pass


# ------------------------------------------------------------------- configuration
def _provider_id(name: str) -> str:
    """Accept the aliases people actually type in .env."""
    n = (name or "").strip().lower()
    return {"google": "google_routes", "google_routes": "google_routes",
            "ors": "openrouteservice", "openrouteservice": "openrouteservice",
            "tomtom": "tomtom", "none": "", "off": "", "disabled": ""}.get(n, n)


def configured_chain() -> list[str]:
    raw = (os.getenv("ROAD_ROUTING_CHAIN") or DEFAULT_CHAIN)
    ids = [_provider_id(p) for p in raw.split(",")]
    return [p for p in ids if p in LABELS]


def resolve_chain(*, traffic: bool = False) -> tuple[list[str], list[dict]]:
    """Usable providers, in order, plus the reason each rejected one was skipped."""
    pinned = _provider_id(os.getenv("ROAD_ROUTING_PROVIDER", ""))
    candidates = [pinned] if pinned else configured_chain()
    if pinned and pinned not in LABELS:
        candidates = []

    usable: list[str] = []
    skipped: list[dict] = []
    for pid in candidates:
        st = reg.status(pid) or {}
        if not st.get("configured"):
            skipped.append({"provider": pid, "reason": "no credential configured"})
            continue
        if not st.get("enabled", True):
            skipped.append({"provider": pid, "reason": "switched off in Settings"})
            continue
        if st.get("state") in (reg.AUTH_FAILURE, reg.QUOTA_OR_BILLING):
            skipped.append({"provider": pid, "reason": st.get("reason") or st.get("state")})
            continue
        if traffic and pid not in TRAFFIC_CAPABLE:
            skipped.append({"provider": pid, "reason": "no live traffic from this provider"})
            continue
        usable.append(pid)
    return usable, skipped


def describe() -> dict:
    """What the planner would use right now, for the Settings and coverage pages."""
    road, skipped = resolve_chain(traffic=False)
    traffic, traffic_skipped = resolve_chain(traffic=True)
    return {
        "chain_configured": configured_chain(),
        "pinned": _provider_id(os.getenv("ROAD_ROUTING_PROVIDER", "")) or None,
        "road_order": road,
        "traffic_order": traffic,
        # A provider skipped for both reasons is one row, not two.
        "skipped": list({(d["provider"], d["reason"]): d
                         for d in skipped + traffic_skipped}.values()),
        "traffic_aware_providers": sorted(TRAFFIC_CAPABLE & set(road)),
        "fallback": None if road else "unavailable (no straight-line substitution)",
        "note": ("Providers are tried in this order. Each result names the provider that "
                 "produced it; a straight-line estimate is labelled as such and never "
                 "presented as a road route."),
    }


# ------------------------------------------------------------------------- geometry
def decode_polyline(encoded: str) -> list[list[float]]:
    """Google's encoded polyline -> [[lat, lon], ...]."""
    if not encoded:
        return []
    points: list[list[float]] = []
    index = lat = lon = 0
    length = len(encoded)
    while index < length:
        for axis in (0, 1):
            shift = result = 0
            while True:
                if index >= length:
                    return points
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if axis == 0:
                lat += delta
            else:
                lon += delta
        points.append([round(lat * 1e-5, 6), round(lon * 1e-5, 6)])
    return points


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    from math import asin, cos, radians, sin, sqrt
    lat1, lon1 = radians(a[0]), radians(a[1])
    lat2, lon2 = radians(b[0]), radians(b[1])
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * asin(sqrt(h))


# ------------------------------------------------------------------------- providers
def _google(origin, destination, *, departure_time: str, want_traffic: bool,
            alternatives: bool) -> list[dict]:
    key = (os.getenv("GOOGLE_MAPS_SERVER_API_KEY")
           or os.getenv("GOOGLE_MAPS_SERVER_KEY") or "").strip()
    if not key:
        raise RoadRoutingError("GOOGLE_MAPS_SERVER_API_KEY is not set")
    body: dict[str, Any] = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": destination[0],
                                                "longitude": destination[1]}}},
        "travelMode": "DRIVE",
        "routingPreference": ("TRAFFIC_AWARE_OPTIMAL" if want_traffic
                              else "TRAFFIC_UNAWARE"),
        "computeAlternativeRoutes": bool(alternatives),
        "units": "METRIC",
    }
    if departure_time:
        body["departureTime"] = departure_time
    started = time.time()
    with httpx.Client(timeout=TIMEOUT_S) as c:
        r = c.post(GOOGLE_ROUTES_URL, json=body,
                   headers={"Content-Type": "application/json",
                            "X-Goog-Api-Key": key,
                            "X-Goog-FieldMask": ("routes.duration,routes.staticDuration,"
                                                 "routes.distanceMeters,"
                                                 "routes.polyline.encodedPolyline,"
                                                 "routes.description")})
    latency = int((time.time() - started) * 1000)
    if r.status_code != 200:
        detail = f"HTTP {r.status_code}: {r.text[:200]}"
        http.log_call("google_routes", "computeRoutes_chain", False, r.status_code,
                      latency, detail)
        raise RoadRoutingError(detail)
    data = r.json()
    routes = (data.get("routes") or [])[:3]
    http.log_call("google_routes", "computeRoutes_chain", bool(routes), 200, latency)
    out = []
    for r0 in routes:
        live = _secs(r0.get("duration"))
        static = _secs(r0.get("staticDuration"))
        metres = r0.get("distanceMeters") or 0
        out.append({
            "distance_km": round(metres / 1000.0, 2),
            "duration_min": round(live / 60.0, 1),
            "duration_free_flow_min": round(static / 60.0, 1) if static else None,
            "delay_min": round((live - static) / 60.0, 1) if (live and static) else None,
            "geometry": decode_polyline((r0.get("polyline") or {}).get("encodedPolyline")),
            "geometry_kind": "polyline",
            "summary": r0.get("description") or "",
            "traffic_aware": want_traffic,
        })
    return out


def _ors(origin, destination, *, departure_time: str, want_traffic: bool,
         alternatives: bool) -> list[dict]:
    key = (os.getenv("OPENROUTESERVICE_API_KEY") or "").strip()
    if not key:
        raise RoadRoutingError("OPENROUTESERVICE_API_KEY is not set")
    body = {"coordinates": [[origin[1], origin[0]], [destination[1], destination[0]]],
            "instructions": False, "units": "km"}
    started = time.time()
    with httpx.Client(timeout=TIMEOUT_S) as c:
        r = c.post(ORS_URL, json=body,
                   headers={"Authorization": key, "Accept": "application/json"})
    latency = int((time.time() - started) * 1000)
    if r.status_code != 200:
        detail = f"HTTP {r.status_code}: {r.text[:200]}"
        http.log_call("openrouteservice", "directions_chain", False, r.status_code,
                      latency, detail)
        raise RoadRoutingError(detail)
    data = r.json()
    feats = data.get("features") or []
    http.log_call("openrouteservice", "directions_chain", bool(feats), 200, latency)
    out = []
    for f in feats[:3]:
        props = f.get("properties") or {}
        summary = props.get("summary") or {}
        km = summary.get("distance")
        secs = summary.get("duration")
        coords = ((f.get("geometry") or {}).get("coordinates") or [])
        out.append({
            "distance_km": round(km, 2) if km is not None else None,
            "duration_min": round(secs / 60.0, 1) if secs is not None else None,
            "duration_free_flow_min": None,          # ORS has no free-flow reference
            "delay_min": None,
            "geometry": [[round(lat, 6), round(lon, 6)] for lon, lat in coords],
            "geometry_kind": "polyline",
            "summary": "ORS driving-car",
            "traffic_aware": False,                  # static network, no live traffic
        })
    return out


def _tomtom(origin, destination, *, departure_time: str, want_traffic: bool,
            alternatives: bool) -> list[dict]:
    key = (os.getenv("TOMTOM_API_KEY") or "").strip()
    if not key:
        raise RoadRoutingError("TOMTOM_API_KEY is not set")
    url = TOMTOM_URL.format(o_lat=origin[0], o_lon=origin[1],
                            d_lat=destination[0], d_lon=destination[1])
    params = {"key": key, "travelMode": "car", "traffic": "true" if want_traffic else "false",
              "computeTravelTimeFor": "all", "routeType": "fastest",
              "maxAlternatives": 2 if alternatives else 0}
    started = time.time()
    with httpx.Client(timeout=TIMEOUT_S) as c:
        r = c.get(url, params=params)
    latency = int((time.time() - started) * 1000)
    if r.status_code != 200:
        detail = f"HTTP {r.status_code}: {r.text[:200]}"
        http.log_call("tomtom", "calculateRoute_chain", False, r.status_code, latency, detail)
        raise RoadRoutingError(detail)
    data = r.json()
    routes = (data.get("routes") or [])[:3]
    http.log_call("tomtom", "calculateRoute_chain", bool(routes), 200, latency)
    out = []
    for r0 in routes:
        s = r0.get("summary") or {}
        pts = []
        for leg in r0.get("legs") or []:
            for p in leg.get("points") or []:
                pts.append([round(p.get("latitude", 0), 6), round(p.get("longitude", 0), 6)])
        secs = s.get("travelTimeInSeconds")
        no_traffic = s.get("noTrafficTravelTimeInSeconds")
        out.append({
            "distance_km": round((s.get("lengthInMeters") or 0) / 1000.0, 2),
            "duration_min": round(secs / 60.0, 1) if secs else None,
            "duration_free_flow_min": round(no_traffic / 60.0, 1) if no_traffic else None,
            "delay_min": (round((s.get("trafficDelayInSeconds") or 0) / 60.0, 1)
                          if s.get("trafficDelayInSeconds") is not None else None),
            "geometry": pts,
            "geometry_kind": "polyline",
            "summary": "TomTom fastest",
            "traffic_aware": bool(want_traffic),
        })
    return out


# Names, not references: a reference captured at import time cannot be swapped in a test or
# in a follow-up provider without editing this line, which is how a chain drifts silently.
CALLER_NAMES = {"google_routes": "_google", "openrouteservice": "_ors", "tomtom": "_tomtom"}


def _caller(provider_id: str):
    return globals()[CALLER_NAMES[provider_id]]


# --------------------------------------------------------------------------- public
def plan(origin: tuple[float, float], destination: tuple[float, float], *,
         departure_time: str = "", want_traffic: bool = True,
         alternatives: bool = True, allow_estimate: bool = False) -> dict:
    """Try the chain in order. Returns the first success, with every attempt recorded."""
    chain, skipped = resolve_chain(traffic=False)
    attempts: list[dict] = []
    for pid in chain:
        started = time.time()
        try:
            routes = _caller(pid)(origin, destination, departure_time=departure_time,
                                  want_traffic=want_traffic and pid in TRAFFIC_CAPABLE,
                                  alternatives=alternatives)
        except (RoadRoutingError, httpx.HTTPError, ValueError) as exc:
            attempts.append({"provider": pid, "ok": False, "ms": int((time.time() - started) * 1000),
                             "error": redact(str(exc))[:300]})
            continue
        if not routes:
            attempts.append({"provider": pid, "ok": False,
                             "ms": int((time.time() - started) * 1000),
                             "error": "provider returned no route"})
            continue
        attempts.append({"provider": pid, "ok": True,
                         "ms": int((time.time() - started) * 1000), "error": ""})
        label, url = ATTRIBUTION[pid]
        return {
            "ok": True,
            "provider": pid,
            "provider_label": LABELS[pid],
            "traffic_aware": any(r["traffic_aware"] for r in routes),
            "routes": routes,
            "attempts": attempts,
            "skipped": skipped,
            "attribution": label,
            "attribution_url": url,
            "is_estimate": False,
            "note": ("" if routes[0]["traffic_aware"] else
                     f"{LABELS[pid]} returns a modelled duration from a static road "
                     f"network; it is not live traffic."),
        }

    # No provider answered. The default is to say so: a car trip approximated from a
    # straight line is exactly the invented number this project refuses to show.
    if allow_estimate:
        straight = _straight(origin, destination)
        return {
            "ok": True,
            "provider": "straight_line",
            "provider_label": "Straight-line estimate",
            "traffic_aware": False,
            "routes": [straight],
            "attempts": attempts,
            "skipped": skipped,
            "attribution": "none",
            "attribution_url": "",
            "is_estimate": True,
            "note": ("No road-routing provider answered, so this is the straight-line "
                     "distance times a 1.35 circuity factor at 28 km/h. It is labelled an "
                     "estimate everywhere it appears."),
        }
    return {
        "ok": False, "available": False,
        "provider": None,
        "provider_label": None,
        "traffic_aware": False,
        "routes": [],
        "attempts": attempts,
        "skipped": skipped,
        "is_estimate": False,
        "reason": unavailable_reason()["note"] if not chain else
                  "Every configured road-routing provider failed (see attempts).",
        **unavailable_reason(),
    }


def unavailable_reason() -> dict:
    """Why nothing can route by road, in the terms each provider needs."""
    chain, skipped = resolve_chain()
    return {
        "available": bool(chain),
        "providers_ready": chain,
        "skipped": skipped,
        "needs": ["GOOGLE_MAPS_SERVER_API_KEY (Routes API)",
                  "OPENROUTESERVICE_API_KEY", "TOMTOM_API_KEY"],
        "docs": "https://developers.google.com/maps/documentation/routes",
        "note": ("Configure any one of these, then test it in Settings. Until then the app "
                 "does not estimate a car trip from straight-line distance."),
    }


def _straight(origin, destination) -> dict:
    km = _haversine(origin, destination) * 1.35
    return {"distance_km": round(km, 2),
            "duration_min": round(km / 28.0 * 60.0, 1),   # 28 km/h urban average, stated
            "duration_free_flow_min": None,
            "delay_min": None,
            "geometry": [[origin[0], origin[1]], [destination[0], destination[1]]],
            "geometry_kind": "straight_line",
            "summary": "straight line x 1.35, 28 km/h assumed",
            "traffic_aware": False}


def _secs(dur: str | None) -> float:
    if not dur:
        return 0.0
    try:
        return float(str(dur).rstrip("s"))
    except ValueError:
        return 0.0


def tomtom_traffic(origin: tuple[float, float], destination: tuple[float, float],
                   *, departure_time: str = "") -> dict:
    """Traffic reading from TomTom, normalised to the shape ``traffic.road_traffic`` uses.

    TomTom reports delay in seconds rather than a speed profile, so ``speed_intervals`` stays
    empty -- that is a property of the provider, not a missing fetch.
    """
    key = (os.getenv("TOMTOM_API_KEY") or "").strip()
    if not key:
        return {"available": False, "provider": "tomtom",
                "reason": "TOMTOM_API_KEY is not set."}
    url = TOMTOM_URL.format(o_lat=origin[0], o_lon=origin[1],
                            d_lat=destination[0], d_lon=destination[1])
    params = {"key": key, "travelMode": "car", "traffic": "true",
              "computeTravelTimeFor": "all", "routeType": "fastest"}
    started = time.time()
    try:
        with httpx.Client(timeout=TIMEOUT_S) as c:
            r = c.get(url, params=params)
    except httpx.HTTPError as exc:
        return {"available": False, "provider": "tomtom",
                "reason": f"transport error: {type(exc).__name__}"}
    latency = int((time.time() - started) * 1000)
    if r.status_code != 200:
        http.log_call("tomtom", "calculateRoute_traffic", False, r.status_code, latency,
                      r.text[:200])
        return {"available": False, "provider": "tomtom",
                "reason": f"TomTom answered HTTP {r.status_code}",
                "status_code": r.status_code}
    data = r.json()
    routes = data.get("routes") or []
    if not routes:
        return {"available": False, "provider": "tomtom",
                "reason": "TomTom returned no route for those coordinates."}
    http.log_call("tomtom", "calculateRoute_traffic", True, 200, latency)
    s = routes[0].get("summary") or {}
    live = s.get("travelTimeInSeconds")
    no_traffic = s.get("noTrafficTravelTimeInSeconds")
    delay = s.get("trafficDelayInSeconds")
    ratio = (live / no_traffic) if (live and no_traffic) else None
    from src.services.traffic import _level, _utc_now
    return {
        "available": True,
        "provider": "tomtom",
        "provider_label": "TomTom",
        "measured_at": _utc_now(),
        "distance_km": round((s.get("lengthInMeters") or 0) / 1000.0, 2),
        "duration_with_traffic_min": round(live / 60.0, 1) if live else None,
        "duration_free_flow_min": round(no_traffic / 60.0, 1) if no_traffic else None,
        "delay_minutes": round(delay / 60.0, 1) if delay is not None else None,
        "congestion_ratio": round(ratio, 2) if ratio else None,
        "level": _level(ratio),
        # TomTom returns a delay, not per-segment speeds; an empty list is the honest answer.
        "speed_intervals": [],
        "traffic_model": "tomtom live",
        "departure_time": departure_time or "now",
        "latency_ms": latency,
        "cached": False,
        "attribution": "TomTom",
        "note": ("travelTimeInSeconds / noTrafficTravelTimeInSeconds are TomTom's own "
                 "figures. TomTom reports a delay rather than per-segment speeds, so no "
                 "speed profile is shown."),
    }
