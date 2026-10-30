"""Ingest the Pink Line (BMRCL Reach 6) alongside the operational Green/Yellow/Purple lines.

The upstream BMRCL GTFS feed only carries lines that are already carrying passengers.
The Pink Line has not opened yet (CMRS inspection completed 8 Oct 2026, authorisation
pending), so it is published here from a curated file rather than the feed.

What this script does
---------------------
1. Adds `service_status` to metro_stops / metro_routes and `fare_source` to
   metro_fare_matrix, plus a `metro_line_status` table.
2. Inserts the Pink Line route and its 18 stations (reusing the existing JDHP and MAGR
   stop_ids so the Jayadeva and MG Road interchanges join the network properly).
3. Derives Pink Line fares with the fare law measured from the published BMRCL matrix:
   the fare is a pure function of the shortest hop count between two stations
   (1,000 % match over all 6,806 published pairs -- see docs). Derived fares are tagged
   `derived_hop_law`, never `published_matrix`.

Run:  python -m src.ingest_metro_pink
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from src.config import CURATED, DB_PATH

PINK_FILE = CURATED / "metro_pink_line.json"

# Fare law fitted on the published BMRCL matrix (6,806 OD pairs, 100% exact):
# fare is determined solely by the shortest number of hops between two stations.
FARE_BY_HOPS = [(2, 10), (4, 20), (6, 30), (8, 40), (10, 50),
                (15, 60), (20, 70), (25, 80), (10**6, 90)]


def fare_for_hops(h: int) -> int:
    for cut, fare in FARE_BY_HOPS:
        if h <= cut:
            return fare
    return 90


def _has_column(con: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in con.execute(f"PRAGMA table_info({table})"))


def ensure_schema(con: sqlite3.Connection) -> None:
    if not _has_column(con, "metro_stops", "service_status"):
        con.execute("ALTER TABLE metro_stops ADD COLUMN service_status TEXT DEFAULT 'operational'")
    if not _has_column(con, "metro_routes", "service_status"):
        con.execute("ALTER TABLE metro_routes ADD COLUMN service_status TEXT DEFAULT 'operational'")
    if not _has_column(con, "metro_fare_matrix", "fare_source"):
        con.execute("ALTER TABLE metro_fare_matrix ADD COLUMN fare_source TEXT DEFAULT 'published_matrix'")
    con.execute("""
        CREATE TABLE IF NOT EXISTS metro_line_status (
            line_id TEXT PRIMARY KEY,
            line_name TEXT, colour TEXT, status TEXT,
            length_km REAL, stations INTEGER,
            elevated_stations INTEGER, underground_stations INTEGER,
            first_opening TEXT, full_opening TEXT,
            note TEXT, source_url TEXT, last_verified TEXT, verification_status TEXT
        )""")
    con.commit()


def load_pink() -> dict:
    return json.loads(PINK_FILE.read_text(encoding="utf-8"))


def insert_line(con: sqlite3.Connection, pink: dict) -> None:
    con.execute("DELETE FROM metro_route_stops WHERE route_id='PINK'")
    con.execute("DELETE FROM metro_routes WHERE route_id='PINK'")
    con.execute("""
        INSERT INTO metro_routes (route_id, route_short_name, route_long_name, route_desc,
                                  route_type, agency_id, route_color, route_text_color,
                                  service_status)
        VALUES ('PINK', ?, ?, ?, '1', 'BMRCL', ?, 'FFFFFF', 'under_construction')""",
                (pink["route_short_name"], pink["route_long_name"], pink["route_desc"],
                 pink["route_color"]))

    stations = pink["stations"]
    for s in stations:
        existing = con.execute("SELECT stop_id, stop_name, service_status FROM metro_stops "
                               "WHERE stop_id=?", (s["stop_id"],)).fetchone()
        if existing:
            # Interchange with an operating line: the station exists and is open today,
            # only the Pink *service* at it is pending. Leave it operational.
            continue
        con.execute("""
            INSERT INTO metro_stops (stop_id, stop_name, stop_lat, stop_lon, location_type,
                                     parent_station, service_status)
            VALUES (?, ?, ?, ?, '1', NULL, 'under_construction')""",
                    (s["stop_id"], s["name"], s["lat"], s["lon"]))

    for direction in ("0", "1"):
        seq_list = stations if direction == "0" else list(reversed(stations))
        for i, s in enumerate(seq_list, start=1):
            con.execute("INSERT INTO metro_route_stops (route_id, direction_id, station_id, seq) "
                        "VALUES ('PINK', ?, ?, ?)", (direction, s["stop_id"], i))
    con.commit()


def line_sequences(con: sqlite3.Connection) -> dict[str, list[str]]:
    seqs = {}
    for line in [r[0] for r in con.execute(
            "SELECT DISTINCT route_id FROM metro_routes WHERE route_id IN "
            "('GREEN','YELLOW','PURPLE','PINK')")]:
        st = [r[0] for r in con.execute(
            "SELECT station_id FROM metro_route_stops WHERE route_id=? AND direction_id='0' "
            "ORDER BY seq", (line,))]
        ded = []
        for s in st:
            if not ded or ded[-1] != s:
                ded.append(s)
        seqs[line] = ded
    return seqs


def build_graph(seqs: dict[str, list[str]], include_pink: bool):
    adj = defaultdict(set)
    for line, seq in seqs.items():
        if line == "PINK" and not include_pink:
            continue
        for a, b in zip(seq, seq[1:]):
            adj[a].add(b)
            adj[b].add(a)
    return adj


def hop_count(adj, a: str, b: str) -> int | None:
    if a == b:
        return 0
    seen = {a: 0}
    frontier = [a]
    while frontier:
        nxt = []
        for u in frontier:
            for v in adj[u]:
                if v not in seen:
                    seen[v] = seen[u] + 1
                    if v == b:
                        return seen[v]
                    nxt.append(v)
        frontier = nxt
    return None


def verify_law(con: sqlite3.Connection, seqs: dict[str, list[str]]) -> tuple[int, int]:
    """Sanity check: does the hop law reproduce the published matrix exactly?"""
    adj = build_graph(seqs, include_pink=False)
    ok = tot = 0
    for a, b, fare in con.execute(
            "SELECT from_station, to_station, fare_inr FROM metro_fare_matrix "
            "WHERE fare_source='published_matrix' OR fare_source IS NULL"):
        if a not in adj or b not in adj:
            continue
        h = hop_count(adj, a, b)
        if h is None:
            continue
        tot += 1
        ok += int(fare_for_hops(h) == fare)
    return ok, tot


def generate_pink_fares(con: sqlite3.Connection, seqs: dict[str, list[str]]) -> int:
    """Price every OD pair that touches a Pink-only station, using the published law."""
    pink_only = set(seqs["PINK"]) - set(seqs["GREEN"]) - set(seqs["YELLOW"]) - set(seqs["PURPLE"])
    adj = build_graph(seqs, include_pink=True)
    nodes = sorted(adj)
    con.execute("DELETE FROM metro_fare_matrix WHERE fare_source='derived_hop_law'")
    rows = []
    for a in nodes:
        for b in nodes:
            if a == b:
                continue
            if a not in pink_only and b not in pink_only:
                continue
            h = hop_count(adj, a, b)
            if h is None:
                continue
            f = fare_for_hops(h)
            rows.append((a, b, float(f), float(f), "derived_hop_law"))
    con.executemany("INSERT INTO metro_fare_matrix "
                    "(from_station, to_station, fare_inr, fare_max_inr, fare_source) "
                    "VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    return len(rows)


def write_status(con: sqlite3.Connection, pink: dict, seqs: dict[str, list[str]]) -> None:
    con.execute("DELETE FROM metro_line_status")
    published = [
        ("GREEN", "Green", "009933", "operational", 31.7, None, None, None,
         "Madavara - Silk Institute. 32 stations."),
        ("PURPLE", "Purple", "8C2877", "operational", 40.5, None, None, None,
         "Whitefield (Kadugodi) - Challaghatta. 37 stations."),
        ("YELLOW", "Yellow", "FFDF00", "operational", 17.6, None, None, None,
         "Rashtreeya Vidyalaya Road - Delta Electronics Bommasandra. 16 stations."),
    ]
    for lid, name, colour, status, km, fo, ffull, _u, note in published:
        con.execute("""INSERT INTO metro_line_status
            (line_id, line_name, colour, status, length_km, stations, elevated_stations,
             underground_stations, first_opening, full_opening, note, source_url,
             last_verified, verification_status)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 'operational', 'operational', ?, ?, ?, ?)""",
                    (lid, name, colour, status, km, len(seqs[lid]), note,
                     "https://github.com/Vonter/bmrcl-gtfs", "2026-10-22",
                     "VERIFIED_SECONDARY"))

    stations = pink["stations"]
    con.execute("""INSERT INTO metro_line_status
        (line_id, line_name, colour, status, length_km, stations, elevated_stations,
         underground_stations, first_opening, full_opening, note, source_url,
         last_verified, verification_status)
        VALUES ('PINK', 'Pink', ?, 'under_construction', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pink["route_color"], pink["length_km_planned"], len(stations),
                 sum(1 for s in stations if s["section"] == "elevated"),
                 sum(1 for s in stations if s["section"] == "underground"),
                 "2026-11 (elevated, Kalena Agrahara - Tavarekere)",
                 "2027 (underground, Dairy Circle - Nagawara)",
                 pink["operational_note"], pink["source_url"],
                 pink["last_verified"], pink["verification_status"]))
    con.commit()


