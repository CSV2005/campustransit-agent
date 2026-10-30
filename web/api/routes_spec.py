"""The endpoint names requested in the brief (§10), with the §11 response contract.

These sit alongside the richer `/api/v1/*` routes rather than replacing them: the UI uses
whichever shape it needs, and anything expecting the published contract gets it here.
"""
from __future__ import annotations

import sqlite3
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config import DB_PATH
from src.services import places, store, traffic, trip_modes
from src.services.api_shape import to_contract
from src.services.planner import plan
from src.services.providers import providers
from src.tools.common import MANUAL_ALIASES
from web.api.schemas import HistoryRequest, SaveRouteRequest

router = APIRouter(tags=["contract"])

TransportMode = Literal["bus", "metro", "walking", "driving"]


# ------------------------------------------------------------------ request models
class PlanRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1, max_length=120)
    destination: str = Field(min_length=1, max_length=120)
    departure_datetime: str | None = None
    arrival_by: str | None = None            # "HH:MM"
    preference: Literal["cheapest", "fastest", "fewest_transfers", "least_walking",
                        "balanced"] = "balanced"
    transport_modes: list[TransportMode] = ["bus", "metro"]
    maximum_walking_distance: int | None = Field(default=None, ge=100, le=20000)
    maximum_transfers: int | None = Field(default=None, ge=0, le=10)

    @field_validator("arrival_by")
    @classmethod
    def _hhmm(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("arrival_by must be HH:MM")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("arrival_by must be a valid 24-hour time")
        return v

    @field_validator("origin", "destination")
    @classmethod
    def _trim(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


# ---------------------------------------------------------------------- public config
@router.get("/api/config/public")
def config_public() -> dict:
    """Everything the browser is allowed to know. Never contains the server key."""
    cfg = providers()
    return {
        **cfg.google_public(),
        "map_provider": cfg.map_provider,
        "tile_url": "/api/tiles/{z}/{x}/{y}.png",
        "default_center": {"lat": 12.9716, "lng": 77.5946},
        "default_zoom": 11,
        "timezone": "Asia/Kolkata",
        "currency": "INR",
        "modes": ["bus", "metro", "walking", "driving"],
        "capabilities": cfg.capabilities(),
        "server_key_exposed": False,
        "note": ("The Google server key is never serialised to the browser. Fares are "
                 "never sourced from Google."),
    }


@router.get("/api/data-sources/status")
def data_source_status() -> dict:
    from src.services import source_audit
    rows = source_audit.seed()
    return {"sources": rows, "count": len(rows)}


# ------------------------------------------------------------------------ locations
@router.get("/api/locations/autocomplete")
def locations_autocomplete(q: str = Query(default="", max_length=120),
                           limit: int = Query(default=8, ge=1, le=20)) -> dict:
    return places.search(q, limit=limit)


@router.get("/api/locations/geocode")
def locations_geocode(q: str = Query(min_length=1, max_length=200)) -> dict:
    r = places.geocode(q)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "no match"))
    return {"query": q, **r}


@router.get("/api/locations/reverse-geocode")
def locations_reverse(lat: float = Query(ge=-90, le=90),
                      lng: float = Query(ge=-180, le=180)) -> dict:
    """Coordinates -> address.

    Google Geocoding when a server key is configured; otherwise the nearest indexed place,
    which the response labels as *not a street address*.
    """
    r = places.reverse_geocode(lat, lng)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "no match"))
    return r


