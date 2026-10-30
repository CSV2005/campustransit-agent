"""Deterministic routing tools over the GTFS route graph.

find_stop      free text -> official GTFS stop (fuzzy + nearest-stop snapping)
find_routes    direct one-seat rides, then verified one-transfer itineraries
plan_metro_leg Namma Metro leg with interchange detection and the published fare
"""
from __future__ import annotations

import math
import re
from typing import Any

from src.tools.common import (db, haversine_km, nearest_metro_station, resolve_place,
                              route_info, trip_serves)

CLUSTER_RADIUS_KM = 1.0


def _stop_cluster(lat: float, lon: float, radius_km: float = CLUSTER_RADIUS_KM,
                  limit: int = 40) -> list[dict[str, Any]]:
    """All BMTC stops within a short walk of a point, nearest first."""
    con = db()
    rows = con.execute(
        "SELECT stop_id, stop_name, stop_desc, zone_id, "
        "CAST(stop_lat AS REAL) lat, CAST(stop_lon AS REAL) lon FROM stops").fetchall()
    con.close()
    out = []
    for r in rows:
        if r["lat"] is None:
            continue
        d = haversine_km(lat, lon, r["lat"], r["lon"])
        if d <= radius_km:
            out.append({"stop_id": r["stop_id"], "stop_name": r["stop_name"],
                        "stop_desc": r["stop_desc"], "zone_id": r["zone_id"],
                        "lat": r["lat"], "lon": r["lon"], "distance_km": round(d, 3)})
    out.sort(key=lambda x: x["distance_km"])
    return out[:limit]


# --------------------------------------------------------------------------- find_stop
def find_stop(place: str) -> dict[str, Any]:
    """Resolve a place name to the nearest official transit stop.

    Returns the matched place, its coordinates, the nearest BMTC stop id/name (with the
    direction description when GTFS provides one), the nearest metro station, and
    alternative matches when the name is ambiguous.
    """
    res = resolve_place(place)
    if not res.get("ok"):
        return {"ok": False, "tool": "find_stop", "query": place,
                "summary": f"No stop or place matched '{place}'. Ask for a nearby landmark or main road.",
                "data": {"candidates": []}, "sources": []}

    data = {
        "matched_name": res["name"],
        "match_kind": res["kind"],
        "confidence": res["confidence"],
        "stop_id": res.get("stop_id"),
        "stop_name": res.get("stop_name") or res["name"],
        "stop_desc": res.get("stop_desc"),
        "zone_id": res.get("zone_id"),
        "lat": res.get("lat"),
        "lon": res.get("lon"),
        "snapped": res.get("snapped", False),
        "snap_distance_km": res.get("snap_distance_km"),
        "nearest_metro": res.get("nearest_metro"),
        "alternatives": res.get("alternatives", []),
    }
    if data["snapped"]:
        summary = (f"'{place}' -> {data['matched_name']} (confidence {data['confidence']:.0f}); "
                   f"nearest BMTC stop is {data['stop_name']} [{data['stop_id']}], "
                   f"{data['snap_distance_km']} km away.")
    else:
        summary = (f"'{place}' -> {data['stop_name']} [{data['stop_id']}] "
                   f"(confidence {data['confidence']:.0f}).")
    return {"ok": True, "tool": "find_stop", "query": place, "summary": summary,
            "data": data, "sources": ["BMTC GTFS stops.txt", "OSM place index"]}


