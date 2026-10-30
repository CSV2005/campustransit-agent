"""D3 (OSM) + curated places -> `places` table in transit.db.

The agent needs to turn "DSU Harohalli", "Jayanagar", "Silk Institute" into
coordinates and then into an actual GTFS stop. This module builds one searchable
place index from four sources:

  1. BMTC stops            (9,960 stops, official GTFS)   -> kind=bmtc_stop
  2. Namma Metro stations  (BMRCL GTFS)                   -> kind=metro_station
  3. OSM POIs              (campuses, colleges, stations) -> kind=osm_poi
  4. OSM bus stops         (south Bengaluru corridor)     -> kind=osm_bus_stop
  5. Curated campus aliases (data/curated/places.json)    -> kind=campus

It also writes GeoJSON versions of the OSM extracts (deliverable evidence for D3).

Run:  python -m src.ingest_places
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from src.config import CURATED, DB_PATH, RAW_OSM


def _geojson(elements: list[dict], out: Path, props: dict) -> None:
    feats = []
    for e in elements:
        tags = e.get("tags", {})
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lon = e.get("lon") or (e.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"osm_id": e["id"], "osm_type": e["type"],
                           "name": tags.get("name"),
                           "amenity": tags.get("amenity"),
                           "place": tags.get("place"),
                           "station": tags.get("station"),
                           "highway": tags.get("highway")},
        })
    out.write_text(json.dumps({
        "type": "FeatureCollection",
        "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        **props,
        "features": feats,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"  ok    {out.name:<34} {len(feats):>6,} features", flush=True)


def main() -> None:
    if not DB_PATH.exists():
        raise SystemExit("transit.db missing - run `python -m src.ingest_gtfs` first")

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        DROP TABLE IF EXISTS places;
        CREATE TABLE places(
            place_id  TEXT PRIMARY KEY,
            name      TEXT NOT NULL,
            norm_name TEXT NOT NULL,
            kind      TEXT NOT NULL,
            lat       REAL,
            lon       REAL,
            stop_id   TEXT,
            aliases   TEXT,
            source    TEXT,
            extra     TEXT
        );
    """)

    def add(pid, name, kind, lat, lon, stop_id=None, aliases="", source="", extra=""):
        if not name:
            return
        norm = " ".join(name.lower().replace("(", " ").replace(")", " ").split())
        cur.execute(
            "INSERT OR REPLACE INTO places VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, name, norm, kind, lat, lon, stop_id, aliases, source, extra))

    # ------------------------------------------------------------------ 1. BMTC
    n = 0
    for sid, name, lat, lon in con.execute(
            "SELECT stop_id, stop_name, CAST(stop_lat AS REAL), CAST(stop_lon AS REAL) FROM stops").fetchall():
        add(f"bmtc:{sid}", name, "bmtc_stop", lat, lon, stop_id=sid,
            source="BMTC GTFS stops.txt")
        n += 1

    # --------------------------------------------------------------- 2. Metro
    m = 0
    for sid, name, lat, lon in con.execute(
            "SELECT stop_id, stop_name, CAST(stop_lat AS REAL), CAST(stop_lon AS REAL) "
            "FROM metro_stops WHERE location_type='1'").fetchall():
        add(f"metro:{sid}", name, "metro_station", lat, lon, stop_id=sid,
            source="BMRCL GTFS stops.txt")
        m += 1

    # ------------------------------------------------------------------- 3+4. OSM
    osm_poi = RAW_OSM / "osm_pois_raw.json"
    o1 = o2 = 0
    if osm_poi.exists():
        data = json.loads(osm_poi.read_text())
        _geojson(data["elements"], RAW_OSM / "bengaluru_pois.geojson",
                 {"source": "Overpass API", "query": "amenity~university|college, "
                  "station=subway, place~suburb|neighbourhood|quarter|town|village"})
        for e in data["elements"]:
            t = e.get("tags", {})
            name = t.get("name")
            lat = e.get("lat") or (e.get("center") or {}).get("lat")
            lon = e.get("lon") or (e.get("center") or {}).get("lon")
            if not name or lat is None:
                continue
            kind = ("metro_station" if t.get("station") == "subway" else
                    "campus" if t.get("amenity") in ("university", "college") else
                    "place")
            add(f"osm:{e['type']}:{e['id']}", name, f"osm_{kind}", lat, lon,
                source="OpenStreetMap via Overpass (ODbL)")
            o1 += 1

    osm_bus = RAW_OSM / "osm_busstops_south_raw.json"
    if osm_bus.exists():
        data = json.loads(osm_bus.read_text())
        _geojson(data["elements"], RAW_OSM / "bus_stops_south_corridor.geojson",
                 {"source": "Overpass API", "query": "highway=bus_stop bbox south corridor"})
        for e in data["elements"]:
            t = e.get("tags", {})
            name = t.get("name")
            if not name:
                continue
            add(f"osm:node:{e['id']}", name, "osm_bus_stop", e.get("lat"), e.get("lon"),
                source="OpenStreetMap via Overpass (ODbL)")
            o2 += 1

    # ------------------------------------------------------------- 5. Curated aliases
    campus_file = CURATED / "places.json"
    c = 0
    if campus_file.exists():
        for p in json.loads(campus_file.read_text(encoding="utf-8")):
            add(f"curated:{p['id']}", p["name"], p.get("kind", "campus"),
                p.get("lat"), p.get("lon"),
                aliases="; ".join(p.get("aliases", [])),
                source=p.get("source", "curated"),
                extra=json.dumps({k: p[k] for k in ("note", "nearest_stop_hint")
                                  if k in p}))
            c += 1

    cur.executescript("""
        CREATE INDEX IF NOT EXISTS ix_places_norm ON places(norm_name);
        CREATE INDEX IF NOT EXISTS ix_places_kind ON places(kind);
    """)
    con.commit()

    total = cur.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    print(f"\nplaces: {total:,}  (BMTC {n:,} | metro {m:,} | OSM POI {o1:,} | "
          f"OSM bus stop {o2:,} | curated {c:,})", flush=True)
    con.close()


if __name__ == "__main__":
    sys.exit(main())
