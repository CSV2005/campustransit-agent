"""Fare primitives used by the graph search, and exact re-pricing of a found path.

Two different jobs live here and they must not be confused:

    marginal_*()   the *increment* a single edge adds to a ticket. Additive by
                   construction, which is what Dijkstra/A* require. For the metro it is
                   exact (the official chart is a step function of hop count, so the
                   increments telescope to the chart value). For buses it telescopes
                   through BMTC's distance slabs, which is an approximation of the
                   published per-stage fare table.

    price_path()   the *exact* fare of the finished itinerary, read from the published
                   tables: BMRCL's official chart for metro hops, the GTFS fare-stage
                   matrix for each bus leg. The search optimises with marginals; the
                   number the user sees always comes from here.
"""
from __future__ import annotations

import functools

from src.tools.common import db
from src.tools.fare_tool import bmtc_slab_fare

MIN_FARE_BUS = 6.0
MIN_FARE_METRO = 10.0

# The BMTC GTFS feed also carries ~40 long-distance KSRTC services (route short names
# like "KSRTC-HASSAN-1"). They are real services with real stops and trips, so they route
# — but they are priced by a state-wide KSRTC fare table that is not in this build, and
# the BMTC distance slab stops at ₹32 for 50 km, which for a 300 km coach trip is not an
# estimate, it is a different number entirely. So: route them, do not price them.
KSRTC_LABEL_PREFIXES = ("KSRTC-", "KSRTC ", "V-KSRTC")


def operator_of(label: str | None, carriers: dict[str, str] | None = None) -> str:
    """Which operator a ride leg belongs to. BMTC unless the feed says KSRTC."""
    text = (label or "").strip().upper()
    if text.startswith(tuple(x.upper() for x in KSRTC_LABEL_PREFIXES)):
        return "KSRTC"
    return "BMTC"


@functools.lru_cache(maxsize=1)
def metro_slabs() -> dict[int, float]:
    """Official BMRCL chart: stations travelled -> fare (14.02.2025)."""
    con = db()
    rows = con.execute("SELECT stations_travelled, fare_inr FROM metro_fare_slabs").fetchall()
    con.close()
    return {int(r["stations_travelled"]): float(r["fare_inr"]) for r in rows}


def metro_fare_for_hops(hops: int) -> float:
    """Official chart value. Zero hops means you never entered the paid area: ₹0."""
    table = metro_slabs()
    if not table:
        return 0.0
    if hops <= 0:
        return 0.0
    return table[max(1, min(max(table), hops))]


def metro_marginal(hops_so_far: int) -> float:
    """Fare added by one more metro hop (₹). Always >= 0, so Dijkstra stays valid."""
    return metro_fare_for_hops(hops_so_far + 1) - metro_fare_for_hops(max(0, hops_so_far))


def bus_ticket_fare(km: float) -> float:
    """BMTC slab fare for a ticket — ₹0 before you board, never the ₹6 minimum."""
    return 0.0 if km <= 0 else float(bmtc_slab_fare(km))


def bus_marginal(km_so_far: float, km: float) -> float:
    """Marginal slab fare. Starting from 0 must charge the full first slab, so the
    baseline is bus_ticket_fare(0) = 0 rather than slab(0) = 6."""
    return bus_ticket_fare(km_so_far + km) - bus_ticket_fare(km_so_far)


