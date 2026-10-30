"""One expansion loop, three cost profiles, two algorithms.

Dijkstra and A* differ only in the order the priority queue pops. Writing that as a
single function with a pluggable `h` guarantees the two algorithms search the *same*
graph with the *same* successor rule — which is what makes the "do they agree?" check
in `benchmark_routing.py` a real test rather than a coincidence.

    dijkstra()   f(n) = g(n)              optimal; expands everything cheaper than the goal
    astar()      f(n) = g(n) + h(n)       optimal if h is admissible; expands far less

The heuristic is pure geometry: straight-line distance to the goal divided by the fastest
speed observed anywhere in the graph. No vehicle beats the fastest vehicle, and no path is
shorter than a straight line, so it can never overestimate the remaining travel time.
That is the admissibility argument, and it is the reason the A* answer may be called
optimal rather than merely good.

Search state: (stop, carrier, ticket, hops, km)

    stop     where you are
    carrier  the vehicle you are on, or "none" while on foot
    ticket   "none" | "metro" | "bus". An open ticket survives a metro interchange but not
             an exit, which is what makes a line change one fare and a bus change two.
    hops     stations travelled on the open metro ticket
    km       kilometres travelled on the open bus ticket
"""
from __future__ import annotations

import heapq
import time

from src.routing.fares import bus_marginal, metro_marginal
from src.routing.graph import (BOARD_PENALTY_MIN, METRO_BOARD_PENALTY_MIN,
                               METRO_LINES, TRANSFER_PENALTY_MIN, Edge, Path, TransitGraph)
from src.tools.common import haversine_km

PROFILES: dict[str, dict[str, float]] = {
    # name                 minutes  rupees  transfers  walk (extra weight on walking minutes)
    "fastest":            {"minutes": 1.0,  "inr": 0.0, "transfers": 0.0,  "walk": 0.0},
    "cheapest":           {"minutes": 0.05, "inr": 1.0, "transfers": 0.0,  "walk": 0.0},
    "fewest_transfers":   {"minutes": 0.05, "inr": 0.0, "transfers": 45.0, "walk": 0.0},
    # Walking minutes dominate the cost, but not absolutely: a 40-minute walk to save no
    # money is still worse than a 5-minute walk to save nothing.
    "least_walking":      {"minutes": 0.15, "inr": 0.30, "transfers": 2.0, "walk": 10.0},
    # Balanced: 1 minute of riding ≈ 35 paise, a change of vehicle ≈ 6 minutes,
    # and a minute on foot costs 1.5× a minute seated.
    "balanced":           {"minutes": 1.0,  "inr": 0.35, "transfers": 6.0, "walk": 1.5},
}
OBJECTIVE_LABEL = {
    "fastest": "fastest journey (scheduled minutes, including wait and transfer time)",
    "cheapest": "lowest total fare",
    "fewest_transfers": "fewest changes of vehicle",
    "least_walking": "least time on foot",
    "balanced": "best trade-off between fare, time, transfers and walking",
}

# Ranking prior for KSRTC services, rupees per km. Derived from the two published
# KSRTC point-to-point fares this build holds — ₹124 for 65 km and ₹162 for 145 km —
# which bracket ₹1.12–₹1.91/km. It exists only so the "cheapest" search does not treat an
# unpriced intercity coach as a ₹32 ride; it is never shown to a user as a fare.
KSRTC_PRIOR_INR_PER_KM = 1.4

State = tuple[str, str, str, int, float]


def _is_ksrtc(edge) -> bool:
    label = (getattr(edge, "label", "") or "").strip().upper()
    return label.startswith(("KSRTC-", "KSRTC ", "V-KSRTC"))


def _board_penalty(carrier: str) -> float:
    return METRO_BOARD_PENALTY_MIN if carrier in METRO_LINES else BOARD_PENALTY_MIN


def make_heuristic(g: TransitGraph, goals: set[str], w: dict[str, float]):
    """Straight-line distance to the closest goal, divided by the fastest edge speed."""
    if w["minutes"] <= 0:
        return lambda node: 0.0
    goal_coords = [g.coord(s) for s in goals if s in g.stops]
    if not goal_coords:
        return lambda node: 0.0
    vmax = max(1.0, g.max_speed_kmh)

    def h(node: str) -> float:
        la, lo = g.coord(node)
        return w["minutes"] * min(haversine_km(la, lo, gl, go) for gl, go in goal_coords) \
            / vmax * 60.0

    return h