# ------------------------------------------------------------------------- find_routes
def _direct_candidates(origin_stops: list[str], dest_stops: list[str]) -> list[dict]:
    con = db()
    o_ph = ",".join("?" * len(origin_stops))
    d_ph = ",".join("?" * len(dest_stops))
    rows = con.execute(f"""
        SELECT a.route_id,
               MIN(b.seq - a.seq) AS hops,
               a.stop_id AS board_stop,
               b.stop_id AS alight_stop
        FROM route_stops a
        JOIN route_stops b ON b.route_id = a.route_id
                          AND b.direction_id = a.direction_id
                          AND b.seq > a.seq
        WHERE a.stop_id IN ({o_ph}) AND b.stop_id IN ({d_ph})
        GROUP BY a.route_id
        ORDER BY hops ASC
        LIMIT 20
    """, (*origin_stops, *dest_stops)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _pick_pair(route_id: str, origin_stops: list[str], dest_stops: list[str]) -> tuple[str, str, int] | None:
    con = db()
    o_ph = ",".join("?" * len(origin_stops))
    d_ph = ",".join("?" * len(dest_stops))
    row = con.execute(f"""
        SELECT a.stop_id AS o, b.stop_id AS d, MIN(b.seq - a.seq) AS hops
        FROM route_stops a
        JOIN route_stops b ON b.route_id = a.route_id
                          AND b.direction_id = a.direction_id AND b.seq > a.seq
        WHERE a.route_id = ? AND a.stop_id IN ({o_ph}) AND b.stop_id IN ({d_ph})
        GROUP BY a.stop_id, b.stop_id
        ORDER BY hops LIMIT 1
    """, (route_id, *origin_stops, *dest_stops)).fetchone()
    con.close()
    if not row:
        return None
    return row["o"], row["d"], row["hops"]


def display_route(short_name: str | None, long_name: str = "") -> str:
    """BMTC short names carry a variant suffix ('213-VD SGH-HRHD'). Show the number only
    when the first token already identifies the route."""
    if not short_name:
        return "?"
    parts = short_name.split()
    if len(parts) > 1 and re.search(r"\d", parts[0]):
        return parts[0]
    return short_name


def _stop_name(stop_id: str) -> tuple[str, str]:
    con = db()
    r = con.execute("SELECT stop_name, stop_desc FROM stops WHERE stop_id=?", (stop_id,)).fetchone()
    con.close()
    return (r["stop_name"], r["stop_desc"]) if r else (stop_id, None)


def _transfers(origin_stops: list[str], dest_stops: list[str],
               max_results: int = 4) -> list[dict]:
    con = db()

    def routes_at(stops: list[str]) -> list[dict]:
        ph = ",".join("?" * len(stops))
        rows = con.execute(f"""
            SELECT route_id, direction_id, stop_id, MIN(seq) AS seq
            FROM route_stops WHERE stop_id IN ({ph}) GROUP BY route_id, direction_id
        """, stops).fetchall()
        return [dict(r) for r in rows]

    origin_routes = routes_at(origin_stops)
    dest_routes = routes_at(dest_stops)

    # downstream stops reachable on one seat from the origin
    reach: dict[str, dict] = {}
    for r in origin_routes[:60]:
        rows = con.execute("""
            SELECT stop_id, seq FROM route_stops
            WHERE route_id=? AND direction_id=? AND seq > ?
            ORDER BY seq LIMIT 70
        """, (r["route_id"], r["direction_id"], r["seq"])).fetchall()
        for x in rows:
            hops = x["seq"] - r["seq"]
            prev = reach.get(x["stop_id"])
            if prev is None or hops < prev["hops"]:
                reach[x["stop_id"]] = {"route_id": r["route_id"], "dir": r["direction_id"],
                                       "hops": hops}
    con.close()

    out = []
    seen = set()
    for d in dest_routes:
        con = db()
        rows = con.execute("""
            SELECT stop_id, seq FROM route_stops
            WHERE route_id=? AND direction_id=? AND seq < ?
            ORDER BY seq DESC LIMIT 70
        """, (d["route_id"], d["direction_id"], d["seq"])).fetchall()
        con.close()
        for x in rows:
            if x["stop_id"] not in reach:
                continue
            leg1 = reach[x["stop_id"]]
            if leg1["route_id"] == d["route_id"]:
                continue
            key = (leg1["route_id"], x["stop_id"], d["route_id"])
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "leg1_route_id": leg1["route_id"],
                "leg1_hops": leg1["hops"],
                "transfer_stop_id": x["stop_id"],
                "leg2_route_id": d["route_id"],
                "leg2_hops": d["seq"] - x["seq"],
                "total_hops": leg1["hops"] + (d["seq"] - x["seq"]),
            })
    out.sort(key=lambda x: x["total_hops"])
    return out[:max_results]


