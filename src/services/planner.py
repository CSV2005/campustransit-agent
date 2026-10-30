"""Route planning facade: one call in, a complete, explainable answer out.

Everything here is derived from data that already exists:
  * geometry, stop sequences, ride minutes  -> GTFS via ``src/routing``
  * fares                                   -> SQLite slabs/charts via ``src/routing/fares``
  * walking times                           -> the same graph's walk edges

Nothing is interpolated to look plausible. If the planner finds no path, the response says
so and lists the reason.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any

from src.config import DB_PATH

# The server may run in UTC while the traveller is in Bengaluru. "Leave now" has to mean
# the traveller's clock, so the zone is explicit and configurable rather than inherited
# from whatever the container happens to be set to.
TIMEZONE = os.getenv("TRANSIT_TIMEZONE", "Asia/Kolkata")


def _now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TIMEZONE))
    except Exception:                                   # no tzdata in this image
        return datetime.utcnow() + timedelta(hours=5, minutes=30)

from src.routing.search import OBJECTIVE_LABEL
from src.tools.plan_tool import ranked_itineraries

ALL_OBJECTIVES = ("fastest", "cheapest", "fewest_transfers", "least_walking", "balanced")

RAIL_LINES = {"Green", "Purple", "Yellow", "Pink"}


def _walk_minutes(it: dict) -> float:
    return round(sum(l["minutes"] for l in it["legs"] if l["mode"] == "walk"), 1)


def _wait_minutes(it: dict) -> float:
    """Boardings carry a scheduled wait that is not attributed to a leg, so recover it.

    ``minutes_in_vehicle`` counts riding only; the difference against the path total is
    the boarding/transfer penalty the search added. We surface it rather than hiding it,
    because a 10-minute ride that needs 12 minutes of waiting is not a 10-minute trip.
    """
    return round(max(0.0, it.get("minutes_total", it["minutes_in_vehicle"]) -
                     it["minutes_in_vehicle"]), 1)


def _fare_breakdown(it: dict) -> list[dict]:
    rows = []
    for leg in it["legs"]:
        if leg["mode"] == "walk":
            continue
        basis = leg.get("fare_basis") or "unknown"
        rows.append({
            "mode": leg["mode"],
            "service": leg["label"],
            "from": leg["from"],
            "to": leg["to"],
            "km": leg["km"],
            "fare_inr": leg["fare_inr"],
            "basis": basis,
            "basis_label": {
                "bmrcl_official_chart": "BMRCL Revised Fare Chart (14.02.2025)",
                "bmtc_distance_slab": "BMTC distance slab",
                "bmtc_fare_stage": "BMTC fare stage",
                "gtfs_fare_stage": "GTFS fare_stage (AC/premium route)",
                "ksrtc_fare_not_published": "KSRTC — fare not published in this build",
                "walking": "no fare",
            }.get(basis, basis),
        })
    return rows


def _explain(it: dict, best: dict[str, float]) -> str:
    """One sentence, built only from numbers that are actually in the response."""
    modes = " + ".join(it["mode_sequence"])
    fare, mins = it["total_price_inr"], it["minutes_in_vehicle"]
    walk = _walk_minutes(it)
    if fare is None:
        # A KSRTC leg has no published fare in this build; say that instead of printing a
        # number that came from a city-bus slab.
        unpriced = ", ".join(u["label"] for u in (it.get("unpriced_legs") or []))
        bits = [f"fare not published for {unpriced or 'one leg'} on {modes}"]
    else:
        bits = [f"₹{fare:.0f} on {modes}"]
    bits.append(f"{mins:.0f} min riding")
    if walk:
        bits.append(f"{walk:.0f} min walking")
    if it["transfers"]:
        bits.append(f"{it['transfers']} change{'s' if it['transfers'] > 1 else ''}")
    else:
        bits.append("no changes")
    head = ", ".join(bits)
    tags = []
    if fare is None:
        tags.append("fare unknown, so not ranked on price")
    elif fare <= best["price"] + 0.5:
        tags.append("cheapest fare")
    if mins + walk <= best["time"] + 0.5:
        tags.append("quickest door to door")
    if it["transfers"] == best["transfers"] and it["transfers"] <= 1:
        tags.append("fewest changes")
    if walk <= best["walk"] + 0.5:
        tags.append("least walking")
    if not tags:
        tags.append(f"₹{fare - best['price']:.0f} more than the cheapest option to save "
                    f"{best['time'] - (mins + walk):.0f} min")
    return f"{head} — {'; '.join(tags)}."


def _annotate(its: list[dict], preference: str = "") -> dict[str, float]:
    """Add ranking flags, scores and explanations. Returns the best-of summary."""
    if not its:
        return {"price": 0, "time": 0, "transfers": 0, "walk": 0}
    for it in its:
        it["walking_minutes"] = _walk_minutes(it)
        it["total_minutes"] = round(it["minutes_in_vehicle"] + it["walking_minutes"], 1)
        it["fare_breakdown"] = _fare_breakdown(it)
        it["is_air_conditioned"] = any(
            "V" == str(l["label"])[:1] for l in it["legs"] if l["mode"] == "bus")

    priced = [i["total_price_inr"] for i in its if i["total_price_inr"] is not None]
    best = {
        # An unpriced itinerary must not become "cheapest" by having no number at all.
        "price": min(priced) if priced else 0.0,
        "time": min(i["total_minutes"] for i in its),
        "transfers": min(i["transfers"] for i in its),
        "walk": min(i["walking_minutes"] for i in its),
    }
    for it in its:
        it["ranked_best_for"] = [
            name for name, key, val in (
                ("cheapest", "price", it["total_price_inr"]),
                ("fastest", "time", it["total_minutes"]),
                ("fewest_transfers", "transfers", it["transfers"]),
                ("least_walking", "walk", it["walking_minutes"]),
            ) if val is not None and abs(val - best[key]) < 0.51
        ]
        # A 0-100 convenience score so the default order is not arbitrary. Deliberately
        # simple and disclosed: min-max normalised, equal weight, lower is better.
        fare_for_score = (it["total_price_inr"] if it["total_price_inr"] is not None
                          else (max(priced) if priced else 0.0))
        span_p = max(1.0, (max(priced) if priced else 0.0) - best["price"])
        span_t = max(1.0, max(i["total_minutes"] for i in its) - best["time"])
        span_w = max(1.0, max(i["walking_minutes"] for i in its) - best["walk"])
        it["comfort_score"] = round(100 - (
            40 * (fare_for_score - best["price"]) / span_p
            + 40 * (it["total_minutes"] - best["time"]) / span_t
            + 10 * (it["walking_minutes"] - best["walk"]) / span_w
            + 10 * min(3, it["transfers"]) / 3), 1)
        it["explanation"] = _explain(it, best)
    # A stated preference outranks the comfort heuristic. If the traveller asks for the
    # cheapest trip, the cheapest trip is what sits at the top of the list.
    pref_key = {"cheapest": "total_price_inr", "fastest": "total_minutes",
                "fewest_transfers": "transfers", "least_walking": "walking_minutes"}
    def _null_last(value):
        return (value is None, value if value is not None else 0.0)

    if preference in pref_key:
        key = pref_key[preference]
        its.sort(key=lambda x: (_null_last(x[key]), -x["comfort_score"],
                                _null_last(x["total_price_inr"])))
    else:
        its.sort(key=lambda x: (-x["comfort_score"], _null_last(x["total_price_inr"])))
    for i, it in enumerate(its, 1):
        it["rank"] = i
    return best


def _departure_advice(its: list[dict], arrival_by: str | None) -> dict:
    """Recommend a departure time, only from durations the planner actually produced.

    Two honest modes:
      * ``depart_now`` — no target time was supplied, so the advice is the clock time and
        the arrival window implied by the chosen itinerary.
      * ``latest_departure`` — a target *arrival* time was supplied; the latest sensible
        departure is arrival minus the itinerary duration. No timetable is invented: the
        duration is the GTFS-derived one, and the note says so.
    """
    if not its:
        return {"mode": "none", "note": "No itinerary, so no departure to recommend."}
    fastest = min(its, key=lambda i: i["total_minutes"])
    out = {
        "mode": "depart_now" if not arrival_by else "latest_departure",
        "basis": "GTFS-derived duration for the recommended itinerary; not a timetable",
        "duration_minutes": fastest["total_minutes"],
        "itinerary_id": fastest["itinerary_id"],
        "timezone": TIMEZONE,
        "service_hours_caveat": (
            "BMTC and BMRCL service hours are not modelled per trip, so a departure outside "
            "roughly 05:00–23:00 may not have a matching service."),
    }
    if arrival_by:
        try:
            target = datetime.strptime(arrival_by, "%H:%M")
        except ValueError:
            out.update(mode="depart_now", note="arrival_by was not HH:MM; ignored")
            target = None
        if target:
            dep = target - timedelta(minutes=fastest["total_minutes"] + 5)
            out["latest_departure"] = dep.strftime("%H:%M")
            out["buffer_minutes"] = 5
            out["note"] = (f"Leave by {dep.strftime('%H:%M')} to arrive around "
                           f"{arrival_by} — {fastest['total_minutes']:.0f} min travel plus a "
                           f"5 min buffer.")
    if "note" not in out:
        now = _now()
        arr = now + timedelta(minutes=fastest["total_minutes"])
        out["leave_now"] = now.strftime("%H:%M")
        out["arrive_about"] = arr.strftime("%H:%M")
        out["note"] = (f"If you leave now you arrive around {arr.strftime('%H:%M')}, based on "
                       f"the {fastest['total_minutes']:.0f} min scheduled duration.")
    return out


# What the UI's mode filter maps to. "walking" and "driving" are not graph modes; they
# are handled separately because neither is a transit ride.
TRANSIT_MODES = {"bus", "metro"}


def plan(origin: str, destination: str, *, arrival_by: str | None = None,
         preference: str = "", max_itineraries: int = 4,
         modes: "set[str] | None" = None) -> dict[str, Any]:
    """Plan a trip and return the full, self-describing payload."""
    t0 = time.perf_counter()
    ordered = list(ALL_OBJECTIVES)
    if preference and preference in ALL_OBJECTIVES:
        ordered = [preference] + [o for o in ordered if o != preference]

    ride_modes = None
    if modes is not None:
        ride_modes = {m for m in modes if m in TRANSIT_MODES}
        if not ride_modes:
            return {
                "ok": False, "origin": {"name": origin}, "destination": {"name": destination},
                "itineraries": [], "warnings": [
                    "No BMTC bus or Namma Metro mode selected, so transit routing was "
                    "skipped. Walking-only and driving trips are computed separately."],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }

    raw = ranked_itineraries(origin, destination, objectives=tuple(ordered),
                             modes=ride_modes)
    if not raw.get("ok"):
        return {
            "ok": False,
            "origin": {"name": origin}, "destination": {"name": destination},
            "itineraries": [], "alternatives": [],
            "warnings": [w for w in raw.get("warnings", []) if w] or
                        ["No route found between those places."],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    its = raw["itineraries"][:max_itineraries]
    best = _annotate(its, preference)
    for it in its:
        it.pop("minutes_total", None)

    rec = its[0] if its else None
    return {
        "ok": True,
        "origin": raw["origin"],
        "destination": raw["destination"],
        "preference": preference or "balanced",
        "recommended": rec["itinerary_id"] if rec else None,
        "itineraries": its,
        "best_of": {
            "cheapest_inr": best["price"],
            "fastest_minutes": best["time"],
            "fewest_transfers": best["transfers"],
            "least_walking_minutes": best["walk"],
        },
        "objectives": {o: OBJECTIVE_LABEL[o] for o in ALL_OBJECTIVES},
        "departure_advice": _departure_advice(its, arrival_by),
        "fare_source": ("BMRCL Revised Fare Chart (14.02.2025) for metro; BMTC distance "
                        "slabs for bus. Google supplies no fares."),
        "scoring_note": ("comfort_score is a disclosed 0–100 heuristic: 40% fare, 40% total "
                         "time, 10% walking, 10% transfers, min–max normalised within the "
                         "results shown. It is a ranking aid, not a product of the data."),
        "graph": raw.get("graph", {}),
        "warnings": raw.get("warnings", []),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