def _run(g: TransitGraph, start: list[tuple[str, float]], goals: set[str],
         objective: str = "fastest", algorithm: str = "dijkstra",
         upper_bound: float | None = None, deadline_s: float = 15.0) -> Path:
    t0 = time.perf_counter()
    w = PROFILES[objective]
    fare_state = w["inr"] > 0
    if algorithm == "astar":
        _h = make_heuristic(g, goals, w)
        _cache: dict[str, float] = {}

        def h(node: str) -> float:
            v = _cache.get(node)
            if v is None:
                v = _cache[node] = _h(node)
            return v
    else:
        def h(node: str) -> float:
            return 0.0

    pq: list[tuple[float, float, int, State]] = []
    best: dict[State, float] = {}
    parent: dict[State, tuple[State, Edge] | None] = {}
    seeds: set[State] = set()
    seq = 0
    for sid, c in start:
        if sid not in g.stops:
            continue
        st: State = (sid, "none", "none", 0, 0.0)
        if c < best.get(st, float("inf")):
            best[st] = c
            parent[st] = None
            seeds.add(st)
            heapq.heappush(pq, (c + h(sid), c, seq, st))
            seq += 1

    expanded = 0
    while pq:
        _, gcost, _, state = heapq.heappop(pq)
        if gcost > best.get(state, float("inf")) + 1e-9:
            continue
        node, carrier, ticket, hops, km = state
        expanded += 1
        if expanded % 4000 == 0 and time.perf_counter() - t0 > deadline_s:
            break
        if node in goals and state not in seeds:
            return _finish(parent, state, gcost, expanded, len(best), algorithm, t0)

        def push(st: State, ncost: float, edge: Edge) -> None:
            nonlocal seq
            if upper_bound is not None and ncost + h(st[0]) >= upper_bound - 1e-9:
                return
            if ncost < best.get(st, float("inf")) - 1e-9:
                best[st] = ncost
                parent[st] = (state, edge)
                heapq.heappush(pq, (ncost + h(st[0]), ncost, seq, st))
                seq += 1

        # Alight. `ticket` doubles as two things: which fare product is currently open,
        # and whether the traveller has boarded anything at all. Only "none" means "has
        # never boarded" — once a bus is left behind the ticket becomes "bus" so that the
        # next boarding is correctly charged a transfer. Resetting it to "none" here used
        # to make every bus-to-bus change free, which is why "fewest transfers" returned
        # nine-leg journeys.
        if carrier != "none":
            if ticket == "metro":
                push((node, "none", "metro", hops, 0.0), gcost, _marker(node, "alight"))
            else:
                push((node, "none", "bus", 0, 0.0), gcost, _marker(node, "alight"))

        # board — first boarding pays a wait, a change of vehicle pays a transfer as well
        if carrier == "none":
            for c2 in g.carriers_at.get(node, ()):
                changing = ticket != "none"
                pen = _board_penalty(c2) + (TRANSFER_PENALTY_MIN if changing else 0.0)
                nc = gcost + w["minutes"] * pen + (w["transfers"] if changing else 0.0)
                metro = c2 in METRO_LINES
                st = (node, c2, "metro" if metro else "bus",
                      hops if (metro and ticket == "metro") else 0, 0.0)
                push(st, nc, Edge(to=node, mode="board", carrier=c2, minutes=pen,
                                  km=0.0, label=f"board {c2}"))

        # ride
        for e in g.out.get(node, ()):
            if e.mode == "walk":
                if carrier != "none":
                    continue
                extra = w.get("walk", 0.0) * e.minutes
                # Preserve `ticket` (did you board already?) but drop `hops`, because an
                # interrupted metro run cannot be priced as one continuous fare.
                push((e.to, "none", ticket, 0, 0.0),
                     gcost + w["minutes"] * e.minutes + extra, e)
                continue
            if e.carrier != carrier:
                continue
            if e.mode == "metro":
                fare = metro_marginal(hops) if fare_state else 0.0
                push((e.to, carrier, "metro", hops + 1 if fare_state else 0, 0.0),
                     gcost + w["minutes"] * e.minutes + w["inr"] * fare, e)
            elif _is_ksrtc(e):
                # A KSRTC coach has no published fare in this build, and the BMTC slab it
                # would otherwise inherit stops at ₹32/50 km — so pricing it that way makes
                # every intercity coach look like the cheapest option on the board. The
                # search uses a documented prior instead (see KSRTC_PRIOR_INR_PER_KM); the
                # response still reports the fare as unavailable, because a ranking prior
                # is not a quote.
                # Never cheaper than the BMTC slab this edge would otherwise inherit:
                # these services run through the city on their way out, and a first-mile
                # hop on one is not cheaper than the bus it replaces.
                fare = (max(bus_marginal(km, e.km), KSRTC_PRIOR_INR_PER_KM * e.km)
                        if fare_state else 0.0)
                push((e.to, carrier, "bus", 0, 0.0),
                     gcost + w["minutes"] * e.minutes + w["inr"] * fare, e)
            else:
                fare = bus_marginal(km, e.km) if fare_state else 0.0
                push((e.to, carrier, "bus", 0, km + e.km if fare_state else 0.0),
                     gcost + w["minutes"] * e.minutes + w["inr"] * fare, e)

    return Path(edges=[], nodes=[], cost=float("inf"), expanded=expanded,
                generated=len(best), algorithm=algorithm,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))


