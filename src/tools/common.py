"""Shared helpers for the deterministic tools: db access, distance maths, place resolution.

Nothing in this module calls an LLM. Every number the agent reports comes from these
functions, which is what keeps fare arithmetic out of the language model.
"""
from __future__ import annotations

import functools
import math
import re
import sqlite3
from typing import Any

from rapidfuzz import fuzz, process

from src.config import DB_PATH

# Extra hand-written aliases for the ways students actually type place names.
MANUAL_ALIASES: dict[str, str] = {
    "dsu": "Dayananda Sagar University — Harohalli Campus",
    "dsu harohalli": "Dayananda Sagar University — Harohalli Campus",
    "dsu main campus": "Dayananda Sagar University — Harohalli Campus",
    "dsce": "Dayananda Sagar College of Engineering / DSU City Campus",
    "dsatm": "Dayananda Sagar Academy of Technology and Management",
    "majestic": "Kempegowda Bus Station",
    "kbs": "Kempegowda Bus Station",
    "btm": "BTM Layout",
    "silk institute": "Silk Institute",
    "lalbagh": "Lalbagh",
    "cubbon park": "Cubbon Park",
    "mg road": "MG Road",
    "electronic city": "Electronic City",
    "ecity": "Electronic City",
    "whitefield": "Whitefield",
    "jayanagar": "Jayanagara Bus Station",
    "jayanagara": "Jayanagara Bus Station",
    "banashankari": "Banashankari Bus Station",
    "bsk": "Banashankari Bus Station",
    "banashankari bsk": "Banashankari Bus Station",
    "hebbal": "Hebbala",
}

KIND_BONUS = {
    "campus": 6,
    "bmtc_stop": 5,
    "metro_station": 5,
    "osm_bus_stop": 3,
    "osm_metro_station": 5,
    "osm_campus": 4,
    "osm_place": 1,
    "place": 1,
}


# BMRCL line colours, used by the map legend, the Explore cards and every metro step.
# Kept here (not in the web layer) so the planner output and the HTTP contract cannot
# drift apart: the itinerary carries the colour, the UI only renders it.
METRO_LINE_COLOURS = {
    "Purple": "#8C2877",
    "Green": "#009933",
    "Yellow": "#FFDF00",
    "Pink": "#E75480",
}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


@functools.lru_cache(maxsize=1)
def _place_rows() -> list[tuple]:
    con = db()
    rows = [tuple(r) for r in con.execute(
        "SELECT place_id, name, norm_name, kind, lat, lon, stop_id, aliases, source, extra "
        "FROM places")]
    con.close()
    return rows


DENSITY_CELL = 0.02          # ~2.2 km grid cell at Bengaluru's latitude
DENSITY_RADIUS = 2.5         # km


@functools.lru_cache(maxsize=1)
def _density_grid() -> dict[tuple[int, int], int]:
    """How many indexed places sit in each coarse grid cell.

    This exists to break exact-name ties. Several BMTC stops share a name with a
    well-known locality; the real one sits in a dense part of the city, the duplicate is
    usually a mislabelled depot at the edge. Counting neighbours separates them without a
    hand-maintained alias list, and it uses only coordinates already in the database.
    """
    grid: dict[tuple[int, int], int] = {}
    for row in _place_rows():
        lat, lon = row[4], row[5]
        if lat is None or lon is None:
            continue
        key = (int(lat / DENSITY_CELL), int(lon / DENSITY_CELL))
        grid[key] = grid.get(key, 0) + 1
    return grid


def _density(lat: float | None, lon: float | None) -> int:
    if lat is None or lon is None:
        return 0
    grid = _density_grid()
    cy, cx = int(lat / DENSITY_CELL), int(lon / DENSITY_CELL)
    total = 0
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            total += grid.get((cy + dy, cx + dx), 0)
    return total


@functools.lru_cache(maxsize=1)
def _stop_points() -> list[tuple]:
    con = db()
    rows = [tuple(r) for r in con.execute(
        "SELECT stop_id, stop_name, stop_desc, zone_id, CAST(stop_lat AS REAL), CAST(stop_lon AS REAL) "
        "FROM stops")]
    con.close()
    return rows


