"""Graph-search routing tools: Dijkstra, A*, RRT over the multimodal network.

These complement `find_routes` rather than replace it. `find_routes` enumerates which
route numbers run between two places — the question a commuter actually asks at a bus
stop. This module answers the optimisation question: *given a cost to minimise, what is
the best path through the whole network, and how confident can we be that it is the best?*

    plan_optimal_route          Dijkstra (exact) or A* (exact + faster), with exact fares
    compare_routing_algorithms  run all three, report agreement and search effort

Every fare printed here is re-priced from the published tables by `price_path` — the
search optimises with marginal costs, the user is quoted the real thing.
"""
from __future__ import annotations

from typing import Any

from src.routing import fares
from src.routing.graph import MAX_WALK_M, Path, build_graph, snap_stops
from src.routing.rrt import rrt_star_walk, rrt_transit
from src.routing.search import (OBJECTIVE_LABEL, PROFILES, astar, dijkstra,
                                replay_cost)
from src.tools.common import METRO_LINE_COLOURS, resolve_place

COST_MODEL = {
    "fastest": "GTFS scheduled run time + 3 min boarding + 4 min per change. Exact.",
    "cheapest": "BMRCL official hop chart + BMTC distance slabs, charged per edge. "
                "Optimal with respect to the slab model; the quoted fare is then "
                "re-priced from the published tables, which can differ on premium "
                "(airport/AC) routes.",
    "fewest_transfers": "number of vehicles boarded. Exact.",
}


def _resolve(place: str) -> dict:
    r = resolve_place(place)
    if not r.get("ok"):
        return {"ok": False, "error": f"could not resolve '{place}'"}
    return r


def _summary(label: str, path: Path, priced: dict, algo: str, objective: str) -> str:
    rides = [l for l in priced["legs"] if l["mode"] in ("bus", "metro")]
    if not rides:
        return f"{label}: no ride legs found ({algo})."
    parts = []
    for l in rides:
        icon = "🚇" if l["mode"] == "metro" else "🚌"
        fare = ("fare not published" if l["fare_inr"] is None
                else f"Rs {l['fare_inr']:.0f}" if l["fare_inr"] else "included")
        parts.append(f"{icon} {l['label']} {l['from_name']} → {l['to_name']} ({fare})")
    total = priced["total_inr"]
    money = f"**Rs {total:.0f}**" if total is not None else "**fare not published**"
    return (f"{label}: {money}, about {path.minutes:.0f} min in-vehicle, "
            f"{len(rides)} leg(s) — {'; '.join(parts)}. "
            f"[{algo} over the {objective} objective]")


def _legs_payload(priced: dict) -> list[dict]:
    out = []
    for l in priced["legs"]:
        out.append({
            "mode": l["mode"], "label": l["label"],
            "from": l.get("from_name"), "to": l.get("to_name"),
            "minutes": round(l["minutes"], 1), "km": round(l["km"], 2),
            # None means "this leg has no published fare" (KSRTC intercity). Coercing it
            # to 0 here would show a free coach ride, which is exactly the invented number
            # the whole project refuses to display.
            "fare_inr": (round(l["fare_inr"], 2)
                         if isinstance(l["fare_inr"], (int, float)) else None),
            "unpriced": bool(l.get("unpriced")),
            "fare_basis": l.get("basis"),
            "stops": l.get("stops"),
        })
    return out