def main() -> None:
    per_station = {}
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        ensure_schema(con)
        # keep the route station ordering straight if the file was edited
        pink = load_pink()
        pink["stations"] = sorted(pink["stations"], key=lambda s: s["seq"])

        insert_line(con, pink)
        seqs = line_sequences(con)

        ok, tot = verify_law(con, seqs)
        print(f"Fare law check on published matrix: {ok}/{tot} exact ({ok/tot:.2%})")

        n = generate_pink_fares(con, seqs)
        print(f"Generated {n} Pink Line fares (fare_source=derived_hop_law)")

        write_status(con, pink, seqs)

        print("\nPer-line summary")
        for r in con.execute("SELECT * FROM metro_line_status ORDER BY "
                             "CASE line_id WHEN 'GREEN' THEN 1 WHEN 'PURPLE' THEN 2 "
                             "WHEN 'YELLOW' THEN 3 ELSE 4 END"):
            print(f"  {r['line_id']:7} {r['line_name']:7} {r['status']:19} "
                  f"{r['stations']:>3} stations  {r['length_km']:>5} km")
        pink_ids = [s["stop_id"] for s in pink["stations"]]
        print(f"\nPink stations in metro_stops: "
              f"{con.execute('SELECT COUNT(*) FROM metro_stops WHERE stop_id IN (%s)' % ','.join('?'*len(pink_ids)), pink_ids).fetchone()[0]}/18")
    finally:
        con.close()


if __name__ == "__main__":
    main()
