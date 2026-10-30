"""Rapidly-exploring Random Tree — two jobs, both honest about what RRT is for.

RRT is a *sampling* planner. It is fast, it finds a feasible path with probability 1 as
n -> infinity, and it makes no promise of optimality. That makes it the wrong tool for
"what is the cheapest fare" and the right tool for two other things:

1. `rrt_transit` — a feasible itinerary in a few milliseconds, grown by sampling random
   points in the city and steering the nearest tree node toward them, with a goal bias.
   Its cost can also be handed to A* as an `upper_bound`, which is the standard
   anytime pattern. Measured on 34 searches it never improved on A*'s own pruning —
   when RRT lands 40% above optimal there is nothing left for the bound to cut — so it
   is offered, benchmarked and reported rather than assumed to help.

2. `rrt_star_walk` — RRT* in continuous 2-D space for the walking connector between a
   street address and the stop you board at. Here the search space really is continuous,
   which is exactly where Dijkstra and A* have no graph to search.

Obstacle data is optional. `data/raw/osm/buildings.geojson` is read when present; without
it the tree is grown in open space, which is honestly reported as `OPEN_FIELD` rather than
pretending the walk went around anything.
"""
from __future__ import annotations

import heapq
import json
import math
import random
import time
from pathlib import Path

from src.config import RAW_OSM
from src.routing.graph import Edge, Path, TransitGraph
from src.tools.common import haversine_km
from src.routing.search import _finish, make_heuristic, PROFILES, State

BUILDINGS = RAW_OSM / "buildings.geojson"