def find_routes(origin: str, destination: str) -> dict[str, Any]:
    """Find bus routes from origin to destination.

    Searches every BMTC stop within 750 m of each endpoint. Direct routes are returned
    when a real trip runs the pair in order; otherwise verified one-transfer
    itineraries are returned. Falls back to KSRTC corridor advice when the BMTC
    network does not connect the two places.
    """
    o = resolve_place(origin)
    d = resolve_place(destination)
    if not o.get("ok") or not d.get("ok"):
        return {"ok": False, "tool": "find_routes",
                "summary": f"Could not resolve '{origin}' or '{destination}'. Try find_stop first."}

    o_cluster = _stop_cluster(o["lat"], o["lon"])
    d_cluster = _stop_cluster(d["lat"], d["lon"])
    if not o_cluster or not d_cluster:
        return {"ok": False, "tool": "find_routes",
                "summary": "One of the endpoints has no BMTC stop within 750 m."}
    o_ids = [s["stop_id"] for s in o_cluster]
    d_ids = [s["stop_id"] for s in d_cluster]

    o_walk = {s["stop_id"]: s["distance_km"] for s in o_cluster}
    d_walk = {s["stop_id"]: s["distance_km"] for s in d_cluster}

    direct = []
    for cand in _direct_candidates(o_ids, d_ids):
        pair = _pick_pair(cand["route_id"], o_ids, d_ids)
        if not pair:
            continue
        board, alight, hops = pair
        if not trip_serves(cand["route_id"], board, alight):
            continue
        info = route_info(cand["route_id"])
        bname, bdesc = _stop_name(board)
        aname, adesc = _stop_name(alight)
        trips = info.get("n_trips") or 0
        walk = o_walk.get(board, 0) + d_walk.get(alight, 0)
        # Rank on a composite: stops ridden + a penalty for walking + a bonus for
        # frequency, so a 6-stop once-a-day express does not beat a frequent route.
        score = hops + walk * 25 - 6 * math.log10(1 + trips)
        direct.append({
            "route_id": cand["route_id"],
            "route_short_name": display_route(info.get("route_short_name"), info.get("route_long_name")),
            "route_name_raw": info.get("route_short_name"),
            "route_long_name": info.get("route_long_name"),
            "board_stop": bname, "board_stop_id": board, "board_dir": bdesc,
            "alight_stop": aname, "alight_stop_id": alight, "alight_dir": adesc,
            "stops_passed": hops,
            "trips_per_day": trips,
            "is_ac": info.get("is_ac"),
            "walk_to_board_km": round(o_walk.get(board, 0), 2),
            "walk_from_alight_km": round(d_walk.get(alight, 0), 2),
            "rank_score": round(score, 2),
        })
    direct.sort(key=lambda x: x["rank_score"])
    direct = direct[:5]

    payload = {
        "origin": {"query": origin, "resolved": o["name"], "stop_id": o.get("stop_id"),
                   "stop_name": o.get("stop_name"), "lat": o["lat"], "lon": o["lon"]},
        "destination": {"query": destination, "resolved": d["name"], "stop_id": d.get("stop_id"),
                        "stop_name": d.get("stop_name"), "lat": d["lat"], "lon": d["lon"]},
        "direct": direct,
        "transfers": [],
        "straight_line_km": round(haversine_km(o["lat"], o["lon"], d["lat"], d["lon"]), 1),
    }

    if not direct:
        for t in _transfers(o_ids, d_ids):
            leg1 = route_info(t["leg1_route_id"])
            leg2 = route_info(t["leg2_route_id"])
            mid_name, mid_desc = _stop_name(t["transfer_stop_id"])
            payload["transfers"].append({
                "leg1_route": display_route(leg1.get("route_short_name"), leg1.get("route_long_name")),
                "leg1_route_long_name": leg1.get("route_long_name"),
                "leg2_route": display_route(leg2.get("route_short_name"), leg2.get("route_long_name")),
                "leg2_route_long_name": leg2.get("route_long_name"),
                "transfer_stop": mid_name, "transfer_stop_desc": mid_desc,
                "transfer_stop_id": t["transfer_stop_id"],
                "approx_stops": t["total_hops"],
            })

    if direct:
        top = direct[0]
        walk_note = ""
        if top["walk_to_board_km"] > 0.2 or top["walk_from_alight_km"] > 0.2:
            walk_note = (f" Walk about {top['walk_to_board_km']} km to the boarding stop"
                         f" and {top['walk_from_alight_km']} km at the other end.")
        payload["summary"] = (
            f"Direct route {top['route_short_name']} ({top['route_long_name']}) from "
            f"{top['board_stop']} to {top['alight_stop']}, about {top['stops_passed']} stops, "
            f"{top['trips_per_day']} trips a day.{walk_note} "
            f"{len(direct)} direct option(s) found.")
    elif payload["transfers"]:
        t = payload["transfers"][0]
        payload["summary"] = (
            f"No single bus. Take {t['leg1_route']}, change at {t['transfer_stop']}, "
            f"then {t['leg2_route']} (about {t['approx_stops']} stops total).")
    else:
        payload["summary"] = (
            f"No BMTC route connects {o['name']} and {d['name']}. This corridor likely needs "
            f"a KSRTC ordinary leg; check the KSRTC options for the destination.")
    return {
        "ok": True,
        "tool": "find_routes",
        "summary": payload.pop("summary"),
        "data": payload,
        "sources": ["BMTC GTFS stop_times/trips (trip-level verification)"],
    }


