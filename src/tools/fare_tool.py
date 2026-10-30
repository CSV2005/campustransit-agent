"""Deterministic fare logic. The LLM never does this arithmetic.

Fare resolution order:
  1. GTFS fare-stage pair for the specific route          (highest confidence)
  2. GTFS fare-stage pair for any route on that pair
  3. The published BMTC distance slab (Jan 2025)          (estimate, clearly labelled)

The published slab is used because the GTFS fare tables only cover a subset of
origin/destination fare-stage pairs; when a pair exists the feed value wins.
"""
from __future__ import annotations

from typing import Any

from src.tools.common import db, haversine_km, resolve_place, route_info

# BMTC ordinary fare slabs after the 5 Jan 2025 revision (Deccan Herald, 4 Jan 2025).
BMTC_SLABS = [(2, 6), (4, 12), (6, 18), (10, 23), (14, 24), (18, 28), (40, 30), (10**9, 32)]
BMTC_SLAB_SOURCE = "https://www.deccanherald.com/india/karnataka/ksrtc-issues-an-order-detailing-revised-fares-for-various-services-3341773"
ROAD_FACTOR = 1.35  # straight-line -> road distance on Bengaluru's street grid


def bmtc_slab_fare(km: float) -> int:
    for limit, fare in BMTC_SLABS:
        if km <= limit:
            return fare
    return 32


def estimate_fare(origin: str, destination: str, route_id: str | None = None,
                  mode: str = "bus") -> dict[str, Any]:
    """Estimate the single-ride fare between two places, with an explicit basis field.

    mode='bus' or 'metro'. route_id is optional; passing it enables the route-specific
    GTFS fare-stage lookup.
    """
    o = resolve_place(origin)
    d = resolve_place(destination)
    if not o.get("ok") or not d.get("ok"):
        return {"ok": False, "tool": "estimate_fare",
                "summary": f"Could not resolve '{origin}' or '{destination}'."}

    straight = haversine_km(o["lat"], o["lon"], d["lat"], d["lon"])
    road_km = round(straight * ROAD_FACTOR, 1)
    con = db()

    if mode == "metro":
        row = con.execute(
            "SELECT fare_inr, fare_max_inr, fare_source FROM metro_fare_matrix "
            "WHERE from_station=? AND to_station=?",
            (o.get("stop_id"), d.get("stop_id"))).fetchone()
        pending = con.execute(
            "SELECT COUNT(*) FROM metro_stops WHERE stop_id IN (?, ?) "
            "AND COALESCE(service_status,'operational')<>'operational'",
            (o.get("stop_id"), d.get("stop_id"))).fetchone()[0]
        con.close()
        if row:
            derived = (row["fare_source"] or "published_matrix") != "published_matrix"
            lo, hi = row["fare_inr"], row["fare_max_inr"]
            price = f"Rs {lo:.0f}" if round(lo) == round(hi) else f"Rs {lo:.0f}-{hi:.0f}"
            msg = f"Metro fare {origin} -> {destination}: {price}"
            msg += (" (derived from the published hop-count fare table; this station is "
                    "not yet in service)" if derived else " (published BMRCL fare matrix)")
            return {"ok": True, "tool": "estimate_fare", "mode": "metro",
                    "summary": msg + ".",
                    "data": {"fare_inr": lo, "fare_max_inr": hi,
                             "basis": "derived_hop_law" if derived else "bmrcl_gtfs_fare_matrix",
                             "fare_source": row["fare_source"] or "published_matrix",
                             "service_pending": bool(pending),
                             "confidence": 0.70 if derived else 0.95,
                             "distance_km": road_km},
                    "sources": (["BMRCL published fare matrix (hop-count law); Pink Line "
                                 "stations from curated Reach 6 data"] if derived
                                else ["BMRCL GTFS fare_rules"])}
        con.close()
        return {"ok": False, "tool": "estimate_fare",
                "summary": "No metro fare found for that station pair."}

    from_zone, to_zone = o.get("zone_id"), d.get("zone_id")
    fare = None
    basis = None
    confidence = None
    if from_zone and to_zone:
        if route_id:
            row = con.execute(
                "SELECT fare_inr, fare_max_inr FROM fare_matrix "
                "WHERE route_id=? AND from_zone=? AND to_zone=?", (route_id, from_zone, to_zone)).fetchone()
            if row:
                fare, basis, confidence = row["fare_inr"], "gtfs_route_fare_stage", 0.95
        if fare is None:
            row = con.execute(
                "SELECT fare_inr, fare_max_inr FROM fare_matrix "
                "WHERE route_id IS NULL AND from_zone=? AND to_zone=? "
                "ORDER BY fare_inr LIMIT 1", (from_zone, to_zone)).fetchone()
            if row:
                fare, basis, confidence = row["fare_inr"], "gtfs_zone_pair", 0.8
    con.close()

    if fare is not None:
        info = route_info(route_id) if route_id else None
        if info and info.get("is_ac"):
            note = "AC (Vajra) route; the GTFS fare stage applies."
        else:
            note = "Fare taken from BMTC's own GTFS fare-stage table."
        return {"ok": True, "tool": "estimate_fare", "mode": "bus",
                "summary": f"Bus fare {origin} -> {destination}: Rs {fare:,.0f} ({basis}).",
                "data": {"fare_inr": fare, "basis": basis, "confidence": confidence,
                         "from_zone": from_zone, "to_zone": to_zone,
                         "distance_km": road_km, "note": note},
                "sources": ["BMTC GTFS fare_attributes / fare_rules"]}

    fare = bmtc_slab_fare(road_km)
    return {"ok": True, "tool": "estimate_fare", "mode": "bus",
            "summary": (f"Bus fare {origin} -> {destination}: about Rs {fare} "
                        f"(published BMTC slab for ~{road_km:.0f} km, ordinary non-AC)."),
            "data": {"fare_inr": fare, "basis": "published_slab_estimate", "confidence": 0.6,
                     "distance_km": road_km, "from_zone": from_zone, "to_zone": to_zone,
                     "note": ("No GTFS fare-stage pair exists for this origin/destination, so the "
                              "fare is read off the published BMTC slab (Jan 2025). AC services cost more.")},
            "sources": ["BMTC published fare slab, Jan 2025", BMTC_SLAB_SOURCE]}


def estimate_monthly_cost(one_way_fare_inr: float, legs_per_trip: int = 1,
                          trips_per_day: int = 2, travel_days_per_month: int = 24) -> dict[str, Any]:
    """Monthly pay-as-you-go spend, computed in Python (never by the model)."""
    rides = legs_per_trip * trips_per_day * travel_days_per_month
    total = rides * float(one_way_fare_inr)
    return {
        "ok": True, "tool": "estimate_monthly_cost",
        "summary": (f"{rides} rides/month x Rs {one_way_fare_inr:,.0f} = "
                    f"Rs {total:,.0f} per month pay-as-you-go."),
        "data": {
            "one_way_fare_inr": float(one_way_fare_inr),
            "legs_per_trip": legs_per_trip,
            "trips_per_day": trips_per_day,
            "travel_days_per_month": travel_days_per_month,
            "rides_per_month": rides,
            "payg_monthly_inr": round(total, 2),
            "per_day_inr": round(legs_per_trip * trips_per_day * float(one_way_fare_inr), 2),
        },
        "sources": ["arithmetic"],
    }
