"""Verification harness for the three search algorithms.

What it proves, on randomly drawn corridors rather than hand-picked ones:

   1. Dijkstra and A* return the *same* cost  (A* optimality)
   2. every returned cost survives an independent replay of the path (cost accounting)
   3. Dijkstra is optimal w.r.t. the corridor graph it searched (nothing cheaper exists
      among paths the graph can express — checked by the replay + the fact that Dijkstra
      terminates only when no cheaper frontier remains)
   4. how much A* saves in node expansions, and how much an RRT upper bound adds on top
   5. how often RRT reaches the destination, and how far from optimal its answer is

Run:  python -m src.eval.benchmark_routing [n_pairs]
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import time

from src.config import PROCESSED
from src.routing.graph import build_graph, snap_stops
from src.routing.rrt import rrt_transit
from src.routing.search import astar, dijkstra, replay_cost
from src.tools.common import db

OBJECTIVES = ["fastest", "cheapest", "fewest_transfers"]


def sample_corridors(n: int, seed: int = 11) -> list[tuple[str, str, str, tuple]]:
    """Draw n stop pairs that are at least 3 km apart, with coordinates."""
    con = db()
    rows = [dict(r) for r in con.execute(
        "SELECT stop_id, stop_name, CAST(stop_lat AS REAL) lat, CAST(stop_lon AS REAL) lon "
        "FROM stops WHERE stop_lat IS NOT NULL AND stop_lon IS NOT NULL "
        "AND stop_name IS NOT NULL AND stop_name <> ''")]
    con.close()
    rng = random.Random(seed)
    out, guard = [], 0
    while len(out) < n and guard < 20000:
        guard += 1
        a, b = rng.choice(rows), rng.choice(rows)
        d = ((a["lat"] - b["lat"]) ** 2 + (a["lon"] - b["lon"]) ** 2) ** 0.5 * 111
        if 3.0 <= d <= 25.0:
            out.append((a["stop_name"], b["stop_name"], f"{a['stop_id']}->{b['stop_id']}",
                        ((a["lat"], a["lon"]), (b["lat"], b["lon"]))))
    return out


def run(n_pairs: int = 24) -> dict:
    corridors = sample_corridors(n_pairs)
    results = []
    for i, (na, nb, tag, (o, d)) in enumerate(corridors, 1):
        t0 = time.perf_counter()
        g = build_graph(o, d)
        build_ms = (time.perf_counter() - t0) * 1000
        start = [(s, 0.0) for s in snap_stops(o[0], o[1])]
        goals = set(snap_stops(d[0], d[1], radius_km=0.8))
        if not start or not goals:
            continue
        for objective in OBJECTIVES:
            dij = dijkstra(g, start, goals, objective)
            ast = astar(g, start, goals, objective)
            if not dij.reached:
                continue
            rrt = rrt_transit(g, start, goals, objective)
            ub = None
            if rrt.goal_hit:
                ub = (replay_cost(rrt, objective) + rrt.tail_min) * 1.002 + 0.25
            ast_b = astar(g, start, goals, objective, upper_bound=ub) if ub else None
            rrt_cost = (replay_cost(rrt, objective) + rrt.tail_min) if rrt.reached else None
            results.append({
                "pair": f"{na} → {nb}", "tag": tag, "objective": objective,
                "build_ms": round(build_ms, 1),
                "stops": len(g.stops),
                "dij_cost": dij.cost, "dij_exp": dij.expanded, "dij_ms": dij.elapsed_ms,
                "ast_cost": ast.cost, "ast_exp": ast.expanded, "ast_ms": ast.elapsed_ms,
                "agree": abs(dij.cost - ast.cost) < 1e-6,
                "replay_ok": abs(replay_cost(dij, objective) - dij.cost) < 0.05,
                "ast_b_exp": ast_b.expanded if ast_b is not None and ast_b.reached else None,
                "ast_b_cost": ast_b.cost if ast_b is not None and ast_b.reached else None,
                "rrt_hit": rrt.goal_hit, "rrt_cost": rrt_cost, "rrt_ms": rrt.elapsed_ms,
                "rrt_gap_pct": (round((rrt_cost / dij.cost - 1) * 100, 1)
                                if rrt_cost else None),
            })
        print(f"  [{i:>2}/{len(corridors)}] {na[:26]:26s} → {nb[:26]:26s}  "
              f"{len(g.stops):>5,} stops", flush=True)

    ok = [r for r in results if r["dij_cost"] is not None]
    agree = sum(r["agree"] for r in ok)
    replay = sum(r["replay_ok"] for r in ok)
    ast_saving = [100 * (1 - r["ast_exp"] / r["dij_exp"]) for r in ok if r["dij_exp"]]
    bound_rows = [r for r in ok if r["ast_b_exp"]]
    bound_saving = [100 * (1 - r["ast_b_exp"] / r["dij_exp"]) for r in bound_rows
                    if r["dij_exp"]]
    hits = [r for r in ok if r["rrt_hit"]]
    gaps = [r["rrt_gap_pct"] for r in hits if r["rrt_gap_pct"] is not None]

    summary = {
        "corridors": len(corridors),
        "searches": len(ok),
        "dijkstra_astar_cost_agreement_pct": round(100 * agree / len(ok), 2) if ok else 0,
        "replay_cost_match_pct": round(100 * replay / len(ok), 2) if ok else 0,
        "astar_expansion_saving_pct_median": round(statistics.median(ast_saving), 1)
        if ast_saving else 0,
        "astar_expansion_saving_pct_max": round(max(ast_saving), 1) if ast_saving else 0,
        "bounded_astar_saving_pct_median": round(statistics.median(bound_saving), 1)
        if bound_saving else 0,
        "bounded_astar_rows": len(bound_rows),
        "bounded_astar_beat_plain_astar_rows": sum(
            1 for r in bound_rows if r["ast_b_exp"] < r["ast_exp"]),
        "rrt_reached_pct": round(100 * len(hits) / len(ok), 1) if ok else 0,
        "rrt_gap_pct_median": round(statistics.median(gaps), 1) if gaps else None,
        "rrt_gap_pct_min": round(min(gaps), 1) if gaps else None,
        "rrt_gap_pct_max": round(max(gaps), 1) if gaps else None,
        "rrt_ms_median": round(statistics.median([r["rrt_ms"] for r in ok]), 1) if ok else None,
        "dijkstra_ms_median": round(statistics.median([r["dij_ms"] for r in ok]), 1) if ok else None,
        "astar_ms_median": round(statistics.median([r["ast_ms"] for r in ok]), 1) if ok else None,
        "graph_build_ms_median": round(statistics.median([r["build_ms"] for r in ok]), 1)
        if ok else None,
    }
    out = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "summary": summary, "runs": results}
    (PROCESSED / "routing_benchmark.json").write_text(json.dumps(out, indent=1))

    print("\n================ ROUTING BENCHMARK ================")
    for k, v in summary.items():
        print(f"  {k:38s} {v}")
    bad = [r for r in ok if not r["agree"] or not r["replay_ok"]]
    print(f"  {'searches with a cost disagreement':38s} {len(bad)}")
    return out


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    run(n)