# -------------------------------------------------------------------------- routing
@router.post("/api/routes/plan")
def plan_route(body: PlanRequestBody) -> dict:
    """Plan a trip and return the contract in the brief §11."""
    modes = set(body.transport_modes)
    ride_modes = modes & {"bus", "metro"}
    wants_drive = "driving" in modes
    wants_walk = "walking" in modes

    payload: dict
    if not ride_modes and not wants_walk and not wants_drive:
        raise HTTPException(422, "select at least one transport mode")

    if ride_modes:
        result = plan(body.origin, body.destination, arrival_by=body.arrival_by,
                      preference=body.preference, modes=ride_modes)
        payload = to_contract(result, arrival_by=body.arrival_by)
    else:
        payload = {"origin": {"name": body.origin}, "destination": {"name": body.destination},
                   "routes": [], "ok": False, "warnings": [],
                   "departure_advice": {}, "generated_at": None}

    origin = places.geocode(body.origin)
    dest = places.geocode(body.destination)
    _warn_if_snapped(payload, body.origin, origin)
    _warn_if_snapped(payload, body.destination, dest)
    if origin.get("ok") and dest.get("ok"):
        payload["origin"] = {"name": origin.get("formatted"), "latitude": origin["lat"],
                             "longitude": origin["lon"]}
        payload["destination"] = {"name": dest.get("formatted"),
                                  "latitude": dest["lat"], "longitude": dest["lon"]}
        if wants_walk:
            w = trip_modes.walk_trip(origin, dest)
            payload["routes"].append(_walk_route(w, body.arrival_by))
        if wants_drive:
            d = trip_modes.drive_trip(origin, dest)
            if d.get("available"):
                payload["routes"].extend(_drive_routes(d, body.arrival_by))
            else:
                payload["warnings"].append(
                    f"Driving unavailable: {d['reason']} (needs {d.get('needs', 'a key')})")

    if body.maximum_transfers is not None:
        before = len(payload["routes"])
        payload["routes"] = [r for r in payload["routes"]
                             if (r.get("transfers") or 0) <= body.maximum_transfers]
        if len(payload["routes"]) < before:
            payload["warnings"].append(
                f"{before - len(payload['routes'])} route(s) hidden by the "
                f"maximum_transfers={body.maximum_transfers} filter.")

    payload["ok"] = bool(payload["routes"])
    if not payload["ok"] and not payload.get("warnings"):
        payload["warnings"] = ["No verified route found between those places."]
    if not payload["ok"] and ride_modes and origin.get("ok") and dest.get("ok"):
        # An empty result is only useful if it says which way out exists. Name the nearest
        # station that *is* in service, and why the requested mode could not reach it.
        if ride_modes == {"metro"}:
            near_o = _nearest_metro(origin["lat"], origin["lon"])
            near_d = _nearest_metro(dest["lat"], dest["lon"])
            far = []
            for label, near, limit_km in (("origin", near_o, 1.0),
                                          ("destination", near_d, 0.8)):
                if near is None:
                    payload["warnings"].append(
                        f"No operational Namma Metro station within 6 km of the {label}.")
                elif near["distance_km"] > limit_km:
                    payload["warnings"].append(
                        f"Nearest metro station to the {label} is {near['name']}, "
                        f"{near['distance_km']} km away — beyond the {limit_km} km "
                        f"walking connector this model allows, so a metro-only trip "
                        f"cannot start there.")
                    far.append(label)
            if not far:
                payload["warnings"].append(
                    f"The nearest stations ({near_o['name'] if near_o else '—'} and "
                    f"{near_d['name'] if near_d else '—'}) are both reachable, but no "
                    f"metro-only path joins them — the lines do not connect without a "
                    f"bus leg. Pink Line stations are excluded because that line is "
                    f"not in service.")
            payload["warnings"].append(
                "Try All Modes or BMTC Bus for this pair; a metro-only search is "
                "answering a question the current network cannot.")
        else:
            payload["warnings"].append(
                f"No {'/'.join(sorted(ride_modes))} itinerary connects those places. "
                f"Try a different mode combination or a closer landmark.")
    # KSRTC is quoted alongside the routed options, never mixed into them: there is no
    # KSRTC GTFS in this build, so anything it contributes here is a fare for a corridor,
    # with its source and its uncertainty attached.
    try:
        from src.services import intercity
        payload["intercity"] = intercity.options_for(
            body.origin, body.destination, origin, dest)
    except Exception as exc:                       # pragma: no cover - never break a plan
        payload["intercity"] = {"available": False, "corridors": [],
                                "note": f"KSRTC corridor lookup failed: {exc}"}

    # The same warning can be raised by more than one stage (origin snapping, an empty
    # result, a mode filter). Show each sentence once, in the order it first appeared.
    seen: set[str] = set()
    payload["warnings"] = [w for w in payload.get("warnings", [])
                           if not (w in seen or seen.add(w))]
    payload["requested"] = body.model_dump()
    return payload