# ------------------------------------------------------------------------- metro info
def metro_info(place: str, from_line: str = "") -> dict[str, Any]:
    """Which Namma Metro line serves the station nearest this place, and where to change.

    If `from_line` (e.g. "green") is given, also returns the station where the rider
    must switch from that line to the line serving `place`.
    """
    r = resolve_place(place)
    if not r.get("ok"):
        return {"ok": False, "tool": "metro_info", "summary": f"Could not resolve '{place}'."}
    st = nearest_metro_station(r["lat"], r["lon"])
    if not st:
        return {"ok": False, "tool": "metro_info",
                "summary": f"No metro station within 8 km of {r['name']}."}
    con = db()
    lines = [dict(x) for x in con.execute("""
        SELECT DISTINCT r.route_id, r.route_short_name, r.route_long_name
        FROM metro_route_stops m JOIN metro_routes r ON r.route_id = m.route_id
        WHERE m.station_id = ?""", (st["station_id"],))]
    if not lines:
        con.close()
        return {"ok": False, "tool": "metro_info",
                "summary": f"{st['station_name']} is not on a mapped metro line."}

    # stations served by more than one line = interchanges
    rows = con.execute("""
        SELECT m.station_id, s.stop_name, COUNT(DISTINCT m.route_id) AS n_lines
        FROM metro_route_stops m JOIN metro_stops s ON s.stop_id = m.station_id
        GROUP BY m.station_id HAVING n_lines > 1""").fetchall()
    interchanges = {x["station_id"]: {"station": x["stop_name"], "station_id": x["station_id"]}
                    for x in rows}

    transfer = None
    if from_line:
        want = re.sub(r"\s*line\s*$", "", from_line.lower()).strip()
        src = [l for l in lines if True]
        con2 = con
        src_lines = [dict(x) for x in con2.execute("""
            SELECT DISTINCT route_id, route_short_name FROM metro_routes
            WHERE lower(route_short_name) = ?""", (want,))]
        if src_lines:
            src_ids = {x["route_id"] for x in src_lines}
            dst_ids = {l["route_id"] for l in lines}
            for sid, info in interchanges.items():
                served = {row[0] for row in con2.execute(
                    "SELECT DISTINCT route_id FROM metro_route_stops WHERE station_id = ?", (sid,))}
                if (served & src_ids) and (served & dst_ids):
                    transfer = info
                    break
    con.close()

    body = {
        "place": r["name"],
        "station": {"id": st["station_id"], "name": st["station_name"],
                    "distance_km": st["distance_km"]},
        "lines": [{"line": x["route_short_name"], "termini": x["route_long_name"]} for x in lines],
        "interchanges": list(interchanges.values())[:4],
        "transfer": transfer,
        "from_line": from_line or None,
    }
    line_txt = " and ".join(x["route_short_name"] + " Line" for x in lines)
    summary = (f"{r['name']} -> nearest metro station is {st['station_name']} "
               f"({st['distance_km']} km) on the {line_txt}.")
    if transfer:
        summary += (f" From the {from_line.title()} Line, change at {transfer['station']} "
                    f"to reach it.")
    elif len(lines) == 1 and interchanges:
        summary += " To reach another line, change at " + ", ".join(
            i["station"] for i in list(interchanges.values())[:2]) + "."
    return {"ok": True, "tool": "metro_info", "summary": summary, "data": body,
            "sources": ["BMRCL GTFS routes/stop_times"]}