def plan_optimal_route(origin: str, destination: str, objective: str = "fastest",
                       algorithm: str = "astar") -> dict[str, Any]:
    """Find the optimal path between two places with Dijkstra or A*.

    Returns legs with per-leg fares and the exact total, the travel time, and the search
    statistics (nodes expanded) so the answer can be audited. Every price is re-read from
    the published BMRCL chart / BMTC fare stages — the search never invents a fare.
    """
    o, d = _resolve(origin), _resolve(destination)
    if not o.get("ok") or not d.get("ok"):
        return {"ok": False, "tool": "plan_optimal_route",
                "summary": f"Could not resolve '{origin}' or '{destination}'."}
    objective = objective if objective in PROFILES else "fastest"
    algorithm = algorithm if algorithm in ("dijkstra", "astar") else "astar"

    g = build_graph((o["lat"], o["lon"]), (d["lat"], d["lon"]))
    if len(g.stops) < 2:
        return {"ok": False, "tool": "plan_optimal_route",
                "summary": "No transit corridor found between those places."}
    start = [(s, 0.0) for s in snap_stops(o["lat"], o["lon"])]
    goals = set(snap_stops(d["lat"], d["lon"], radius_km=0.8))
    if not start or not goals:
        return {"ok": False, "tool": "plan_optimal_route",
                "summary": "No bus or metro stop near one of those places."}

    runner = dijkstra if algorithm == "dijkstra" else astar
    path = runner(g, start, goals, objective)
    if not path.reached:
        return {"ok": False, "tool": "plan_optimal_route",
                "summary": f"No route found between {o['name']} and {d['name']}.",
                "data": {"expanded": path.expanded, "generated": path.generated}}

    priced = fares.price_path(g, path)
    label = f"{o['name']} → {d['name']}"
    return {
        "ok": True,
        "tool": "plan_optimal_route",
        "summary": _summary(label, path, priced, algorithm, objective),
        "data": {
            "origin": o["name"], "destination": d["name"],
            "total_fare_inr": priced["total_inr"],
            "minutes_in_vehicle": path.minutes,
            "legs": _legs_payload(priced),
            "objective": objective,
            "objective_label": OBJECTIVE_LABEL[objective],
            "cost_model": COST_MODEL[objective],
            "algorithm": algorithm,
            "nodes_expanded": path.expanded,
            "nodes_generated": path.generated,
            "elapsed_ms": path.elapsed_ms,
            "graph": {"stops": len(g.stops), "ride_edges": g.n_ride_edges,
                      "walk_edges": g.n_walk_edges,
                      "carriers_pruned": getattr(g, "pruned_parallel_carriers", 0)},
            "coordinates": [list(g.coord(s)) for s in path.nodes if s in g.stops],
            "cost_replay_check": replay_cost(path, objective),
            "cost_matches_replay": abs(replay_cost(path, objective) - path.cost) < 0.05,
            "fare_source": "BMRCL official chart (metro) / BMTC GTFS fare stages (bus)",
        },
        "sources": ["BMRCL Revised Fare Chart 14.02.2025",
                    "BMTC GTFS fare_rules + fare stages",
                    "GTFS stop_times (scheduled run times)"],
    }