def _nearest_metro(lat: float, lng: float, radius_km: float = 6.0) -> dict | None:
    """Nearest *operational* Namma Metro station to a coordinate, from the BMRCL GTFS.

    Under-construction stations are filtered out: telling a student to walk to a Pink
    Line station that is not carrying passengers would be worse than saying nothing.
    """
    from src.tools.common import haversine_km
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT stop_name, CAST(stop_lat AS REAL) lat, CAST(stop_lon AS REAL) lon "
            "FROM metro_stops WHERE service_status IN ('operational', '') "
            "AND CAST(stop_lat AS REAL) IS NOT NULL").fetchall()
    finally:
        con.close()
    best = None
    for name, sla, slo in rows:
        d = haversine_km(lat, lng, sla, slo)
        if best is None or d < best[0]:
            best = (d, name)
    if not best or best[0] > radius_km:
        return None
    return {"name": best[1], "distance_km": round(best[0], 2)}


def _warn_if_snapped(payload: dict, typed: str, resolved: dict) -> None:
    """Say so when the place index interpreted the user's text as something else.

    The local resolver falls back to fuzzy tokens, which is what makes "Hebbal" find
    "Hebbala". For a query that shares almost nothing with the match, that fallback is
    a misroute, not a convenience — and the response has to admit it rather than
    quietly plan a trip to an unrelated suburb.
    """
    if not resolved.get("ok"):
        return
    got = (resolved.get("formatted") or "").strip()
    if not got:
        return
    # A hand-maintained alias ("majestic" -> Kempegowda Bus Station) is the resolver
    # working as designed. Only a weak fuzzy match deserves a warning.
    key = " ".join(typed.lower().strip().split())
    if MANUAL_ALIASES.get(key):
        return
    from difflib import SequenceMatcher
    # Two ways a match can be legitimate: every meaningful word the user typed survives
    # inside the resolved name ("Hebbal" -> "Hebbala", "Jayanagar" -> "Jayanagara Bus
    # Station"), or the strings are near-identical ("Whitefield" -> "White Field").
    # Anything else is a fuzzy coincidence — "Narnia Central" -> "Central",
    # "Atlantis Megacity 9000" -> "Megacity Layout" — and gets flagged.
    n_got = " ".join(got.lower().split())
    words = [w for w in typed.lower().replace(",", " ").split() if len(w) > 3]
    kept = all(w in n_got for w in words) if words else True
    score = SequenceMatcher(None, typed.lower().strip(), n_got).ratio()
    if not kept and score < 0.75:
        payload.setdefault("warnings", []).append(
            f"'{typed}' was not in the place index; it was interpreted as '{got}', the "
            f"closest fuzzy match. Check the destination before travelling.")