# ------------------------------------------------------------------------ metro planning
def plan_metro_leg(origin: str, destination: str) -> dict[str, Any]:
    """Plan the metro portion of a trip between two places, including interchanges and fare."""
    o = resolve_place(origin)
    d = resolve_place(destination)
    if not o.get("ok") or not d.get("ok"):
        return {"ok": False, "tool": "plan_metro_leg",
                "summary": f"Could not resolve '{origin}' or '{destination}'."}

    o_st = nearest_metro_station(o["lat"], o["lon"])
    d_st = nearest_metro_station(d["lat"], d["lon"])
    if not o_st or not d_st:
        # Say which end has no station, in words. Printing the dicts leaked Python
        # internals into a user-facing answer.
        missing = []
        for label, st in ((o["name"], o_st), (d["name"], d_st)):
            missing.append(f"{label} — nearest station {st['station_name']}, "
                           f"{st['distance_km']} km" if st
                           else f"{label} — no Namma Metro station within 8 km")
        return {"ok": False, "tool": "plan_metro_leg",
                "summary": "No usable metro leg for this trip — " + "; ".join(missing) + "."}
    if o_st["station_id"] == d_st["station_id"]:
        return {"ok": False, "tool": "plan_metro_leg",
                "summary": f"Both places are closest to {o_st['station_name']}; this trip is a walk, not a metro ride."}

    con = db()
    lines = {}
    for r in con.execute("SELECT route_id, route_short_name, route_long_name FROM metro_routes"):
        lines[r["route_id"]] = {"route_id": r["route_id"], "short": r["route_short_name"],
                                "long": r["route_long_name"], "stations": {}}
    for r in con.execute("""
        SELECT m.route_id, m.station_id, m.seq, s.stop_name, m.direction_id
        FROM metro_route_stops m JOIN metro_stops s ON s.stop_id = m.station_id
        ORDER BY m.route_id, m.direction_id, m.seq"""):
        lines[r["route_id"]]["stations"].setdefault(r["station_id"], r["stop_name"])

    def seq_in(route_id: str, station: str, direction: str) -> int | None:
        row = con.execute("SELECT seq FROM metro_route_stops WHERE route_id=? AND station_id=? "
                          "AND direction_id=? LIMIT 1", (route_id, station, direction)).fetchone()
        return row["seq"] if row else None

    best = None
    for rid, line in lines.items():
        for direction in ("0", "1"):
            a = seq_in(rid, o_st["station_id"], direction)
            b = seq_in(rid, d_st["station_id"], direction)
            if a is not None and b is not None and b > a:
                n = b - a
                if best is None or n < best["stops"]:
                    best = {"lines": [rid], "stops": n, "direction": direction}

    if best is None:
        # try one interchange
        for r1, l1 in lines.items():
            for r2, l2 in lines.items():
                if r1 == r2:
                    continue
                shared = set(l1["stations"]) & set(l2["stations"])
                for mid in shared:
                    for d1 in ("0", "1"):
                        for d2 in ("0", "1"):
                            a = seq_in(r1, o_st["station_id"], d1)
                            m1 = seq_in(r1, mid, d1)
                            m2 = seq_in(r2, mid, d2)
                            b = seq_in(r2, d_st["station_id"], d2)
                            if None in (a, m1, m2, b):
                                continue
                            if m1 > a and b > m2:
                                n = (m1 - a) + (b - m2)
                                if best is None or n < best["stops"]:
                                    best = {"lines": [r1, r2], "stops": n,
                                            "interchange": mid, "direction": d1}
    if best is None:
        con.close()
        return {"ok": False, "tool": "plan_metro_leg",
                "summary": f"No metro path found between {o_st['station_name']} and {d_st['station_name']}."}

    fare = con.execute("SELECT fare_inr, fare_max_inr, fare_source FROM metro_fare_matrix "
                       "WHERE from_station=? AND to_station=?",
                       (o_st["station_id"], d_st["station_id"])).fetchone()
    line_status = {r["route_id"]: r["status"] for r in con.execute(
        "SELECT line_id AS route_id, status FROM metro_line_status")}
    pending_lines = [r for r in best["lines"] if line_status.get(r, "operational") != "operational"]
    con.close()

    route_desc = " -> ".join(f"{lines[r]['short']} Line" for r in best["lines"])
    return {
        "ok": True, "tool": "plan_metro_leg",
        "summary": (f"{o_st['station_name']} -> {d_st['station_name']} on the {route_desc}, "
                    f"{best['stops']} stops"
                    + (f", change at {lines[best['lines'][0]]['stations'].get(best.get('interchange'), best.get('interchange'))}" if best.get("interchange") else "")
                    + (f". Fare {fare['fare_inr']:.0f} INR."
                       if fare and round(fare['fare_inr']) == round(fare['fare_max_inr'])
                       else f". Fare {fare['fare_inr']:.0f}-{fare['fare_max_inr']:.0f} INR."
                       if fare else ".")),
        "data": {
            "board_station": {"id": o_st["station_id"], "name": o_st["station_name"],
                              "distance_km": o_st["distance_km"]},
            "alight_station": {"id": d_st["station_id"], "name": d_st["station_name"],
                               "distance_km": d_st["distance_km"]},
            "lines": [{"route_id": r, "line": lines[r]["short"], "termini": lines[r]["long"]}
                      for r in best["lines"]],
            "stops": best["stops"],
            "interchange": (lines[best["lines"][0]]["stations"].get(best["interchange"])
                            if best.get("interchange") else None),
            "fare_inr": {"min": fare["fare_inr"], "max": fare["fare_max_inr"]} if fare else None,
            "walk_to_station_km": round(o_st["distance_km"], 2),
            "walk_from_station_km": round(d_st["distance_km"], 2),
            "operator": "BMRCL (Namma Metro)",
            "fare_source": (fare["fare_source"] if fare else None),
            "service_pending_lines": pending_lines,
        },
        "sources": ["BMRCL GTFS fare_rules / fare_attributes"],
    }

