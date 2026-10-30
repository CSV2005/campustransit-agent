"""Writes data/raw/dataset_manifest.json - the auditable record of every dataset.

Run:  python -m src.make_manifest
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from src.config import DB_PATH, MANIFEST_PATH, RAW_KSRTC, RAW_OSM, RAW_GTFS_BMTC, RAW_GTFS_BMRCL


def count_lines(p: Path) -> int | None:
    if not p.exists():
        return None
    with p.open("rb") as fh:
        return max(0, sum(1 for _ in fh) - 1)


def main() -> None:
    con = sqlite3.connect(DB_PATH) if DB_PATH.exists() else None
    db_counts = {}
    if con:
        for t in ["stops", "routes", "trips", "stop_times", "route_stops",
                  "fare_rules", "fare_matrix", "metro_stops", "metro_route_stops",
                  "metro_fare_matrix", "places", "ksrtc_corridor"]:
            try:
                db_counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                db_counts[t] = None
        con.close()

    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "note": "data.gov.in and kstrc.karnataka.gov.in were unreachable from the "
                    "build sandbox on 2026-10-22; see failed_attempts below.",
        },
        "datasets": [
            {
                "id": "D1",
                "name": "BMTC GTFS (static)",
                "provider": "Bengaluru Metropolitan Transport Corporation, via the DULT "
                            "Transport Data Hub; mirrored by Vonter/bmtc-gtfs (scraped from "
                            "the official Namma BMTC app)",
                "url": "https://github.com/Vonter/bmtc-gtfs",
                "download": "https://raw.githubusercontent.com/Vonter/bmtc-gtfs/main/gtfs/bmtc.zip",
                "license": "Open data / CC-BY-4.0 style attribution requested by upstream; "
                           "BMTC is the data owner. See ATTRIBUTATIONS in the feed.",
                "feed_validity": "2026-09-07 to 2027-09-07",
                "files": ["agency.txt", "routes.txt", "stops.txt", "trips.txt",
                          "stop_times.txt", "calendar.txt", "fare_attributes.txt",
                          "fare_rules.txt", "shapes.txt", "translations.txt"],
                "row_counts": {f.stem: count_lines(p) for f, p in
                               [(p, p) for p in sorted(RAW_GTFS_BMTC.glob("*.txt"))]},
                "used_for": "route graph, stop coordinates, trip sequences, fare stages",
                "caveat": "Upstream mirrors what the Namma BMTC app exposes; only routes "
                          "with live tracking are present, and stop timings can drift.",
            },
            {
                "id": "D2",
                "name": "BMRCL / Namma Metro GTFS",
                "provider": "Bengaluru Metro Rail Corporation, via the DULT Transport Data "
                            "Hub; mirrored by Vonter/bmrcl-gtfs",
                "url": "https://github.com/Vonter/bmrcl-gtfs",
                "download": "https://raw.githubusercontent.com/Vonter/bmrcl-gtfs/main/gtfs/bmrcl.zip",
                "license": "Open data; BMRCL is the data owner",
                "feed_validity": "2026-08-17 to 2027-08-17",
                "files": ["routes.txt", "stops.txt", "trips.txt", "stop_times.txt",
                          "fare_attributes.txt", "fare_rules.txt", "fare_products.txt",
                          "fare_leg_rules.txt", "pathways.txt", "transfers.txt"],
                "row_counts": {p.stem: count_lines(p) for p in sorted(RAW_GTFS_BMRCL.glob("*.txt"))},
                "used_for": "metro legs, station access (entrances/pathways), metro fares",
                "caveat": "Intermediate stop times are approximated from BMRCL timetable "
                          "images and stop spacing, not official per-stop timings.",
            },
            {
                "id": "D3",
                "name": "OpenStreetMap — Bengaluru / Kanakapura corridor extract",
                "provider": "OpenStreetMap contributors, via the Overpass API",
                "url": "https://overpass-api.de/api/interpreter",
                "license": "ODbL 1.0 (© OpenStreetMap contributors)",
                "bbox_pois": [12.30, 77.20, 13.15, 77.80],
                "bbox_bus_stops": [12.25, 77.15, 12.85, 77.65],
                "query_file": "data/raw/osm/pois.ql, data/raw/osm/busstops_south.ql",
                "features": {"pois": count_lines(RAW_OSM / "osm_pois_raw.json"),
                             "bus_stops_south": count_lines(RAW_OSM / "osm_busstops_south_raw.json")},
                "used_for": "campus/landmark geocoding, metro entrances, corridor bus stops",
                "caveat": "OSM coverage of individual college gates is uneven; every place "
                          "is snapped to the nearest official GTFS stop before routing.",
            },
            {
                "id": "D4",
                "name": "Karnataka transport / KSRTC corridor data (curated)",
                "provider": "Curated by CampusTransit from KSRTC fare table reproduced by "
                            "Star of Mysore (2025-01-05) and The Indian Express (2025-01-05)",
                "url": "data/curated/ksrtc_corridor.csv",
                "license": "Facts (fares) - no database right asserted",
                "rows": db_counts.get("ksrtc_corridor"),
                "used_for": "KSRTC intercity leg on the Kanakapura Road / Harohalli corridor",
                "caveat": "data.gov.in was unreachable during the build (HTTP 503 / no DNS). "
                          "Only two fares are published numbers; the Harohalli fare is an "
                          "explicitly-labelled interpolation.",
            },
        ],
        "failed_attempts": [
            {"url": "https://www.data.gov.in/resource/stateutt-wise-number-buses-operated-state-road-transport-undertakings",
             "result": "HTTP 503", "at": "2026-10-22"},
            {"url": "https://data.gov.in", "result": "no response (timeout)", "at": "2026-10-22"},
            {"url": "https://api.data.gov.in/catalog", "result": "no response (timeout)", "at": "2026-10-22"},
            {"url": "https://karnataka.data.gov.in", "result": "DNS failure", "at": "2026-10-22"},
            {"url": "https://kstrc.karnataka.gov.in", "result": "no response (timeout)", "at": "2026-10-22"},
            {"url": "https://www.ksrtc.in/oprs-web/avail/fetchAllTripServices.do",
             "result": "HTTP 302 redirect to homepage (session/CSRF protected)", "at": "2026-10-22"},
            {"url": "https://tdh.dult-karnataka.com", "result": "no response (timeout)", "at": "2026-10-22"},
            {"url": "https://transit.land/api/v2/rest/feeds", "result": "HTTP 401 Unauthorized (API key required)",
             "at": "2026-10-22"},
        ],
        "derived_sqlite_tables": db_counts,
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {MANIFEST_PATH}")
    print(json.dumps(db_counts, indent=2))


if __name__ == "__main__":
    sys.exit(main())
