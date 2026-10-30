"""Traffic panel data.

There are exactly two things this module will report:

1. **Live traffic**, and only from a provider that actually returned it. Today that means
   Google Routes with ``routingPreference=TRAFFIC_AWARE``, or TomTom routing with
   ``traffic=true``. OpenRouteService can never supply it (static network). With neither
   configured the answer is ``available: false`` and the reason — never an estimate dressed
   up as a measurement.
   The GTFS import in this repo carries no departure times, so a "congestion by hour"
   series cannot be derived from it either; inventing one would be the exact failure the
   brief forbids.

2. **Scheduled network supply**, which is real: how many trips traverse each segment of an
   itinerary, and the speed implied by the scheduled minutes over that segment's length.
   It is labelled ``scheduled`` everywhere it appears, because a slow scheduled speed can
   mean traffic *or* an old timetable, and this data cannot tell the two apart.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from src.config import DB_PATH
from src.services import http
from src.services.providers import providers

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
TRAFFIC_FIELDS = ("routes.duration,routes.staticDuration,routes.distanceMeters,"
                  "routes.polyline.encodedPolyline,routes.travelAdvisory,"
                  "routes.legs.steps.navigationInstruction,routes.description")


def road_traffic(origin: tuple[float, float], destination: tuple[float, float],
                 *, departure_time: str = "", traffic_model: str = "BEST_GUESS") -> dict:
    """Road congestion between two points, from Google Routes. Honest when absent."""
    cfg = providers()
    if not cfg.google_server:
        # Google is the first choice because it is the only provider here that returns a
        # per-segment speed profile. TomTom can still answer; ORS cannot answer at all,
        # because it routes on a static network and has no live traffic.
        from src.services import road_routing
        tt = road_routing.tomtom_traffic(origin, destination, departure_time=departure_time)
        if tt.get("available"):
            return tt
        return {
            "available": False,
            "provider": None,
            "providers_checked": ["google_routes", "tomtom"],
            "reason": ("No traffic-capable provider is configured: GOOGLE_MAPS_SERVER_API_KEY "
                       "is not set and TOMTOM_API_KEY is not set (or the call failed). "
                       "Nothing on this panel is estimated."),
            "needs": ("GOOGLE_MAPS_SERVER_API_KEY (Routes API) or TOMTOM_API_KEY"),
            "docs": "https://developers.google.com/maps/documentation/routes",
            "traffic_model_requested": traffic_model,
        }

    body: dict[str, Any] = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": destination[0],
                                                "longitude": destination[1]}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "extraComputations": ["TRAFFIC_ON_POLYLINE"],
        "units": "METRIC",
    }
    if departure_time:
        body["departureTime"] = departure_time

    res = http.post_json(ROUTES_URL, body, provider="google_routes",
                         endpoint="computeRoutes_traffic",
                         headers={"X-Goog-FieldMask": TRAFFIC_FIELDS}, ttl=120)
    if not res["ok"]:
        return {"available": False, "provider": "google_routes",
                "reason": f"Routes API call failed: {res['error']}",
                "status_code": res.get("status")}

    routes = (res["data"] or {}).get("routes") or []
    if not routes:
        return {"available": False, "provider": "google_routes",
                "reason": "Routes API returned no route for those coordinates."}

    r0 = routes[0]
    live_s = _seconds(r0.get("duration"))
    static_s = _seconds(r0.get("staticDuration"))
    metres = r0.get("distanceMeters")

    # TRAFFIC_ON_POLYLINE attaches speed intervals to the route polyline; keep whatever
    # the API actually returned rather than synthesising a profile.
    intervals = []
    pd = r0.get("polylineDetails") or {}
    for si in pd.get("speedIntervals") or []:
        intervals.append({
            "speed": si.get("speed"),
            "start_index": si.get("startPolylinePointIndex"),
            "end_index": si.get("endPolylinePointIndex"),
        })

    ratio = (live_s / static_s) if (live_s and static_s) else None
    return {
        "available": True,
        "provider": "google_routes",
        "measured_at": _utc_now(),
        "distance_km": round(metres / 1000.0, 2) if metres else None,
        "duration_with_traffic_min": round(live_s / 60.0, 1) if live_s else None,
        "duration_free_flow_min": round(static_s / 60.0, 1) if static_s else None,
        "delay_minutes": round((live_s - static_s) / 60.0, 1) if (live_s and static_s) else None,
        "congestion_ratio": round(ratio, 2) if ratio else None,
        "level": _level(ratio),
        "speed_intervals": intervals[:40],
        "traffic_model": traffic_model,
        "departure_time": departure_time or "now",
        "latency_ms": res.get("latency_ms"),
        "cached": res.get("cached", False),
        "note": ("duration / staticDuration are Google's own figures for this request; the "
                 "congestion ratio is live ÷ free-flow and is the only derived number here."),
    }


def _level(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if ratio < 1.15:
        return "free flowing"
    if ratio < 1.40:
        return "moderate"
    if ratio < 1.80:
        return "heavy"
    return "severe"


def _seconds(dur: str | None) -> float:
    """Routes API returns durations like "1234s"."""
    if not dur:
        return 0.0
    try:
        return float(str(dur).rstrip("s"))
    except ValueError:
        return 0.0


def _utc_now() -> str:
    from src.db.extra_schema import now_iso
    return now_iso()


def network_supply(origin: str, destination: str) -> dict:
    """Scheduled service supply along the segments of the best itinerary.

    Real GTFS numbers: ``n_trips`` is how many scheduled trips use that segment, and the
    implied speed is segment length ÷ scheduled minutes. Explicitly *not* live traffic.
    """
    from src.services.planner import plan
    from src.tools.common import resolve_place

    p = plan(origin, destination, max_itineraries=1)
    if not p.get("ok"):
        return {"available": False, "reason": "No itinerary, so no segments to report.",
                "kind": "scheduled_network_supply"}

    it = p["itineraries"][0]
    segments = []
    for leg in it["legs"]:
        if leg["mode"] not in ("bus", "metro"):
            continue
        segments.extend(_segments_for(leg, it))

    n = len(segments)
    trips = [s["n_trips"] for s in segments if s["n_trips"]]
    speeds = [s["implied_speed_kmh"] for s in segments if s["implied_speed_kmh"]]
    return {
        "available": True,
        "kind": "scheduled_network_supply",
        "itinerary_id": it["itinerary_id"],
        "route": f"{p['origin']['name']} → {p['destination']['name']}",
        "sample_segments": segments[:40],
        "summary": {
            "samples": n,
            "median_trips_per_segment": _median(trips),
            "median_implied_speed_kmh": _median(speeds),
            "slowest_segment_speed_kmh": min(speeds) if speeds else None,
        },
        "note": (f"Derived from the GTFS n_trips counts and scheduled segment minutes in "
                 f"this database. A low implied speed can be congestion or a dated "
                 f"timetable — this data cannot distinguish them, and it is not live traffic."),
    }


def _segments_for(leg: dict, it: dict) -> list[dict]:
    """Look up the real route_segment rows between the leg's endpoints."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    label = leg["label"]
    SQL = ("SELECT rs.from_stop, rs.to_stop, rs.minutes, rs.n_trips, rs.km, "
           "       sa.stop_name AS from_name, sb.stop_name AS to_name "
           "FROM route_segment rs JOIN routes r ON r.route_id = rs.route_id "
           "LEFT JOIN stops sa ON sa.stop_id = rs.from_stop "
           "LEFT JOIN stops sb ON sb.stop_id = rs.to_stop "
           "WHERE {cond} AND rs.minutes > 0 AND rs.km > 0 "
           "ORDER BY rs.n_trips DESC LIMIT 12")
    rows = con.execute(SQL.format(cond="r.route_short_name = ?"), (label,)).fetchall()
    if not rows:
        for cand in (label.replace("-", " "), "%" + label.replace("-", " ") + "%"):
            rows = con.execute(SQL.format(cond="r.route_long_name LIKE ?"),
                               (cand,)).fetchall()
            if rows:
                break
    con.close()
    out = []
    for r in rows:
        km = r["km"] or 0
        mins = r["minutes"] or 0
        out.append({
            "service": label,
            "from": r["from_name"] or r["from_stop"],
            "to": r["to_name"] or r["to_stop"],
            "scheduled_minutes": round(mins, 1),
            "km": round(km, 2),
            "n_trips": r["n_trips"],
            "implied_speed_kmh": round(km / (mins / 60.0), 1) if mins and km else None,
        })
    return out


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    m = len(s) // 2
    return round(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2, 1)


def panel(origin: str = "", destination: str = "") -> dict:
    """Everything the traffic panel needs in one response, each part self-labelled."""
    out = {
        "live_traffic": {
            "available": False, "provider": "google_routes",
            "reason": "No origin/destination supplied.",
        },
        "network_supply": {"available": False, "reason": "No origin/destination supplied."},
        "sources": providers().capabilities(),
    }
    if not origin or not destination:
        return out

    from src.tools.common import resolve_place
    o, d = resolve_place(origin), resolve_place(destination)
    if o.get("ok") and d.get("ok"):
        out["live_traffic"] = road_traffic((o["lat"], o["lon"]), (d["lat"], d["lon"]))
        out["network_supply"] = network_supply(origin, destination)
        out["route"] = {"origin": o.get("name"), "destination": d.get("name")}
    else:
        reason = "Could not resolve " + ("origin" if not o.get("ok") else "destination")
        out["live_traffic"] = {"available": False, "reason": reason}
        out["network_supply"] = {"available": False, "reason": reason}
    return out