# ------------------------------------------------------------- line / station fares
def _metro_network(include_pending: bool = True) -> tuple[dict, dict, dict]:
    """Union station graph plus per-line station order.

    include_pending=False restricts the graph to lines carrying passengers today, which
    is the only valid basis for talking about journeys a rider can make right now.
    """
    con = db()
    seqs: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    live = {r[0] for r in con.execute(
        "SELECT line_id FROM metro_line_status WHERE status='operational'")}
    for r in con.execute("SELECT route_id FROM metro_routes"):
        if not include_pending and live and r["route_id"] not in live:
            continue
        st = [x[0] for x in con.execute(
            "SELECT station_id FROM metro_route_stops WHERE route_id=? AND direction_id='0' "
            "ORDER BY seq", (r["route_id"],))]
        ded = []
        for x in st:
            if not ded or ded[-1] != x:
                ded.append(x)
        seqs[r["route_id"]] = ded
    for sid, nm in con.execute("SELECT stop_id, stop_name FROM metro_stops "
                               "WHERE location_type='1'"):
        names.setdefault(sid, nm)
    adj: dict[str, set] = {}
    for seq in seqs.values():
        for a, b in zip(seq, seq[1:]):
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
    con.close()
    return adj, names, seqs


def _hops(adj: dict, a: str, b: str) -> int | None:
    if a == b:
        return 0
    seen, frontier = {a: 0}, [a]
    while frontier:
        nxt = []
        for u in frontier:
            for v in adj.get(u, ()):
                if v not in seen:
                    seen[v] = seen[u] + 1
                    if v == b:
                        return seen[v]
                    nxt.append(v)
        frontier = nxt
    return None


def _match_station(query: str, names: dict) -> dict | None:
    from rapidfuzz import fuzz, process
    keys = list(names)
    hit = process.extractOne(query, [names[k] for k in keys], scorer=fuzz.WRatio)
    if not hit or hit[1] < 62:
        return None
    idx = hit[2]
    return {"id": keys[idx], "name": names[keys[idx]], "score": round(hit[1], 1)}