def compare_routing_algorithms(origin: str, destination: str,
                               objective: str = "fastest") -> dict[str, Any]:
    """Run Dijkstra, A* and RRT on the same corridor and report how they compare.

    Use this when the user asks how the routing works, or wants the cheapest/fastest
    option with the search effort exposed. It also proves the optimality claim: if
    Dijkstra and A* ever disagreed on cost, `costs_agree` would be False.
    """
    o, d = _resolve(origin), _resolve(destination)
    if not o.get("ok") or not d.get("ok"):
        return {"ok": False, "tool": "compare_routing_algorithms",
                "summary": f"Could not resolve '{origin}' or '{destination}'."}
    objective = objective if objective in PROFILES else "fastest"

    g = build_graph((o["lat"], o["lon"]), (d["lat"], d["lon"]), modes=modes)
    start = [(s, 0.0) for s in snap_stops(o["lat"], o["lon"])]
    goals = set(snap_stops(d["lat"], d["lon"], radius_km=0.8))
    if not start or not goals:
        return {"ok": False, "tool": "compare_routing_algorithms",
                "summary": "No stops near one of those places."}

    dij = dijkstra(g, start, goals, objective)
    ast = astar(g, start, goals, objective)
    rrt = rrt_transit(g, start, goals, objective)
    ub = None
    if rrt.goal_hit and rrt.reached:
        ub = (replay_cost(rrt, objective) + rrt.tail_min) * 1.002 + 0.25
    ast_b = astar(g, start, goals, objective, upper_bound=ub) if ub else None

    agree = (dij.reached and ast.reached and abs(dij.cost - ast.cost) < 1e-6)
    rrt_cost = (replay_cost(rrt, objective) + rrt.tail_min) if rrt.reached else None
    gap = (round((rrt_cost / dij.cost - 1) * 100, 1)
           if rrt_cost and dij.reached else None)

    rows = [
        {"algorithm": "dijkstra", "optimal": True, "reached": dij.reached,
         "cost": dij.cost, "nodes_expanded": dij.expanded, "elapsed_ms": dij.elapsed_ms},
        {"algorithm": "astar", "optimal": True, "reached": ast.reached,
         "cost": ast.cost, "nodes_expanded": ast.expanded, "elapsed_ms": ast.elapsed_ms},
    ]
    if ast_b is not None:
        rows.append({"algorithm": "astar+rrt_bound", "optimal": True,
                     "reached": ast_b.reached, "cost": ast_b.cost,
                     "nodes_expanded": ast_b.expanded, "elapsed_ms": ast_b.elapsed_ms})
    rows.append({"algorithm": "rrt", "optimal": False, "reached": rrt.goal_hit,
                 "cost": round(rrt_cost, 2) if rrt_cost else None,
                 "nodes_expanded": rrt.expanded, "elapsed_ms": rrt.elapsed_ms,
                 "gap_vs_optimal_pct": gap, "notes": rrt.notes})

    best = fares.price_path(g, dij) if dij.reached else {"legs": [], "total_inr": 0}
    width = (1 - ast.expanded / dij.expanded) * 100 if dij.expanded else 0
    bound = ((1 - ast_b.expanded / dij.expanded) * 100 if ast_b is not None and
             ast_b.reached and dij.expanded else 0)
    summary = (f"{o['name']} → {d['name']}: Dijkstra and A* agree on the optimal cost "
               f"({dij.cost:.2f} min of generalised cost) — "
               f"{'yes' if agree else 'NO'}. A* expanded {ast.expanded} vs "
               f"{dij.expanded} nodes ({width:.0f}% fewer)")
    if ast_b is not None and ast_b.reached:
        helped = ast_b.expanded < ast.expanded
        summary += (f"; the RRT upper bound expanded {ast_b.expanded} ({bound:.0f}% fewer) — "
                    + ("it cut the frontier further" if helped else
                       f"no help, because RRT's own answer was {gap:+.0f}% off optimal"
                       if gap is not None else "no help"))
    summary += f". RRT found a feasible route in {rrt.elapsed_ms:.0f} ms"
    summary += (f", {gap:+.0f}% off optimal" if gap is not None
                else " but did not reach the destination stop")

    return {
        "ok": True,
        "tool": "compare_routing_algorithms",
        "summary": summary + ".",
        "data": {
            "origin": o["name"], "destination": d["name"], "objective": objective,
            "costs_agree": agree, "rrt_goal_hit": rrt.goal_hit,
            "rrt_gap_pct": gap,
            "astar_expansion_saving_pct": round(width, 1),
            "bounded_astar_expansion_saving_pct": round(bound, 1),
            "rrt_bound_helped": bool(ast_b is not None and ast_b.reached
                                     and ast_b.expanded < ast.expanded),
            "table": rows,
            "best_fare_inr": best["total_inr"],
            "best_legs": _legs_payload(best),
            "graph": {"stops": len(g.stops), "ride_edges": g.n_ride_edges,
                      "walk_edges": g.n_walk_edges},
        },
        "sources": ["GTFS stop_times", "BMRCL Revised Fare Chart 14.02.2025"],
    }