# --------------------------------------------------------------- graph-based RRT
def rrt_transit(g: TransitGraph, start: list[tuple[str, float]], goals: set[str],
                objective: str = "fastest", max_iters: int = 1500, goal_bias: float = 0.20,
                step_edges: int = 6, seed: int = 7, deadline_s: float = 3.0) -> Path:
    """Goal-biased RRT over the transit graph.

    Sampling is geographic: most iterations draw a random stop from the corridor, the
    rest draw the destination itself. The tree node nearest the sample is extended toward
    it by one edge — repeated for `step_edges` so a single iteration advances the tree by
    a ride rather than a single hop.

    One deviation from textbook RRT, and it matters: the tree is kept over *physical
    stops*, not over full search states. On a discrete graph every state is a deterministic
    function of its parent, so a state-keyed tree saturates within ~50 nodes and stops
    exploring. Keying on the stop makes the tree monotone in coverage, which is what makes
    the planner converge.

    The result is *a* feasible itinerary, not the best one. Its job is to give A* an upper
    bound to prune against — and if it never reaches the goal it says so instead of
    pretending (`goal_hit = False`).
    """
    t0 = time.perf_counter()
    rng = random.Random(seed)
    w = PROFILES[objective]

    goals = {s for s in goals if s in g.stops}
    starts = [(s, c) for s, c in start if s in g.stops]
    if not goals or not starts or len(g.stops) < 2:
        return Path(algorithm="rrt", elapsed_ms=0.0)

    all_stops = list(g.stops)
    goal_coords = [g.coord(s) for s in goals]

    parent: dict[State, tuple[State, Edge] | None] = {}
    cost: dict[State, float] = {}
    tree: list[State] = []
    visited: set[str] = set()
    for sid, c in starts:
        st: State = (sid, "none", "none", 0, 0.0)
        parent[st] = None
        cost[st] = c
        tree.append(st)
        visited.add(sid)

    def near_goal(sid: str, radius_km: float = 0.25) -> bool:
        la, lo = g.coord(sid)
        return any(haversine_km(la, lo, gl, go) <= radius_km for gl, go in goal_coords)

    def add(state_from: State, st2: State, edge: Edge, ncost: float) -> None:
        edge.src = state_from[0]
        parent[st2] = (state_from, edge)
        cost[st2] = ncost
        tree.append(st2)
        visited.add(st2[0])

    expanded = 0
    for _ in range(max_iters):
        if time.perf_counter() - t0 > deadline_s:
            break
        expanded += 1
        target = (goal_coords[rng.randrange(len(goal_coords))] if rng.random() < goal_bias
                  else g.coord(all_stops[rng.randrange(len(all_stops))]))

        # try the nearest tree nodes in turn: a dead end must not stall the planner
        nearest = heapq.nsmallest(20, tree, key=lambda st: _sq(g.coord(st[0]), target))
        best = None
        for state in nearest:
            node, carrier, ticket, hops, km = state
            gcost = cost[state]
            cands: list[tuple[float, State, Edge, float]] = []
            if carrier == "none":
                for e in g.out.get(node, ()):
                    if e.to in visited:
                        continue
                    if e.mode == "walk":
                        st2: State = (e.to, "none", "none", 0, 0.0)
                        nc = gcost + w["minutes"] * e.minutes
                    else:
                        metro = e.mode == "metro"
                        st2 = (e.to, e.carrier, "metro" if metro else "bus", 0, 0.0)
                        nc = gcost + w["minutes"] * (2.0 if metro else 3.0)
                    cands.append((_sq(g.coord(e.to), target), st2, e, nc))
            else:
                for e in g.out.get(node, ()):
                    if e.mode == "walk" or e.carrier != carrier or e.to in visited:
                        continue
                    st2 = (e.to, carrier, ticket,
                           hops + 1 if e.mode == "metro" else 0,
                           km + e.km if e.mode == "bus" else 0.0)
                    cands.append((_sq(g.coord(e.to), target), st2, e,
                                  gcost + w["minutes"] * e.minutes))
                if rng.random() < 0.5:
                    off = (node, "none", "metro" if ticket == "metro" else "none", hops, 0.0)
                    if off not in parent:
                        cands.append((_sq(g.coord(node), target), off,
                                      Edge(to=node, mode="alight", carrier="none",
                                            minutes=0.0, km=0.0, label="alight"), gcost))
            if cands:
                c = min(cands, key=lambda x: x[0])
                if best is None or c[0] < best[0][0]:
                    best = (c, state)
        if best is None:
            break
        (_, st2, edge, ncost), state = best
        add(state, st2, edge, ncost)
        if st2[1] == "none":
            continue

        # a step, not a hop: keep riding the same vehicle toward the sample
        for _ in range(step_edges - 1):
            node2, car2, tick2, hop2, km2 = st2
            if st2[0] in goals or near_goal(st2[0]):
                break
            ahead = [(_sq(g.coord(e.to), target), e) for e in g.out.get(node2, ())
                     if e.mode != "walk" and e.carrier == car2 and e.to not in visited]
            if not ahead:
                break
            _, e = min(ahead, key=lambda x: x[0])
            st3 = (e.to, car2, tick2, hop2 + 1 if e.mode == "metro" else 0,
                   km2 + e.km if e.mode == "bus" else 0.0)
            add(st2, st3, e, cost[st2] + w["minutes"] * e.minutes)
            st2 = st3

        if st2[0] in goals or near_goal(st2[0]):
            # finishing short of the stop is only fair if we pay for the remaining walk
            rem_km = min(haversine_km(*g.coord(st2[0]), *gl) for gl in goal_coords)
            tail = w["minutes"] * rem_km / 4.8 * 60 * 1.25
            p = _finish(parent, st2, cost[st2] + tail, expanded, len(parent), "rrt", t0)
            p.generated = len(parent)
            p.goal_hit = True
            p.tail_min = round(tail, 2)
            p.notes = [] if st2[0] in goals else [
                f"finished {rem_km * 1000:.0f} m short of the destination stop "
                f"(walk time included in the reported cost)"]
            return p

    best_state, best_d = None, float("inf")
    for st in tree:
        if st[1] == "none":
            continue
        la, lo = g.coord(st[0])
        for gl, go in goal_coords:
            d = haversine_km(la, lo, gl, go)
            if d < best_d:
                best_d, best_state = d, st
    if best_state is not None:
        p = _finish(parent, best_state, cost[best_state], expanded, len(parent), "rrt", t0)
        p.goal_hit = False
        p.notes = [f"nearest approach {best_d:.2f} km from the destination stop"]
        return p
    return Path(algorithm="rrt", expanded=expanded, generated=len(parent),
                elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))


def _sq(a, b) -> float:
    return (a[0] - b[0]) ** 2 + ((a[1] - b[1]) * math.cos(math.radians(a[0]))) ** 2