def _marker(node: str, kind: str) -> Edge:
    return Edge(to=node, mode=kind, carrier="none", minutes=0.0, km=0.0, label=kind)


def _finish(parent, state, cost, expanded, generated, algorithm, t0) -> Path:
    edges: list[Edge] = []
    nodes: list[str] = []
    cur = state
    guard = 0
    while parent.get(cur) is not None and guard < 200_000:
        prev, edge = parent[cur]
        edge.src = prev[0]
        edges.append(edge)
        nodes.append(cur[0])
        cur = prev
        guard += 1
    nodes.append(cur[0])
    edges.reverse()
    nodes.reverse()
    rides = [e for e in edges if e.mode in ("bus", "metro")]
    boardings = sum(1 for e in edges if e.mode == "board")
    return Path(edges=edges, nodes=nodes, cost=round(cost, 2),
                minutes=round(sum(e.minutes for e in rides), 1),
                boardings=boardings, expanded=expanded, generated=generated,
                algorithm=algorithm,
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))


# ------------------------------------------------------------------- public API
def dijkstra(g: TransitGraph, start, goals, objective: str = "fastest", **kw) -> Path:
    return _run(g, start, goals, objective, "dijkstra", **kw)


def astar(g: TransitGraph, start, goals, objective: str = "fastest", **kw) -> Path:
    return _run(g, start, goals, objective, "astar", **kw)


def replay_cost(path, objective: str = "fastest") -> float:
    """Independently recompute a path's cost from its edge list.

    Deliberately a second implementation of the same cost model, weights included. If the
    search and this function disagree, one of them is wrong — which is exactly what the
    benchmark checks. Boarding is inferred from a change of carrier, so paths produced by
    RRT (which folds the boarding into the first ride edge) score on the same basis as
    Dijkstra's.
    """
    w = PROFILES[objective]
    fare_state = w["inr"] > 0
    total = 0.0
    cur = None
    ticket = "none"
    hops, km = 0, 0.0
    for e in path.edges:
        if e.mode == "alight":
            cur = None
            ticket = "metro" if ticket == "metro" else "none"
            continue
        if e.mode == "board":
            total += w["minutes"] * e.minutes
            if ticket != "none":
                total += w["transfers"]
                if e.carrier not in METRO_LINES:
                    hops, km = 0, 0.0
            cur = e.carrier
            ticket = "metro" if e.carrier in METRO_LINES else "bus"
            if e.carrier not in METRO_LINES:
                hops, km = 0, 0.0
            continue
        if e.mode == "walk":
            total += w["minutes"] * e.minutes
            cur, ticket, hops, km = None, "none", 0, 0.0
            continue
        if e.carrier != cur:
            base = METRO_BOARD_PENALTY_MIN if e.mode == "metro" else BOARD_PENALTY_MIN
            total += w["minutes"] * base
            if ticket != "none":
                total += w["minutes"] * TRANSFER_PENALTY_MIN + w["transfers"]
            if not (e.mode == "metro" and ticket == "metro"):
                hops, km = 0, 0.0
            cur, ticket = e.carrier, ("metro" if e.mode == "metro" else "bus")
        total += w["minutes"] * e.minutes
        if fare_state:
            if e.mode == "metro":
                total += w["inr"] * metro_marginal(hops)
                hops += 1
            else:
                total += w["inr"] * bus_marginal(km, e.km)
                km += e.km
    return round(total, 3)


# kept for callers that only care about time
def replay_minutes(path) -> float:
    return replay_cost(path, "fastest")