def price_path(graph, path) -> dict:
    """Exact price of a path, leg by leg, from the published tables.

    Every maximal run of ride-edges on the same vehicle is one leg (one ticket).
    Metro legs across an interchange share one BMRCL ticket, so their hops are summed
    before the chart is applied — which is how BMRCL actually charges.
    """
    legs: list[dict] = []
    cur = None
    for idx, e in enumerate(path.edges):
        if e.mode in ("board", "alight"):
            cur = None if e.mode == "alight" else cur
            continue
        if e.mode == "walk":
            if e.minutes > 0.6:
                prev = legs[-1] if legs else None
                if prev is not None and prev["mode"] == "walk":
                    # consecutive walking edges are one walk, not five
                    prev["to_stop"] = e.to
                    prev["km"] += e.km
                    prev["minutes"] += e.minutes
                    prev["edge_end"] = idx + 1
                else:
                    legs.append({"mode": "walk", "carrier": None, "label": "walk",
                                 "from_stop": e.src, "to_stop": e.to, "stops": 1, "km": e.km,
                                 "minutes": e.minutes, "fare_inr": 0.0, "basis": "walking",
                                 "edge_start": idx, "edge_end": idx + 1})
            cur = None
            continue
        if cur is None or cur["carrier"] != e.carrier:
            cur = {"mode": e.mode, "carrier": e.carrier, "label": e.label,
                   "from_stop": e.src, "to_stop": e.to, "stops": 1, "km": 0.0,
                   "minutes": 0.0, "fare_inr": 0.0, "basis": None,
                   "edge_start": idx, "edge_end": idx + 1}
            legs.append(cur)
        cur["to_stop"] = e.to
        cur["stops"] += 1
        cur["km"] += e.km
        cur["minutes"] += e.minutes
        cur["edge_end"] = idx + 1

    con = db()
    total = 0.0

    metro_buffer = []
    for lg in legs:
        if lg["mode"] == "metro":
            lg["source"] = "bmrcl_official_chart"
            metro_buffer.append(lg)
            continue
        if metro_buffer:
            _settle_metro(metro_buffer)
            total += sum(g["fare_inr"] for g in metro_buffer)
            metro_buffer = []
        if lg["mode"] == "bus":
            lg["operator"] = operator_of(lg.get("label"))
            if lg["operator"] == "KSRTC":
                # No KSRTC fare table in this build. Leave the amount unset rather than
                # run an intercity coach through a city bus slab.
                lg["fare_inr"] = None
                lg["basis"] = "ksrtc_fare_not_published"
                lg["unpriced"] = True
                continue
            fare, basis = _bus_leg_fare(con, lg["carrier"], lg["from_stop"], lg["to_stop"],
                                        lg["km"])
            lg["fare_inr"], lg["basis"] = fare, basis
            total += fare
    if metro_buffer:
        _settle_metro(metro_buffer)
        total += sum(g["fare_inr"] for g in metro_buffer)
    con.close()

    unpriced = [lg for lg in legs if lg.get("unpriced")]
    for lg in legs:
        lg["from_name"] = graph.stops.get(lg["from_stop"], {}).get("name", lg["from_stop"])
        lg["to_name"] = graph.stops.get(lg["to_stop"], {}).get("name", lg["to_stop"])
        lg["minutes"] = round(lg["minutes"], 1)
        lg["km"] = round(lg["km"], 2)
        lg.setdefault("operator", "BMRCL" if lg["mode"] == "metro" else
                      ("—" if lg["mode"] == "walk" else "BMTC"))
    return {
        "legs": legs,
        # A total that silently omits an unpriced leg is worse than no total: a student
        # could read ₹40 and board a ₹400 coach. None means "this trip has a leg we cannot
        # price", and `priced_subtotal_inr` says how much of it we could.
        "total_inr": None if unpriced else round(total, 2),
        "priced_subtotal_inr": round(total, 2),
        "unpriced_legs": [{"label": lg.get("label"), "operator": lg.get("operator"),
                           "from": lg.get("from_name"), "to": lg.get("to_name"),
                           "km": lg["km"]} for lg in unpriced],
    }


def _settle_metro(group: list[dict]) -> None:
    """BMRCL charges one token for a whole metro journey, interchanges included."""
    hops = sum(g["stops"] - 1 for g in group)
    fare = metro_fare_for_hops(hops) if hops else 0.0
    for g in group:
        g["fare_inr"] = 0.0
        g["basis"] = "bmrcl_official_chart"
        g["hops"] = g["stops"] - 1
    if group:
        group[-1]["fare_inr"] = fare
    return


def _bus_leg_fare(con, route_id: str, from_stop: str, to_stop: str, km: float):
    """Published fare-stage value if the feed has the exact pair, else the distance slab."""
    zones = {}
    for sid in (from_stop, to_stop):
        row = con.execute("SELECT zone_id FROM stops WHERE stop_id=?", (sid,)).fetchone()
        zones[sid] = row["zone_id"] if row else None
    fa, fb = zones.get(from_stop), zones.get(to_stop)
    if fa and fb:
        row = con.execute(
            "SELECT fare_inr, fare_max_inr FROM fare_matrix WHERE route_id=? "
            "AND from_zone=? AND to_zone=?", (route_id, fa, fb)).fetchone()
        if row:
            lo = float(row["fare_inr"])
            hi = float(row["fare_max_inr"] or lo)
            return (lo + hi) / 2.0, "gtfs_fare_stage"
    return float(bmtc_slab_fare(km)), "bmtc_distance_slab"