def _walk_route(w: dict, arrival_by: str | None) -> dict:
    from src.services.api_shape import _iso, _now
    from datetime import timedelta
    dur = w["total_minutes"]
    if arrival_by:
        hh, mm = (int(x) for x in arrival_by.split(":"))
        target = _now().replace(hour=hh, minute=mm, second=0, microsecond=0)
        depart = target - timedelta(minutes=dur)
        dep_s, arr_s = depart.strftime("%H:%M"), arrival_by
        dep_i, arr_i = _iso(depart), _iso(target)
    else:
        now = _now()
        dep_s, arr_s = now.strftime("%H:%M"), (now + timedelta(minutes=dur)).strftime("%H:%M")
        dep_i, arr_i = _iso(now), _iso(now + timedelta(minutes=dur))
    return {
        "id": "walk-only", "title": "Walking", "transport_modes": ["walking"],
        "fare": {"amount": 0, "currency": "INR", "status": "verified",
                 "source": "no paid leg"},
        "duration_minutes": dur, "distance_km": w["distance_km"],
        "departure_time": dep_s, "arrival_time": arr_s,
        "departure_datetime": dep_i, "arrival_datetime": arr_i,
        "transfers": 0, "walking_minutes": w["walking_minutes"],
        "explanation": w["explanation"],
        "steps": [{"index": 1, "mode": "walk", "from": w["legs"][0]["from"],
                   "to": w["legs"][0]["to"], "duration_minutes": dur,
                   "distance_km": w["distance_km"], "fare": 0}],
        "geometry": w["geometry"], "geometry_kind": "straight_line",
        "traffic": {"status": "not_applicable", "source": None, "observed_at": None,
                    "delay_minutes": None,
                    "reason": "walking is not affected by road traffic in this model"},
        "data_sources": [{"name": "Places index coordinates", "type": "geometry",
                          "status": "derived"}],
        "warnings": ["Distance = straight line × 1.35 circuity; no footpath geometry."],
        "confidence": "estimated",
    }


def _drive_routes(d: dict, arrival_by: str | None) -> list[dict]:
    out = []
    for it in d["itineraries"]:
        out.append({
            "id": it["itinerary_id"],
            "title": "Driving",
            "transport_modes": ["driving"],
            "fare": {"amount": None, "currency": "INR", "status": "unavailable",
                     "source": None,
                     "reason": "Google Routes returns no monetary cost"},
            "duration_minutes": it["total_minutes"],
            "distance_km": it["distance_km"],
            "departure_time": None, "arrival_time": arrival_by,
            "transfers": 0, "walking_minutes": 0,
            "explanation": it["explanation"],
            "steps": [{"index": 1, "mode": "drive", "from": "", "to": "",
                       "duration_minutes": it["total_minutes"],
                       "distance_km": it["distance_km"], "fare": None}],
            "geometry": None, "geometry_kind": "none",
            "encoded_polyline": it["legs"][0].get("encoded_polyline"),
            "traffic": {
                "status": "live",
                "source": "Google Routes API (TRAFFIC_AWARE_OPTIMAL)",
                "observed_at": None,
                "delay_minutes": (it.get("durations") or {}).get("delay_min"),
                "with_traffic_minutes": (it.get("durations") or {}).get("with_traffic_min"),
                "free_flow_minutes": (it.get("durations") or {}).get("free_flow_min"),
            },
            "data_sources": [{"name": "Google Routes API", "type": "route",
                              "status": "live"}],
            "warnings": [], "confidence": "provider",
        })
    return out


@router.get("/api/routes/objectives")
def route_objectives() -> dict:
    from src.routing.search import OBJECTIVE_LABEL, PROFILES
    return {"objectives": [{"key": k, "label": OBJECTIVE_LABEL[k],
                            "weights": PROFILES[k]} for k in
                           ("fastest", "cheapest", "fewest_transfers", "least_walking",
                            "balanced")]}


@router.get("/api/routes/{route_id}")
def route_detail(route_id: str) -> dict:
    """A saved itinerary by id, or the shared itinerary definition + live re-plan."""
    saved = store.get_saved(int(route_id)) if route_id.isdigit() else None
    if saved:
        return {"id": saved["id"], "title": saved["title"], "origin": saved["origin"],
                "destination": saved["destination"], "itinerary": saved["payload"],
                "created_at": saved["created_at"], "source": "saved_route"}
    result = plan(route_id.split("|")[0] or route_id, route_id, max_itineraries=1) \
        if "|" in route_id else None
    if not result or not result.get("ok"):
        raise HTTPException(404, f"no route '{route_id}'")
    return to_contract(result)