# ------------------------------------------------------------- continuous-space RRT*
def load_obstacles(path: Path | None = None) -> list[list[tuple[float, float]]]:
    """Building footprints as lat/lon rings. Returns [] when the file is absent."""
    path = path or BUILDINGS
    if not path.exists():
        return []
    try:
        gj = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    rings = []
    for feat in gj.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") == "Polygon":
            for ring in geom["coordinates"][:1]:
                rings.append([(c[1], c[0]) for c in ring])
        elif geom.get("type") == "MultiPolygon":
            for poly in geom["coordinates"]:
                for ring in poly[:1]:
                    rings.append([(c[1], c[0]) for c in ring])
    return rings


def _cross(a, b, c) -> float:
    return (b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])


def _segments_cross(p1, p2, p3, p4) -> bool:
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _blocked(a, b, rings) -> bool:
    for ring in rings:
        n = len(ring)
        for i in range(n - 1):
            if _segments_cross(a, b, ring[i], ring[i + 1]):
                return True
    return False


def rrt_star_walk(start: tuple[float, float], goal: tuple[float, float],
                  obstacles: list[list[tuple[float, float]]] | None = None,
                  max_iters: int = 900, radius_deg: float = 0.0006,
                  seed: int = 3) -> dict:
    """RRT* in lat/lon space. Returns the polyline plus an honest data status.

    `radius_deg` is the rewire radius (~65 m). With no obstacle layer the tree still
    finds a near-straight connection and the caller is told so via `data_status`.
    """
    obstacles = obstacles or []
    rng = random.Random(seed)
    nodes = [start]
    parent = [None]
    cost = [0.0]

    def dist(a, b):
        return math.hypot(a[0] - b[0], (a[1] - b[1]) * math.cos(math.radians(a[0])))

    goal_reach = dist(start, goal)
    step = max(radius_deg, goal_reach / 12.0)

    for _ in range(max_iters):
        sample = goal if rng.random() < 0.15 else (
            rng.uniform(min(start[0], goal[0]) - 0.001, max(start[0], goal[0]) + 0.001),
            rng.uniform(min(start[1], goal[1]) - 0.001, max(start[1], goal[1]) + 0.001))
        i = min(range(len(nodes)), key=lambda k: dist(nodes[k], sample))
        base = nodes[i]
        d = dist(base, sample)
        if d < 1e-9:
            continue
        step_frac = min(1.0, step / d)
        new = (base[0] + (sample[0] - base[0]) * step_frac,
               base[1] + (sample[1] - base[1]) * step_frac)
        if _blocked(base, new, obstacles):
            continue

        # choose the cheapest nearby parent (RRT*), then rewire (RRT*)
        near = [k for k in range(len(nodes)) if dist(nodes[k], new) < radius_deg * 2.5]
        best_parent, best_cost = i, cost[i] + dist(base, new)
        for k in near:
            if _blocked(nodes[k], new, obstacles):
                continue
            c = cost[k] + dist(nodes[k], new)
            if c < best_cost:
                best_parent, best_cost = k, c
        nodes.append(new)
        parent.append(best_parent)
        cost.append(best_cost)
        new_idx = len(nodes) - 1
        for k in near:
            if k == best_parent:
                continue
            c = best_cost + dist(new, nodes[k])
            if c < cost[k] and not _blocked(new, nodes[k], obstacles):
                parent[k] = new_idx
                cost[k] = c

        if dist(new, goal) < step * 0.6 and not _blocked(new, goal, obstacles):
            nodes.append(goal)
            parent.append(new_idx)
            cost.append(best_cost + dist(new, goal))
            break

    # best path to the goal node (or the closest node if we never reached it)
    idx = len(nodes) - 1
    reached = dist(nodes[idx], goal) < 1e-6
    path = []
    while idx is not None:
        path.append(nodes[idx])
        idx = parent[idx]
    path.reverse()

    length_km = sum(dist(path[k], path[k + 1]) for k in range(len(path) - 1)) * 111.0
    straight_km = dist(start, goal) * 111.0
    return {
        "reached": bool(reached),
        "path": [(round(p[0], 6), round(p[1], 6)) for p in path],
        "length_km": round(max(length_km, straight_km), 3),
        "straight_line_km": round(straight_km, 3),
        "detour_ratio": round(max(length_km, straight_km) / straight_km, 3) if straight_km else 1.0,
        "tree_size": len(nodes),
        "obstacles": len(obstacles),
        "data_status": "OBSTACLE_AWARE" if obstacles else "OPEN_FIELD",
    }