@functools.lru_cache(maxsize=1)
def _metro_points(include_pending: bool = False) -> list[tuple]:
    """Metro stations that are physically open today.

    The Pink Line is in metro_stops for planning and fare lookups, but until it carries
    passengers it must never be offered as someone's nearest station.
    """
    con = db()
    clauses = "location_type='1'"
    if not include_pending:
        clauses += " AND COALESCE(service_status,'operational')='operational'"
    rows = [tuple(r) for r in con.execute(
        f"SELECT stop_id, stop_name, CAST(stop_lat AS REAL), CAST(stop_lon AS REAL) "
        f"FROM metro_stops WHERE {clauses}")]
    con.close()
    return rows


def nearest_bmtc_stop(lat: float, lon: float, max_km: float = 6.0) -> dict[str, Any] | None:
    best = None
    for stop_id, name, desc, zone, slat, slon in _stop_points():
        if slat is None or slon is None:
            continue
        d = haversine_km(lat, lon, slat, slon)
        if best is None or d < best["distance_km"]:
            best = {"stop_id": stop_id, "stop_name": name, "stop_desc": desc,
                    "zone_id": zone, "lat": slat, "lon": slon, "distance_km": round(d, 2)}
    if best and best["distance_km"] <= max_km:
        return best
    return None


def nearest_metro_station(lat: float, lon: float, max_km: float = 8.0,
                          include_pending: bool = False) -> dict[str, Any] | None:
    best = None
    for sid, name, slat, slon in _metro_points(include_pending=include_pending):
        d = haversine_km(lat, lon, slat, slon)
        if best is None or d < best["distance_km"]:
            best = {"station_id": sid, "station_name": name, "lat": slat, "lon": slon,
                    "distance_km": round(d, 2)}
    if best and best["distance_km"] <= max_km:
        return best
    return None


STOP_TOKENS = {
    "the", "a", "an", "to", "from", "and", "or", "of", "in", "at", "on", "for", "is",
    "it", "best", "route", "routes", "bus", "buses", "which", "what", "how", "do",
    "i", "get", "go", "goes", "going", "reach", "pass", "passes", "cheapest",
    "fastest", "quickest", "good", "nearest", "near", "me", "my", "tell", "show",
    "recommend", "should", "can", "could", "please", "want", "need", "there", "here",
    "monthly", "daily", "cheap", "better", "vs", "versus", "any",
}