@router.post("/api/routes/compare")
def routes_compare(body: PlanRequestBody) -> dict:
    """Every objective for the same pair, plus the algorithm comparison."""
    out = {}
    for pref in ("cheapest", "fastest", "fewest_transfers", "least_walking", "balanced"):
        r = plan(body.origin, body.destination, preference=pref,
                 modes=set(body.transport_modes) & {"bus", "metro"} or {"bus", "metro"})
        if r.get("ok"):
            top = r["itineraries"][0]
            out[pref] = {"route_id": top["itinerary_id"],
                         "fare": top["total_price_inr"],
                         "minutes": top["total_minutes"],
                         "transfers": top["transfers"],
                         "walking_minutes": top["walking_minutes"],
                         "modes": top["mode_sequence"]}
    if not out:
        return {"ok": False, "warnings": ["No comparable routes found."], "comparison": {}}
    return {"ok": True, "comparison": out,
            "note": "Each objective is an independent search with its own cost weights."}


# -------------------------------------------------------------------------- traffic
@router.post("/api/traffic/analyze")
def traffic_analyze(body: PlanRequestBody) -> dict:
    return traffic.panel(body.origin, body.destination)


@router.get("/api/traffic/status")
def traffic_status() -> dict:
    cfg = providers()
    cap = cfg.caps["google_traffic"]
    return {
        "available": cap.available,
        "reason": cap.reason,
        "provider": "google_routes",
        "needs": cap.needs,
        "observed_at": None,
        "note": ("No traffic measurement is cached or synthesised on this endpoint. "
                 "Use POST /api/traffic/analyze for a live corridor reading."),
    }


# -------------------------------------------------------------------------- transit
@router.get("/api/transit/stops/nearby")
def nearby_stops(lat: float = Query(ge=-90, le=90), lng: float = Query(ge=-180, le=180),
                 radius_km: float = Query(default=1.0, ge=0.1, le=10),
                 limit: int = Query(default=10, ge=1, le=50)) -> dict:
    from src.tools.common import haversine_km
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT stop_id, stop_name, CAST(stop_lat AS REAL) lat, CAST(stop_lon AS REAL) lon "
        "FROM stops WHERE CAST(stop_lat AS REAL) BETWEEN ? AND ? "
        "AND CAST(stop_lon AS REAL) BETWEEN ? AND ?",
        (lat - radius_km / 111, lat + radius_km / 111,
         lng - radius_km / 90, lng + radius_km / 90)).fetchall()
    con.close()
    found = []
    for r in rows:
        d = haversine_km(lat, lng, r["lat"], r["lon"])
        if d <= radius_km:
            found.append({"stop_id": r["stop_id"], "name": r["stop_name"],
                          "lat": r["lat"], "lon": r["lon"], "distance_km": round(d, 3),
                          "mode": "bus", "source": "BMTC GTFS"})
    found.sort(key=lambda x: x["distance_km"])
    return {"lat": lat, "lng": lng, "radius_km": radius_km,
            "count": len(found), "stops": found[:limit]}


@router.get("/api/transit/routes")
def transit_routes(q: str = Query(default="", max_length=60),
                   limit: int = Query(default=20, ge=1, le=100)) -> dict:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    if q:
        rows = con.execute(
            "SELECT route_id, route_short_name, route_long_name, agency_id, route_type "
            "FROM routes WHERE route_short_name LIKE ? OR route_long_name LIKE ? LIMIT ?",
            (f"%{q}%", f"%{q}%", limit)).fetchall()
    else:
        rows = con.execute("SELECT route_id, route_short_name, route_long_name, agency_id, "
                           "route_type FROM routes ORDER BY route_short_name LIMIT ?",
                           (limit,)).fetchall()
    con.close()
    return {"count": len(rows), "routes": [dict(r) for r in rows]}


@router.get("/api/transit/stations")
def transit_stations() -> dict:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT stop_id, stop_name, CAST(stop_lat AS REAL) lat, CAST(stop_lon AS REAL) lon, "
        "COALESCE(service_status,'operational') status FROM metro_stops "
        "WHERE location_type='1' ORDER BY stop_name").fetchall()
    lines = con.execute("SELECT * FROM metro_line_status").fetchall()
    con.close()
    return {"count": len(rows), "stations": [dict(r) for r in rows],
            "lines": [dict(r) for r in lines],
            "note": ("Stations with status 'under_construction' are not in service and are "
                     "never returned by the router as a boarding point.")}