def plan_walk_connector(origin: str, destination: str) -> dict[str, Any]:
    """Plan a walking path between two points with RRT* (continuous space).

    Used for the last-metre question — "how far is the stop from here, really" — where
    there is no transit graph to search. Returns the polyline, its length, and an honest
    `data_status`: OBSTACLE_AWARE when building footprints are loaded, OPEN_FIELD when
    the obstacle layer is empty (which is the current default).
    """
    o, d = _resolve(origin), _resolve(destination)
    if not o.get("ok") or not d.get("ok"):
        return {"ok": False, "tool": "plan_walk_connector",
                "summary": f"Could not resolve '{origin}' or '{destination}'."}
    res = rrt_star_walk((o["lat"], o["lon"]), (d["lat"], d["lon"]))
    km = res["length_km"]
    minutes = km / 4.8 * 60
    return {
        "ok": True,
        "tool": "plan_walk_connector",
        "summary": (f"Walking from {o['name']} to {d['name']}: about {km:.2f} km "
                    f"({minutes:.0f} min at 4.8 km/h), RRT* planned "
                    f"{'around obstacles' if res['obstacles'] else 'in open space'}. "
                    f"Straight line is {res['straight_line_km']:.2f} km."),
        "data": {**res, "minutes_at_4_8_kmh": round(minutes, 1),
                 "note": ("no building/barrier layer is loaded, so the planner has "
                          "nothing to avoid and the path is near-straight"
                          if not res["obstacles"] else
                          f"{res['obstacles']} obstacle rings avoided")},
        "sources": ["RRT* continuous planner over WGS84 coordinates"],
    }


# --------------------------------------------------------------------- map / UI
def ranked_itineraries(origin: str, destination: str,
                       objectives: tuple[str, ...] = ("fastest", "cheapest",
                                                      "fewest_transfers"),
                       modes: "set[str] | None" = None) -> dict[str, Any]:
    """Rank itineraries between two places, with polyline geometry for a map.

    This is the merge point the UI calls: one graph build, one search per objective,
    every result priced from the published tables, every leg carrying the stop-to-stop
    geometry the path actually traversed.

    Geometry comes from the GTFS stops the path visits — real coordinates, not a straight
    line between endpoints and not a Google polyline.
    """
    o, d = _resolve(origin), _resolve(destination)
    if not o.get("ok") or not d.get("ok"):
        return {"ok": False,
                "warnings": [f"Could not resolve '{origin}'." if not o.get("ok") else "",
                             f"Could not resolve '{destination}'." if not d.get("ok") else ""],
                "itineraries": []}

    g = build_graph((o["lat"], o["lon"]), (d["lat"], d["lon"]), modes=modes)
    start = [(s, 0.0) for s in snap_stops(o["lat"], o["lon"])]
    goals = set(snap_stops(d["lat"], d["lon"], radius_km=0.8))
    if not start or not goals:
        return {"ok": False, "warnings": ["No bus or metro stop near one of those places."],
                "itineraries": [], "origin": {"name": o["name"], "lat": o["lat"],
                                              "lng": o["lon"]},
                "destination": {"name": d["name"], "lat": d["lat"], "lng": d["lon"]}}

    seen: dict[tuple, dict] = {}
    for objective in objectives:
        path = dijkstra(g, start, goals, objective)
        if not path.reached:
            continue
        priced = fares.price_path(g, path)
        legs = _geometry_legs(g, path, priced)
        signature = tuple((l["mode"], l["label"]) for l in legs if l["mode"] != "walk")
        if not signature:
            continue
        if signature in seen:
            seen[signature]["also_optimal_for"].append(objective)
            continue
        seen[signature] = {
            "itinerary_id": f"{objective}-{'|'.join(str(s[1]) for s in signature)[:40]}",
            "objective": objective,
            "also_optimal_for": [],
            "mode_sequence": _mode_sequence(legs),
            "total_price_inr": priced["total_inr"],
            "minutes_in_vehicle": path.minutes,
            "paid_legs": sum(1 for l in legs if l["mode"] in ("bus", "metro")),
            "transfers": max(0, sum(1 for l in legs if l["mode"] in ("bus", "metro")) - 1),
            "legs": legs,
            "geometry": [[round(g.stops[s]["lat"], 5), round(g.stops[s]["lon"], 5)]
                         for s in path.nodes if s in g.stops],
            "fare_source": "bmrcl_official_chart / bmtc_fare_stage",
            "verification": "VERIFIED_PRIMARY" if any(
                l["mode"] == "metro" for l in legs) else "VERIFIED_SECONDARY",
            "priced_subtotal_inr": priced.get("priced_subtotal_inr"),
            "unpriced_legs": priced.get("unpriced_legs") or [],
            "operators": sorted({l.get("operator") for l in legs
                                 if l["mode"] in ("bus", "metro")} ),
            "notes": [],
        }
        if seen[signature]["unpriced_legs"]:
            seen[signature]["verification"] = "PARTIALLY_UNPRICED"
            seen[signature]["notes"].append(
                "uses a KSRTC service; KSRTC's fare table is not in this build, so the "
                "total is not shown")
        if objective == "cheapest":
            seen[signature]["notes"].append(
                "ranked by BMTC distance slabs; the quoted fare is the published one "
                "and may differ on premium (airport/AC) routes")

    its = list(seen.values())
    # Unpriced itineraries sort last: an unknown fare is not a cheap fare.
    its.sort(key=lambda x: (x["total_price_inr"] is None, x["total_price_inr"] or 0.0,
                            x["minutes_in_vehicle"]))
    for i, it in enumerate(its, 1):
        it["rank"] = i
    return {
        "ok": bool(its),
        "origin": {"name": o["name"], "lat": round(o["lat"], 5), "lng": round(o["lon"], 5)},
        "destination": {"name": d["name"], "lat": round(d["lat"], 5),
                        "lng": round(d["lon"], 5)},
        "itineraries": its,
        "sort_order": "total_price_asc",
        "graph": {"stops": len(g.stops), "ride_edges": g.n_ride_edges},
        "warnings": [] if its else ["No feasible itinerary found for that pair."],
    }


