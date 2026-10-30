"""Adapter from the internal planner result to the response contract in the brief (§11).

The internal shape is richer than the contract and is kept as-is at
``/api/v1/routes/plan``. This module produces the documented shape for
``POST /api/routes/plan`` so both exist: one for the UI, one for anything that expects
the published contract.

Every field is either a real value or explicitly null with a status. Nothing is
default-filled to look complete.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from src.services.planner import TIMEZONE, _now
from src.tools.common import METRO_LINE_COLOURS

MODE_LABEL = {"bus": "BMTC Bus", "metro": "Namma Metro", "walk": "Walking",
              "drive": "Driving"}

# Where each part of an itinerary came from. Named rather than implied, because the UI
# shows these as chips on every card.
SOURCES = {
    "bus": {"name": "BMTC GTFS (static)", "type": "schedule", "status": "verified"},
    "metro": {"name": "BMRCL GTFS (static)", "type": "schedule", "status": "verified"},
    "walk": {"name": "GTFS stop coordinates", "type": "geometry", "status": "derived"},
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def _clock(minutes_from_now: float) -> str:
    return (_now() + timedelta(minutes=minutes_from_now)).strftime("%H:%M")


def _title(itin: dict) -> str:
    seq = [MODE_LABEL.get(m, m) for m in itin.get("mode_sequence", [])]
    if not seq:
        return "Journey"
    return " + ".join(dict.fromkeys(seq)) if len(set(seq)) < len(seq) else " + ".join(seq)


# Official Namma Metro line colours live in src/tools/common.py so the planner and the
# HTTP contract cannot drift; imported up here with the rest rather than mid-module.


def _steps(itin: dict) -> list[dict]:
    steps = []
    for i, leg in enumerate(itin.get("legs", []), 1):
        label = leg.get("label")
        step = {
            "index": i,
            "mode": leg.get("mode"),
            "service": label,
            "from": leg.get("from"),
            "to": leg.get("to"),
            "duration_minutes": leg.get("minutes"),
            "distance_km": leg.get("km"),
            "fare": leg.get("fare_inr"),
            "geometry_points": len(leg.get("geometry") or []),
        }
        if leg.get("operator"):
            step["operator"] = leg["operator"]
        if leg.get("mode") == "metro" and label in METRO_LINE_COLOURS:
            step["line"] = label
            step["line_colour"] = METRO_LINE_COLOURS[label]
        steps.append(step)
    return steps


def _fare(itin: dict) -> dict:
    amount = itin.get("total_price_inr")
    if amount is None:
        unpriced = itin.get("unpriced_legs") or []
        if unpriced:
            names = ", ".join(u.get("label") or "?" for u in unpriced)
            return {
                "amount": None, "currency": "INR", "status": "unavailable",
                "source": "KSRTC (no fare table in this build)",
                "reason": (f"This trip uses {names}, a KSRTC service. KSRTC prices by a "
                           f"state-wide distance table that is not in this build, so no "
                           f"total is shown rather than a BMTC city-bus estimate."),
                "unpriced_legs": unpriced,
                "priced_subtotal": itin.get("priced_subtotal_inr"),
            }
        return {"amount": None, "currency": "INR", "status": "unavailable",
                "source": None, "reason": "the provider returned no price for this mode"}
    bases = {b.get("basis") for b in itin.get("fare_breakdown", [])}
    if "bmrcl_official_chart" in bases:
        status, source = "verified", "BMRCL Revised Fare Chart (14.02.2025)"
    elif bases & {"bmtc_distance_slab", "bmtc_fare_stage", "gtfs_fare_stage"}:
        status = "estimated"
        source = "BMTC distance slab (Jan 2025 revision)"
    elif amount == 0:
        status, source = "verified", "no paid leg"
    else:
        status, source = "estimated", "composite"
    return {"amount": round(float(amount), 2), "currency": "INR",
            "status": status, "source": source,
            "breakdown": itin.get("fare_breakdown", [])}


def _traffic(itin: dict) -> dict:
    d = itin.get("durations") or {}
    if itin.get("mode_sequence") == ["drive"] and d.get("with_traffic_min"):
        return {
            "status": "live",
            "source": "Google Routes API (TRAFFIC_AWARE_OPTIMAL)",
            "observed_at": _iso(_now()),
            "with_traffic_minutes": d.get("with_traffic_min"),
            "free_flow_minutes": d.get("free_flow_min"),
            "delay_minutes": d.get("delay_min"),
        }
    return {
        "status": "unavailable",
        "source": None,
        "observed_at": None,
        "delay_minutes": None,
        "reason": ("No traffic provider is attached to bus/metro itineraries. GTFS "
                   "scheduled minutes are used as-is and are not adjusted for traffic."),
    }


def _sources(itin: dict) -> list[dict]:
    seen, out = set(), []
    for leg in itin.get("legs", []):
        mode = leg.get("mode")
        if mode in SOURCES and mode not in seen:
            seen.add(mode)
            out.append(SOURCES[mode])
    return out


def to_contract(plan_result: dict, *, arrival_by: str | None = None) -> dict[str, Any]:
    """Map ``planner.plan()`` output onto the published response contract."""
    origin = plan_result.get("origin") or {}
    destination = plan_result.get("destination") or {}
    its = plan_result.get("itineraries") or []

    routes = []
    for it in its:
        dur = it.get("total_minutes")
        if arrival_by and dur:
            hh, mm = (int(x) for x in arrival_by.split(":"))
            target = _now().replace(hour=hh, minute=mm, second=0, microsecond=0)
            depart = target - timedelta(minutes=dur + 5)
            dep_s, arr_s = depart.strftime("%H:%M"), arrival_by
            dep_iso, arr_iso = _iso(depart), _iso(target)
        elif dur:
            depart = _now()
            arr = depart + timedelta(minutes=dur)
            dep_s, arr_s = depart.strftime("%H:%M"), arr.strftime("%H:%M")
            dep_iso, arr_iso = _iso(depart), _iso(arr)
        else:
            dep_s = arr_s = dep_iso = arr_iso = None

        km = it.get("distance_km")
        if km is None:
            km = round(sum((l.get("km") or 0) for l in it.get("legs", [])), 2)

        routes.append({
            "id": it.get("itinerary_id"),
            "title": _title(it),
            "transport_modes": it.get("mode_sequence", []),
            "fare": _fare(it),
            "duration_minutes": dur,
            "distance_km": km,
            "departure_time": dep_s,
            "arrival_time": arr_s,
            "departure_datetime": dep_iso,
            "arrival_datetime": arr_iso,
            "transfers": it.get("transfers"),
            "walking_minutes": it.get("walking_minutes"),
            "comfort_score": it.get("comfort_score"),
            "ranked_best_for": it.get("ranked_best_for", []),
            "explanation": it.get("explanation"),
            "steps": _steps(it),
            "geometry": it.get("geometry"),
            "geometry_kind": ("stop_sequence" if it.get("mode_sequence") != ["walk"]
                              else "straight_line"),
            "traffic": _traffic(it),
            "data_sources": _sources(it),
            "warnings": [],
            "confidence": it.get("confidence", "verified"),
        })

    warnings = list(plan_result.get("warnings") or [])
    if not routes and not warnings:
        warnings = ["No verified route found between those places."]

    return {
        "origin": {"name": origin.get("name"),
                   "latitude": origin.get("lat"),
                   "longitude": origin.get("lon")},
        "destination": {"name": destination.get("name"),
                        "latitude": destination.get("lat"),
                        "longitude": destination.get("lon")},
        "routes": routes,
        "ok": bool(routes),
        "preference": plan_result.get("preference"),
        "objectives": plan_result.get("objectives"),
        "best_of": plan_result.get("best_of"),
        "departure_advice": plan_result.get("departure_advice"),
        "fare_source": plan_result.get("fare_source"),
        "scoring_note": plan_result.get("scoring_note"),
        "warnings": warnings,
        "timezone": TIMEZONE,
        "generated_at": _iso(_now()),
        "elapsed_ms": plan_result.get("elapsed_ms"),
    }