@router.get("/api/transit/lines")
def transit_lines() -> dict:
    """The Namma Metro network as lines, with their stations in travel order.

    This is the machine-readable answer to "which lines exist, which are running, and
    where do they go". Purple, Green and Yellow are the operational lines; Pink is
    carried for display only and is flagged `routable: false` everywhere it appears.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    line_rows = con.execute("SELECT * FROM metro_line_status").fetchall()
    station_rows = con.execute(
        "SELECT stop_id, stop_name, CAST(stop_lat AS REAL) lat, "
        "CAST(stop_lon AS REAL) lon, COALESCE(service_status,'operational') status "
        "FROM metro_stops WHERE location_type='1'").fetchall()
    seq_rows = con.execute(
        "SELECT route_id, direction_id, station_id, CAST(seq AS INT) seq "
        "FROM metro_route_stops ORDER BY route_id, direction_id, CAST(seq AS INT)"
    ).fetchall()
    fare_rows = con.execute(
        "SELECT from_station, to_station, fare_inr FROM metro_fare_matrix "
        "WHERE fare_inr IS NOT NULL").fetchall()
    con.close()

    by_id = {r["stop_id"]: dict(r) for r in station_rows}
    # Which lines call at each station: an interchange is a station served by 2+ lines.
    lines_at: dict[str, set[str]] = {}
    for r in seq_rows:
        lines_at.setdefault(r["station_id"], set()).add(r["route_id"])

    fares: dict[str, list[float]] = {}
    for f in fare_rows:
        for station in (f["from_station"], f["to_station"]):
            for line in lines_at.get(station, ()):
                fares.setdefault(line, []).append(f["fare_inr"])

    out = []
    for row in line_rows:
        route_id = row["line_id"]
        forward = [r for r in seq_rows
                   if r["route_id"] == route_id and str(r["direction_id"]) == "0"]
        if not forward:                       # some feeds only publish direction 1
            forward = [r for r in seq_rows if r["route_id"] == route_id]
        stations = []
        for seq_row in forward:
            st = by_id.get(seq_row["station_id"])
            if not st:
                continue
            on = sorted(lines_at.get(st["stop_id"], ()))
            stations.append({
                "seq": seq_row["seq"], "station_id": st["stop_id"], "name": st["stop_name"],
                "lat": st["lat"], "lon": st["lon"], "status": st["status"],
                "lines": on,
                "interchange": len([l for l in on if l != route_id]) > 0,
            })
        operational = str(row["status"]) != "under_construction"
        f = fares.get(route_id) or []
        out.append({
            "line_id": route_id,
            "name": row["line_name"],
            "colour": f"#{row['colour']}" if row["colour"] else None,
            "status": row["status"],
            "routable": operational,
            "length_km": row["length_km"],
            "stations_declared": row["stations"],
            "station_count": len(stations),
            "terminals": [stations[0]["name"], stations[-1]["name"]] if stations else [],
            "interchanges": [st["name"] for st in stations if st["interchange"]],
            "fare_range_inr": {"min": min(f), "max": max(f)} if f else None,
            "verification": row["verification_status"],
            "source_url": row["source_url"],
            "last_verified": row["last_verified"],
            "note": row["note"],
            "stations": stations,
        })

    order = {"PURPLE": 0, "GREEN": 1, "YELLOW": 2, "PINK": 3}
    out.sort(key=lambda l: order.get(l["line_id"], 9))
    return {
        "count": len(out),
        # Names, not feed ids: the ids are upper-case and the UI shows the display name.
        "operational": [l["name"] for l in out if l["routable"]],
        "under_construction": [l["name"] for l in out if l["status"] == "under_construction"],
        "lines": out,
        "note": ("Purple, Green and Yellow are operational and are used by the router. "
                 "Pink is under construction: its stations are listed for reference and "
                 "it is never offered as a boarding option. Fares come from the BMRCL "
                 "published matrix, not from any external API."),
    }


@router.get("/api/transit/ksrtc-routes")
def ksrtc_routes() -> dict:
    """KSRTC corridors this build holds prices for — and how far to trust each one.

    KSRTC is deliberately *not* in the routing graph: there is no stop-level GTFS feed
    for it in this repo, only a handful of published corridor fares. Quoting a corridor
    is honest; drawing a fake route across 65 km would not be.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM ksrtc_corridor ORDER BY corridor_id").fetchall()
    con.close()
    corridors = []
    for r in rows:
        d = dict(r)
        verified = d["verification_status"] in ("VERIFIED_PRIMARY", "VERIFIED_SECONDARY")
        d["usable_as_fare_quote"] = bool(verified and d["fare_inr"] is not None)
        if d["fare_inr"] is None:
            d["fare_note"] = ("No published fare was reachable for this service class, so "
                              "none is shown. Ask at the depot or use the KSRTC app.")
        elif d["verification_status"] == "UNVERIFIED_DERIVED":
            d["fare_note"] = ("Derived by scaling a published fare to this distance, not "
                              "quoted from a KSRTC source. Treat as an estimate.")
        else:
            d["fare_note"] = f"Published fare ({d['fare_basis']}), {d['publisher']}."
        d["routable"] = False
        # Same shape as the intercity block inside a plan, so one client parser covers
        # both: an amount, its currency, whether it can be quoted at all, and why not.
        if d["fare_inr"] is None:
            status, amount = "unavailable", None
        elif d["usable_as_fare_quote"]:
            status, amount = "verified", d["fare_inr"]
        else:
            status, amount = "estimated", None
        d["fare"] = {
            "amount": amount,
            "estimated_amount": d["fare_inr"],
            "currency": "INR",
            "status": status,
            "source": d["publisher"],
            "reason": d["fare_note"],
        }
        corridors.append(d)
    return {
        "count": len(corridors),
        "corridors": corridors,
        "in_routing_graph": False,
        "note": ("KSRTC runs outside the BMTC/Namma Metro network this router searches. "
                 "The corridors below carry a published or derived fare only; no KSRTC "
                 "timetable, stop sequence or live vehicle data is present, so no KSRTC "
                 "itinerary is ever synthesised."),
    }