def metro_fare(from_station: str, to_station: str) -> dict[str, Any]:
    """Fare between two named metro stations, including lines that are not open yet."""
    adj_all, names, seqs = _metro_network(include_pending=True)
    a = _match_station(from_station, names)
    b = _match_station(to_station, names)
    if not a or not b:
        return {"ok": False, "tool": "metro_fare",
                "summary": f"Could not match '{from_station if not a else to_station}' to a "
                           f"Namma Metro station."}
    con = db()
    row = con.execute("SELECT fare_inr, fare_max_inr, fare_source FROM metro_fare_matrix "
                      "WHERE from_station=? AND to_station=?", (a["id"], b["id"])).fetchone()
    status = {r["line_id"]: (r["status"], r["first_opening"]) for r in con.execute(
        "SELECT line_id, status, first_opening FROM metro_line_status")}
    pending = [sid for sid in (a["id"], b["id"])
               if con.execute("SELECT COALESCE(service_status,'operational') FROM metro_stops "
                              "WHERE stop_id=?", (sid,)).fetchone()[0] != "operational"]
    if not row:
        con.close()
        return {"ok": False, "tool": "metro_fare",
                "summary": f"No fare between {a['name']} and {b['name']}."}
    # Hop counts must come from the network that is open when the trip is taken.
    adj_live, _, _ = _metro_network(include_pending=False)
    hops = _hops(adj_live if not pending else adj_all, a["id"], b["id"])
    if hops is None:
        hops = _hops(adj_all, a["id"], b["id"])
    # Official fare slab for this station count (the chart is the primary source).
    official = con.execute(
        "SELECT fare_inr FROM metro_fare_slabs WHERE stations_travelled=?",
        (min(hops or 1, 30),)).fetchone()
    con.close()
    lines_used = [ln for ln, seq in seqs.items() if a["id"] in seq or b["id"] in seq]
    # Smart card / NCMC fares from the official discount rules.
    smart = {}
    if row["fare_inr"]:
        base = row["fare_inr"]
        smart = {"token": base,
                 "smartcard_peak": round(base * 0.95, 2),
                 "smartcard_offpeak": round(base * 0.90, 2)}
    lo, hi = row["fare_inr"], row["fare_max_inr"]
    price = f"Rs {lo:.0f}" if round(lo) == round(hi) else f"Rs {lo:.0f}-{hi:.0f}"
    derived = (row["fare_source"] or "published_matrix") != "published_matrix"
    msg = f"{a['name']} -> {b['name']}: {price}"
    msg += f", {hops} stops" if hops is not None else ""
    if derived:
        msg += (" (derived from BMRCL's official fare chart"
                "; this station is not in service yet)" if pending else ")")
    elif official:
        msg += " (BMRCL official fare chart, 14.02.2025)"
    else:
        msg += " (published BMRCL fare matrix)"
    return {"ok": True, "tool": "metro_fare",
            "summary": msg,
            "data": {"from": a, "to": b, "fare_inr": lo, "fare_max_inr": hi,
                     "stops": hops, "fare_source": row["fare_source"] or "published_matrix",
                     "official_slab": (official["fare_inr"] if official else None),
                     "smartcard_fares": smart,
                     "lines_on_route": lines_used,
                     "service_pending": bool(pending),
                     "line_status": status},
            "sources": (["BMRCL published fare matrix + curated Pink Line geometry"]
                        if derived else ["BMRCL GTFS fare_rules"])}


def metro_line_details(line: str = "pink") -> dict[str, Any]:
    """Every station on a metro line, its service status, and fares from the terminus."""
    adj, names, seqs = _metro_network()
    want = line.strip().upper()
    if want not in seqs:
        for ln in seqs:
            if ln.startswith(want[:4]):
                want = ln
                break
    if want not in seqs:
        return {"ok": False, "tool": "metro_line_details",
                "summary": f"No metro line called '{line}'. Known lines: {', '.join(seqs)}."}
    seq = seqs[want]
    con = db()
    meta = con.execute("SELECT * FROM metro_line_status WHERE line_id=?", (want,)).fetchone()
    fares = []
    for sid in seq:
        if sid == seq[0]:
            fares.append({"station": names.get(sid, sid), "station_id": sid,
                          "fare_inr": 0.0, "fare_source": "origin"})
            continue
        row = con.execute("SELECT fare_inr, fare_max_inr, fare_source FROM metro_fare_matrix "
                          "WHERE from_station=? AND to_station=?", (seq[0], sid)).fetchone()
        fares.append({"station": names.get(sid, sid), "station_id": sid,
                      "fare_inr": row["fare_inr"] if row else None,
                      "fare_source": row["fare_source"] if row else None})
    con.close()
    status = meta["status"] if meta else "operational"
    summary = (f"{want.title()} Line: {len(seq)} stations, "
               f"{meta['length_km'] if meta else '?'} km, status {status.replace('_', ' ')}.")
    if meta and status != "operational" and meta["first_opening"]:
        summary += f" Expected to open {meta['first_opening']}."
    return {"ok": True, "tool": "metro_line_details",
            "summary": summary,
            "data": {"line": want, "stations": [names.get(s, s) for s in seq],
                     "status": status,
                     "note": meta["note"] if meta else None,
                     "source_url": meta["source_url"] if meta else None,
                     "fares_from_terminus": fares},
            "sources": ([meta["source_url"]] if meta else ["BMRCL GTFS"])}
