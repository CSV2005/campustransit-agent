"""Walking-only and driving trips.

Neither of these is a transit ride, so neither belongs in the corridor graph. Both are
built here, and both are explicit about how confident they are.

**Walking** — there are no footpath geometries in this repo, so a walk is computed from
two real coordinates plus one documented assumption: a street-network circuity factor of
1.35 over the straight-line distance. The result is labelled ``estimated`` with the
assumption stated in the response, never presented as a measured route.

**Driving** — the road-routing chain in :mod:`src.services.road_routing` (Google Routes,
OpenRouteService or TomTom, whichever is configured and healthy). Each result names its
provider and says whether that provider supplied live traffic. With nothing configured the
answer is ``unavailable``; it does not approximate a car trip from straight-line distance.
"""
from __future__ import annotations

from typing import Any

from src.routing.graph import haversine_km
from src.services import road_routing

# Standard planning value for how much further you walk than the straight line between
# two points. 1.35 is the conventional range (1.3-1.4) for a grid-ish street network.
WALK_CIRCUITY = 1.35
WALK_KMH = 4.8

def walk_trip(origin: dict, destination: dict) -> dict[str, Any]:
    """A walking-only itinerary between two resolved places."""
    straight = haversine_km(origin["lat"], origin["lon"], destination["lat"], destination["lon"])
    route_km = straight * WALK_CIRCUITY
    minutes = route_km / WALK_KMH * 60.0
    return {
        "itinerary_id": "walk-only",
        "objective": "walking",
        "mode_sequence": ["walk"],
        "total_price_inr": 0.0,
        "minutes_in_vehicle": 0.0,
        "total_minutes": round(minutes, 1),
        "walking_minutes": round(minutes, 1),
        "transfers": 0,
        "rank": 1,
        "legs": [{
            "mode": "walk", "label": "Walk",
            "from": origin.get("name"), "to": destination.get("name"),
            "minutes": round(minutes, 1), "km": round(route_km, 2),
            "fare_inr": 0, "fare_basis": "walking", "stops": None,
            # Two real points joined by a straight line. Calling this "route geometry"
            # would be a lie; the response flags it.
            "geometry": [[origin["lat"], origin["lon"]],
                         [destination["lat"], destination["lon"]]],
            "geometry_kind": "straight_line",
        }],
        "geometry": [[origin["lat"], origin["lon"]], [destination["lat"], destination["lon"]]],
        "fare_breakdown": [],
        "ranked_best_for": ["cheapest", "fewest_transfers"],
        "explanation": (f"{route_km:.1f} km on foot, about {minutes:.0f} min. No fare. "
                        f"Distance is the straight line ({straight:.2f} km) times a "
                        f"{WALK_CIRCUITY} street-circuity factor — there are no footpath "
                        f"geometries in this dataset."),
        "confidence": "estimated",
        "distance_km": round(route_km, 2),
        "straight_line_km": round(straight, 2),
    }


def drive_trip(origin: dict, destination: dict, *, departure_time: str = "",
               traffic_aware: bool = True) -> dict[str, Any]:
    """A driving trip from the configured road-routing chain, or an honest 'unavailable'.

    The provider is chosen in :mod:`src.services.road_routing` from configuration, not here.
    Whatever answers is named in ``provider``/``provider_label``, and the itinerary keeps
    that provider's own quality flags: an OpenRouteService duration is never described as
    traffic-aware, and a straight line is never described as a road route.
    """
    plan = road_routing.plan((origin["lat"], origin["lon"]),
                             (destination["lat"], destination["lon"]),
                             departure_time=departure_time,
                             want_traffic=traffic_aware,
                             alternatives=True)
    if not plan.get("ok"):
        return {
            "ok": False, "available": False, "mode": "driving",
            "provider": None,
            "reason": plan.get("reason") or "No road-routing provider is configured.",
            "needs": ", ".join(plan.get("needs") or []),
            "docs": plan.get("docs", ""),
            "attempts": plan.get("attempts") or [],
            "skipped": plan.get("skipped") or [],
        }

    pid = plan["provider"]
    label = plan["provider_label"]
    out = []
    for i, r in enumerate(plan["routes"][:3]):
        live = r.get("duration_min")
        static = r.get("duration_free_flow_min")
        km = r.get("distance_km")
        out.append({
            "itinerary_id": f"drive-{i + 1}",
            "objective": "driving",
            "mode_sequence": ["drive"],
            "total_price_inr": None,          # no provider prices fuel or tolls here
            "total_minutes": live,
            "minutes_in_vehicle": live,
            "walking_minutes": 0.0,
            "transfers": 0,
            "distance_km": km,
            "provider": pid,
            "provider_label": label,
            "traffic_aware": bool(r.get("traffic_aware")),
            "legs": [{
                "mode": "drive", "label": "Drive",
                "from": origin.get("name"), "to": destination.get("name"),
                "minutes": live, "km": km,
                "fare_inr": None, "fare_basis": "not_supplied_by_provider",
                # Real provider geometry when there is one; the straight-line case says so.
                "geometry": r.get("geometry") or [],
                "geometry_kind": r.get("geometry_kind", "polyline"),
                "provider": label,
            }],
            "durations": {"with_traffic_min": live if r.get("traffic_aware") else None,
                          "free_flow_min": static,
                          "delay_min": r.get("delay_min"),
                          "provider_reported": live,
                          "traffic_aware": bool(r.get("traffic_aware"))},
            "fare_breakdown": [{
                "mode": "drive", "service": "car",
                "fare_inr": None, "basis": "not_supplied_by_provider",
                "basis_label": (f"{label} returns distance and duration, not a cost. Fuel, "
                                "tolls and parking are not modelled."),
            }],
            "confidence": "estimated" if plan.get("is_estimate") else "provider",
            "explanation": (f"{km} km by road, {live} min"
                            + (" with traffic" if r.get("traffic_aware") else
                               " (no live traffic from this provider)")
                            + f" — {label}."),
        })
    return {
        "ok": True, "available": True, "mode": "driving",
        "provider": pid, "provider_label": label,
        "traffic_aware": plan.get("traffic_aware"),
        "itineraries": out,
        "attempts": plan.get("attempts") or [],
        "attribution": plan.get("attribution"),
        "attribution_url": plan.get("attribution_url"),
        "is_estimate": bool(plan.get("is_estimate")),
        "note": plan.get("note") or "",
    }


def _secs(dur: str | None) -> float:
    if not dur:
        return 0.0
    try:
        return float(str(dur).rstrip("s"))
    except ValueError:
        return 0.0