# --------------------------------------------------------------- saved + history
@router.get("/api/saved-routes")
def saved_routes() -> dict:
    rows = store.list_saved()
    return {"count": len(rows), "saved_routes": rows}


@router.delete("/api/saved-routes/{save_id}")
def delete_saved_route(save_id: int) -> dict:
    if not store.delete_saved(save_id):
        raise HTTPException(404, f"saved route {save_id} not found")
    return {"deleted": save_id}


@router.post("/api/saved-routes", status_code=201)
def create_saved_route(body: SaveRouteRequest) -> dict:
    """Same store as POST /api/v1/saved-routes, published under the brief §10 path."""
    return store.save_route(origin=body.origin, destination=body.destination,
                            route_id=body.route_id, title=body.title,
                            payload=body.itinerary)


@router.get("/api/travel-history")
def travel_history(limit: int = Query(default=50, ge=1, le=200),
                   offset: int = Query(default=0, ge=0)) -> dict:
    return {"count": store.history_stats()["trips_logged"],
            "travel_history": store.list_history(limit=limit, offset=offset),
            "stats": store.history_stats()}


@router.post("/api/travel-history", status_code=201)
def add_travel_history(body: HistoryRequest) -> dict:
    return store.add_history(origin=body.origin, destination=body.destination,
                             departure_time=body.departure_time,
                             selected_mode=body.selected_mode,
                             fare=body.estimated_fare_inr,
                             duration=body.estimated_duration,
                             route_id=body.route_id)


@router.delete("/api/travel-history")
def clear_travel_history() -> dict:
    return {"deleted": store.clear_history()}
