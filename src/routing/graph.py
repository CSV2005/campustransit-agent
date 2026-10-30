"""Multimodal transit graph for the graph-search routers.

The agent has two families of routing code and they answer different questions:

    src/tools/route_tool.py    "which route numbers go from A to B"   (SQL, enumerative)
    src/routing/*              "what is the optimal path, and can you prove it"  (search)

This module builds the object the second family searches over: a *time-expanded corridor
graph*. Every node is a physical stop, every edge is either a scheduled ride on one vehicle
or a short walk, and the search state carries the vehicle you are on plus the fare counter
for the ticket in your hand. Without that state a search would happily "transfer" without
paying a second fare, so the state is what makes the arithmetic correct.

Edge weights come from real GTFS data (`route_segment` / `metro_segment`, built by
`src/ingest_routing_graph.py`): mean scheduled run time and stop-to-stop distance across
every trip that serves the segment. Nothing here invents a timetable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from src.tools.common import db, haversine_km

BOARD_PENALTY_MIN = 3.0       # bus: wait + board.  metro: platform + wait
METRO_BOARD_PENALTY_MIN = 2.0
TRANSFER_PENALTY_MIN = 4.0    # alight, find the next bay/platform, wait again
MAX_WALK_M = 450              # longer than this is not a connection, it is a walk
MAX_EDGE_KMH = 90.0           # fasted plausible vehicle speed; see _plausible()

MODE_LABEL = {"bus": "BMTC bus", "metro": "Namma Metro", "walk": "walk"}

METRO_LINES = ("GREEN", "PURPLE", "YELLOW", "PINK")


@dataclass(slots=True)
class Edge:
    to: str
    mode: str                  # bus | metro | walk
    carrier: str               # route_id, line_id, or "walk"
    minutes: float
    km: float
    label: str = ""
    src: str = ""              # filled in during path reconstruction

    @property
    def is_ride(self) -> bool:
        return self.mode in ("bus", "metro")


@dataclass
class Path:
    """A concrete itinerary: the ordered edges plus the search statistics."""
    edges: list[Edge] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    cost: float = 0.0
    minutes: float = 0.0
    boardings: int = 0
    expanded: int = 0
    generated: int = 0
    algorithm: str = ""
    elapsed_ms: float = 0.0
    goal_hit: bool = False
    tail_min: float = 0.0        # walk left over when a planner stopped short of the stop
    notes: list[str] = field(default_factory=list)

    @property
    def reached(self) -> bool:
        return bool(self.nodes)


class TransitGraph:
    def __init__(self) -> None:
        self.stops: dict[str, dict[str, Any]] = {}
        self.out: dict[str, list[Edge]] = {}
        self.carriers_at: dict[str, list[str]] = {}
        self.max_speed_kmh: float = 25.0
        self.n_ride_edges: int = 0
        self.n_walk_edges: int = 0

    def add_stop(self, sid: str, name: str, lat: float, lon: float, mode: str) -> None:
        if lat is None or lon is None or sid in self.stops:
            return
        self.stops[sid] = {"stop_id": sid, "name": name, "lat": float(lat),
                           "lon": float(lon), "mode": mode}
        self.out.setdefault(sid, [])

    def add(self, src: str, e: Edge) -> None:
        if src not in self.stops or e.to not in self.stops:
            return
        self.out.setdefault(src, []).append(e)
        if e.is_ride:
            lst = self.carriers_at.setdefault(src, [])
            if e.carrier not in lst:
                lst.append(e.carrier)
            self.n_ride_edges += 1
            if e.minutes > 0.05 and e.km > 0.15:
                self.max_speed_kmh = max(self.max_speed_kmh, e.km / (e.minutes / 60.0))
        else:
            self.n_walk_edges += 1

    def coord(self, sid: str) -> tuple[float, float]:
        s = self.stops[sid]
        return s["lat"], s["lon"]

    def dist_km(self, a: str, b: str) -> float:
        la, lo = self.coord(a)
        lb, lob = self.coord(b)
        return haversine_km(la, lo, lb, lob)


# --------------------------------------------------------------------- assembly
def _bbox(pts: Iterable[tuple[float, float]], pad_km: float):
    pts = list(pts)
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    dlat = pad_km / 111.0
    dlon = pad_km / (111.0 * max(0.2, math.cos(math.radians(sum(lats) / len(lats)))))
    return min(lats) - dlat, max(lats) + dlat, min(lons) - dlon, max(lons) + dlon


def _ellipse_ok(o, d, p, factor: float, pad_km: float) -> bool:
    """Keep stops on a plausible detour between origin and destination."""
    od = haversine_km(o[0], o[1], d[0], d[1])
    if od < 1.0:
        return haversine_km(o[0], o[1], p[0], p[1]) <= 6.0
    return (haversine_km(o[0], o[1], p[0], p[1])
            + haversine_km(p[0], p[1], d[0], d[1])) <= factor * od + pad_km


def build_graph(origin: tuple[float, float], destination: tuple[float, float],
                include_pink: bool = False, factor: float = 1.24, pad_km: float = 2.0,
                max_stops: int = 4000, max_parallel: int = 2,
                modes: "set[str] | None" = None) -> TransitGraph:
    """Build the corridor graph.

    `max_parallel` is the important knob. Bengaluru runs ~20 bus routes over the same
    consecutive stop pair, so the raw corridor has 53,850 ride edges over only ~2,600
    distinct links — a 20x redundancy that makes every search expand the whole graph.
    Keeping the `max_parallel` fastest carriers per link removes the duplicates without
    changing which journeys exist: the dropped carriers are slower on every one of those
    links. The reduction is reported so the benchmark can prove it did not change answers.
    """
    # `modes` restricts which *ride* edges exist. Nodes and walk edges are always
    # built — snapping to the nearest stop and walking between stops are part of every
    # mode, including "metro only" (walk to the station, ride, walk from it).
    allowed = {"bus", "metro"} if modes is None else set(modes)
    g = TransitGraph()
    con = db()
    lat0, lat1, lon0, lon1 = _bbox([origin, destination], pad_km + 1.0)

    for r in con.execute(
            "SELECT stop_id, stop_name, CAST(stop_lat AS REAL) l, CAST(stop_lon AS REAL) n "
            "FROM stops WHERE CAST(stop_lat AS REAL) BETWEEN ? AND ? "
            "AND CAST(stop_lon AS REAL) BETWEEN ? AND ?",
            (lat0, lat1, lon0, lon1)):
        g.add_stop(r["stop_id"], r["stop_name"], r["l"], r["n"], "bus")

    pink = "" if include_pink else " AND COALESCE(service_status,'operational')='operational'"
    for r in con.execute(
            "SELECT stop_id, stop_name, CAST(stop_lat AS REAL) l, CAST(stop_lon AS REAL) n "
            "FROM metro_stops WHERE location_type='1'" + pink):
        g.add_stop(r["stop_id"], r["stop_name"], r["l"], r["n"], "metro")

    keep = {sid for sid, s in g.stops.items()
            if _ellipse_ok(origin, destination, (s["lat"], s["lon"]), factor, pad_km)}
    for sid in list(g.stops):
        if sid not in keep:
            del g.stops[sid]
    if len(g.stops) > max_stops:
        def _score(sid: str) -> float:
            la, lo = g.coord(sid)
            return haversine_km(*origin, la, lo) + haversine_km(la, lo, *destination)
        for sid in sorted(g.stops, key=_score)[max_stops:]:
            del g.stops[sid]

    ids = list(g.stops)
    if not ids:
        con.close()
        return g

    con.execute("DROP TABLE IF EXISTS temp.tmp_corridor")
    con.execute("CREATE TEMP TABLE tmp_corridor(stop_id TEXT PRIMARY KEY)")
    con.executemany("INSERT INTO tmp_corridor VALUES (?)", [(s,) for s in ids])

    rows = con.execute("""
        SELECT s.from_stop, s.to_stop, s.route_id, s.km, s.minutes, r.route_short_name
        FROM route_segment s
        JOIN tmp_corridor c ON c.stop_id = s.from_stop
        LEFT JOIN routes r ON r.route_id = s.route_id
        WHERE s.minutes > 0 AND s.km IS NOT NULL AND s.km >= 0
    """).fetchall()
    dropped = 0
    links: dict[tuple[str, str], list[tuple[float, str, float, str]]] = {}
    for r in rows:
        km, mins = max(0.0, float(r["km"])), max(0.25, float(r["minutes"]))
        if not _plausible(km, mins):
            dropped += 1
            continue
        label = r["route_short_name"] or str(r["route_id"])
        links.setdefault((r["from_stop"], r["to_stop"]), []).append(
            (mins, str(r["route_id"]), km, label))
    pruned = 0
    for (a, b), opts in links.items():
        opts.sort()
        if "bus" in allowed:
            for mins, rid, km, label in opts[:max_parallel]:
                g.add(a, Edge(to=b, mode="bus", carrier=rid, minutes=mins, km=km,
                              label=label))
        pruned += max(0, len(opts) - max_parallel)
    g.dropped_implausible = dropped
    g.pruned_parallel_carriers = pruned
    g.raw_ride_edges = len(rows) - dropped

    plat_to_station = {r["stop_id"]: r["parent_station"] for r in con.execute(
        "SELECT stop_id, parent_station FROM metro_stops "
        "WHERE parent_station IS NOT NULL AND parent_station<>''")}
    line_name = {r["route_id"]: (r["route_short_name"] or r["route_id"])
                 for r in con.execute("SELECT route_id, route_short_name FROM metro_routes")}
    mrows = con.execute("SELECT from_stop, to_stop, route_id, km, minutes FROM metro_segment "
                        "WHERE minutes > 0").fetchall()
    for r in (mrows if "metro" in allowed else []):
        a = plat_to_station.get(r["from_stop"], r["from_stop"])
        b = plat_to_station.get(r["to_stop"], r["to_stop"])
        if a not in g.stops or b not in g.stops:
            continue
        g.add(a, Edge(to=b, mode="metro", carrier=str(r["route_id"]),
                      minutes=max(0.3, float(r["minutes"])),
                      km=max(0.0, float(r["km"])),
                      label=line_name.get(r["route_id"], r["route_id"])))
    con.close()

    _add_walk_edges(g, MAX_WALK_M)
    return g


def _plausible(km: float, minutes: float) -> bool:
    """Reject GTFS segments that no vehicle could run.

    370 of BMTC's 205,354 segments link two stops 50-64 km apart in under a minute — a
    duplicated stop code in the feed, not a bus. They are harmless for the enumerative
    router but fatal for A*: a single 7,000 km/h edge raises `max_speed_kmh` and makes the
    heuristic vacuous, which turns A* back into Dijkstra.
    """
    if km <= 0 or minutes <= 0:
        return km <= 0.05
    return km / (minutes / 60.0) <= MAX_EDGE_KMH and km < 80.0


def _add_walk_edges(g: TransitGraph, max_m: int) -> None:
    """Connect stops a short walk apart, using a cheap spatial grid."""
    cell = 0.005                                  # ~550 m at Bengaluru's latitude
    grid: dict[tuple[int, int], list[str]] = {}
    for sid, s in g.stops.items():
        grid.setdefault((int(s["lat"] / cell), int(s["lon"] / cell)), []).append(sid)

    for (gy, gx), members in grid.items():
        near: list[str] = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                near.extend(grid.get((gy + dy, gx + dx), ()))
        for a in members:
            for b in near:
                if a == b:
                    continue
                d_km = g.dist_km(a, b)
                d_m = d_km * 1000
                if d_m > max_m:
                    continue
                if g.stops[a]["mode"] != "metro" and g.stops[b]["mode"] != "metro" and d_m > 300:
                    continue
                g.add(a, Edge(to=b, mode="walk", carrier="walk",
                              minutes=d_km / 4.8 * 60 * 1.25, km=d_km, label="walk"))


def snap_stops(lat: float, lon: float, radius_km: float = 1.0, limit: int = 5) -> list[str]:
    """Entry points into the graph: the stops nearest a coordinate."""
    con = db()
    out: list[tuple[float, str]] = []
    box = (lat - radius_km / 111, lat + radius_km / 111,
           lon - radius_km / 80, lon + radius_km / 80)
    for r in con.execute(
            "SELECT stop_id, CAST(stop_lat AS REAL) l, CAST(stop_lon AS REAL) n FROM stops "
            "WHERE CAST(stop_lat AS REAL) BETWEEN ? AND ? AND CAST(stop_lon AS REAL) BETWEEN ? AND ?",
            box):
        d = haversine_km(lat, lon, r["l"], r["n"])
        if d <= radius_km:
            out.append((d, r["stop_id"]))
    for r in con.execute(
            "SELECT stop_id, CAST(stop_lat AS REAL) l, CAST(stop_lon AS REAL) n FROM metro_stops "
            "WHERE location_type='1' AND COALESCE(service_status,'operational')='operational' "
            "AND CAST(stop_lat AS REAL) BETWEEN ? AND ? AND CAST(stop_lon AS REAL) BETWEEN ? AND ?",
            box):
        d = haversine_km(lat, lon, r["l"], r["n"])
        if d <= radius_km:
            out.append((d, r["stop_id"]))
    con.close()
    out.sort()
    return [sid for _, sid in out[:limit]]