def resolve_place(query: str, limit: int = 5) -> dict[str, Any]:
    """Turn a free-text place into a coordinate plus the nearest official GTFS stop.

    Strategy: exact manual alias -> exact name -> fuzzy match over 13k places, with a
    bonus for kinds that are useful for routing. If the best hit is an OSM feature
    without a stop id (a campus, a locality), snap to the nearest BMTC stop.
    """
    q = norm(query)
    if not q:
        return {"ok": False, "query": query, "error": "empty place name"}
    content_tokens = [t for t in q.split() if t not in STOP_TOKENS]
    if not content_tokens:
        return {"ok": False, "query": query, "error": f"'{query}' is not a place name"}
    q_set = set(content_tokens)

    rows = _place_rows()

    # 1. manual alias
    target = MANUAL_ALIASES.get(q)
    if target:
        q = norm(target)

    scored: dict[str, tuple[float, tuple]] = {}
    for row in rows:
        _pid, name, norm_name, kind, lat, lon, stop_id, aliases, _source, _extra = row
        score = fuzz.WRatio(q, norm_name)
        if norm_name == q:
            score = 100.0
        elif norm_name.startswith(q) or q in norm_name.split():
            score = max(score, 92.0)
        if aliases and q in norm(aliases):
            score = max(score, 98.0)
        # How busy the surrounding area is. This is what tells the real Koramangala
        # (437 indexed places within ~2.5 km) from a mislabelled BMTC stop of the same
        # name 40 km away in a field (29). Without it the kind bonus alone wins.
        score += 4.0 * min(4.0, math.log10(1 + _density(lat, lon)))
        score += KIND_BONUS.get(kind, 0)
        key = f"{norm_name}|{round(lat or 0, 3)}"
        if key not in scored or score > scored[key][0]:
            scored[key] = (score, row)

    # Break exact-name ties by neighbourhood density before anything else, so that a
    # duplicate stop name in an empty corner cannot outrank the real locality.
    def _sort_key(item: tuple[float, tuple]) -> tuple[float, int]:
        score, row = item
        return (-score, -_density(row[4], row[5]))

    ranked = sorted(scored.values(), key=_sort_key)
    # Guard against fuzzy noise: a hit must share a content token with the query,
    # or be a near-exact string match (typo tolerance for single-word inputs).
    kept = []
    for score, row in ranked:
        name_tokens = set(norm(row[1]).split())
        if q_set & name_tokens:
            kept.append((score, row))
        elif len(content_tokens) == 1 and len(content_tokens[0]) >= 5 and score >= 96:
            kept.append((score, row))
    ranked = kept
    candidates = []
    for score, row in ranked[:limit]:
        _pid, name, norm_name, kind, lat, lon, stop_id, aliases, source, extra = row
        cand = {
            "name": name, "kind": kind, "confidence": round(min(score, 100.0), 1),
            "lat": lat, "lon": lon, "stop_id": stop_id, "source": source,
        }
        if stop_id and kind == "bmtc_stop":
            for sid, sname, sdesc, zone, slat, slon in _stop_points():
                if sid == stop_id:
                    cand.update(stop_desc=sdesc, zone_id=zone)
                    break
        candidates.append(cand)

    if not candidates:
        return {"ok": False, "query": query, "error": f"no place matched '{query}'"}

    best = candidates[0]

    # 2. snap non-stop features (campuses, localities, POIs) to real stops
    if not best.get("stop_id") or best["kind"] not in ("bmtc_stop", "metro_station"):
        if best.get("lat") is not None:
            near = nearest_bmtc_stop(best["lat"], best["lon"])
            if near:
                best["snapped"] = True
                best["stop_id"] = near["stop_id"]
                best["stop_name"] = near["stop_name"]
                best["stop_desc"] = near["stop_desc"]
                best["zone_id"] = near["zone_id"]
                best["snap_distance_km"] = near["distance_km"]
                best["feature_lat"] = best["lat"]
                best["feature_lon"] = best["lon"]
                best["lat"], best["lon"] = near["lat"], near["lon"]
        near_metro = nearest_metro_station(best.get("feature_lat") or best.get("lat") or 0,
                                           best.get("feature_lon") or best.get("lon") or 0)
        if near_metro:
            best["nearest_metro"] = near_metro

    best["alternatives"] = [
        {"name": c["name"], "kind": c["kind"], "confidence": c["confidence"],
         "stop_id": c.get("stop_id")}
        for c in candidates[1:4]
    ]
    best["ok"] = True
    best["query"] = query
    return best


def route_info(route_id: str) -> dict[str, Any]:
    con = db()
    row = con.execute(
        "SELECT r.route_id, r.route_short_name, r.route_long_name, "
        "(SELECT n_trips FROM route_stats WHERE route_id=r.route_id) AS n_trips "
        "FROM routes r WHERE r.route_id=?", (route_id,)).fetchone()
    con.close()
    if not row:
        return {"route_id": route_id, "route_short_name": route_id, "route_long_name": ""}
    d = dict(row)
    d["is_ac"] = is_ac_route(d.get("route_short_name") or "", d.get("route_long_name") or "")
    return d


_AC_PATTERNS = [re.compile(p, re.I) for p in (
    r"^V[\s-]?\d", r"^VJ", r"^KIA", r"^K\.I\.A", r"^G-?\d", r"\bAC\b", r"VAJRA", r"AIRPORT",
)]


def is_ac_route(short_name: str, long_name: str = "") -> bool:
    text = f"{short_name} {long_name}"
    return any(p.search(text) for p in _AC_PATTERNS)


def trip_serves(route_id: str, from_stop: str, to_stop: str) -> bool:
    """True if some real trip on this route visits from_stop before to_stop.

    route_stops collapses trip variants, so a candidate is only trustworthy once a
    physical trip is confirmed to make the run.
    """
    con = db()
    row = con.execute("""
        SELECT 1
        FROM trips t
        JOIN trip_stops a ON a.trip = (SELECT k FROM trip_key WHERE trip_id = t.trip_id)
                         AND a.stop = (SELECT k FROM stop_key WHERE stop_id = ?)
        JOIN trip_stops b ON b.trip = a.trip AND b.stop = (SELECT k FROM stop_key WHERE stop_id = ?)
                         AND b.seq > a.seq
        WHERE t.route_id = ?
        LIMIT 1
    """, (from_stop, to_stop, route_id)).fetchone()
    con.close()
    return row is not None


def money(x: float | None) -> str:
    if x is None:
        return "n/a"
    return f"Rs {x:,.0f}" if float(x).is_integer() else f"Rs {x:,.2f}"