def _geometry_legs(g, path, priced: dict) -> list[dict]:
    """Attach the traversed stop coordinates to each priced leg.

    `price_path` records which edge range each leg covers, so this is a direct slice —
    no counting, no guessing which edges belong to which leg.
    """
    # edge i runs nodes[i] -> nodes[i+1]
    coords: list[list[list[float]]] = []
    for i in range(len(path.edges)):
        a = path.nodes[i] if i < len(path.nodes) else None
        b = path.nodes[i + 1] if i + 1 < len(path.nodes) else None
        if a in g.stops and b in g.stops:
            coords.append([[round(g.stops[a]["lat"], 5), round(g.stops[a]["lon"], 5)],
                           [round(g.stops[b]["lat"], 5), round(g.stops[b]["lon"], 5)]])
        else:
            coords.append([])

    payload = []
    for leg in priced["legs"]:
        pts: list[list[float]] = []
        for i in range(leg.get("edge_start", 0), leg.get("edge_end", 0)):
            for pt in coords[i]:
                if not pts or pts[-1] != pt:
                    pts.append(pt)
        fare = leg.get("fare_inr")
        entry = {
            "mode": leg["mode"], "label": leg["label"],
            "operator": leg.get("operator"),
            "from": leg.get("from_name"), "to": leg.get("to_name"),
            "minutes": round(leg["minutes"], 1), "km": round(leg["km"], 2),
            # None means "this leg has no published fare", which is different from ₹0.
            "fare_inr": None if fare is None else round(fare, 2),
            "fare_basis": leg.get("basis"), "stops": leg.get("stops"),
            "geometry": pts,
        }
        if leg["mode"] == "metro":
            # Name the line and its official colour on the leg itself, so the rich
            # endpoint and the §11 contract describe a metro ride the same way.
            entry["line"] = leg["label"]
            entry["line_colour"] = METRO_LINE_COLOURS.get(leg["label"])
        payload.append(entry)
    return payload


def _mode_sequence(legs: list[dict]) -> list[str]:
    seq = [l["mode"] for l in legs if l["mode"] in ("bus", "metro")]
    return seq or ["bus"]
