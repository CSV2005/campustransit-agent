"""D1 + D2 -> SQLite transit warehouse.

Loads the BMTC GTFS feed (D1) and the BMRCL/Namma Metro feed (D2) into a single
SQLite database, then builds the derived tables the agent's deterministic tools
query:

  route_stops     which routes serve which stops, in order (BMTC)
  metro_route_stops   same for the metro network
  fare_matrix     (route, origin fare-stage, destination fare-stage) -> INR
  zone_centroid   centroid of every BMTC fare stage (distance fallback)
  route_stats     trips per route (used for ranking)

Run:  python -m src.ingest_gtfs
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

from src.config import DB_PATH, PROCESSED, RAW_GTFS_BMRCL, RAW_GTFS_BMTC

GTFS_TABLES = ["agency", "calendar", "fare_attributes", "fare_rules", "routes", "stops", "trips"]
CHUNK = 200_000


def _log(msg: str) -> None:
    print(msg, flush=True)


def load_csv(con: sqlite3.Connection, path: Path, table: str, chunksize: int = CHUNK,
             usecols=None, drop_empty_route_id: bool = False) -> int:
    """Load a GTFS .txt into SQLite in chunks (keeps memory flat on 1.5M-row files)."""
    if not path.exists():
        _log(f"  skip  {path.name} (not found)")
        return 0
    total, first = 0, True
    for chunk in pd.read_csv(path, dtype=str, low_memory=False, chunksize=chunksize,
                             usecols=usecols, keep_default_na=False):
        if drop_empty_route_id and "route_id" in chunk.columns:
            chunk.loc[chunk["route_id"] == "", "route_id"] = None
        chunk.to_sql(table, con, if_exists="replace" if first else "append", index=False)
        first = False
        total += len(chunk)
    _log(f"  ok    {table:<18} {total:>9,} rows   ({path.name})")
    return total


def build_bmtc(con: sqlite3.Connection) -> None:
    _log("D1  BMTC GTFS")
    for t in GTFS_TABLES:
        load_csv(con, RAW_GTFS_BMTC / f"{t}.txt", t,
                 drop_empty_route_id=(t == "fare_rules"))
    load_csv(con, RAW_GTFS_BMTC / "stop_times.txt", "stop_times")
    # traffic / timetable detail is not needed by the agent; drop it to keep the DB small
    con.execute("DROP TABLE IF EXISTS shapes")

    cur = con.cursor()
    _log("  ..    building indexes")
    for stmt in [
        "CREATE INDEX IF NOT EXISTS ix_st_trip ON stop_times(trip_id)",
        "CREATE INDEX IF NOT EXISTS ix_st_stop ON stop_times(stop_id)",
        "CREATE INDEX IF NOT EXISTS ix_tr_route ON trips(route_id)",
        "CREATE INDEX IF NOT EXISTS ix_stops_name ON stops(stop_name)",
        "CREATE INDEX IF NOT EXISTS ix_stops_zone ON stops(zone_id)",
    ]:
        cur.execute(stmt)
    con.commit()

    _log("  ..    route_stops (route x direction x stop, min sequence)")
    cur.executescript("""
        DROP TABLE IF EXISTS route_stops;
        CREATE TABLE route_stops AS
        SELECT t.route_id                        AS route_id,
               COALESCE(t.direction_id, '0')     AS direction_id,
               st.stop_id                        AS stop_id,
               MIN(CAST(st.stop_sequence AS INTEGER)) AS seq
        FROM stop_times st
        JOIN trips t ON t.trip_id = st.trip_id
        GROUP BY t.route_id, COALESCE(t.direction_id, '0'), st.stop_id;
    """)
    cur.executescript("""
        CREATE INDEX IF NOT EXISTS ix_rs_stop  ON route_stops(stop_id);
        CREATE INDEX IF NOT EXISTS ix_rs_route ON route_stops(route_id, direction_id, seq);
    """)
    _log(f"  ok    route_stops       {cur.execute('SELECT COUNT(*) FROM route_stops').fetchone()[0]:>9,} rows")

    _log("  ..    fare_matrix (fare stage -> fare stage -> INR)")
    cur.executescript("""
        DROP TABLE IF EXISTS fare_matrix;
        CREATE TABLE fare_matrix AS
        SELECT fr.route_id                       AS route_id,
               fr.origin_id                      AS from_zone,
               fr.destination_id                 AS to_zone,
               MIN(CAST(fa.price AS REAL))       AS fare_inr,
               MAX(CAST(fa.price AS REAL))       AS fare_max_inr,
               COUNT(*)                          AS n_rules
        FROM fare_rules fr
        JOIN fare_attributes fa ON fa.fare_id = fr.fare_id
        WHERE fr.origin_id <> '' AND fr.destination_id <> ''
        GROUP BY fr.route_id, fr.origin_id, fr.destination_id;
        CREATE INDEX IF NOT EXISTS ix_fm_pair  ON fare_matrix(from_zone, to_zone);
        CREATE INDEX IF NOT EXISTS ix_fm_route ON fare_matrix(route_id, from_zone, to_zone);
    """)
    _log(f"  ok    fare_matrix       {cur.execute('SELECT COUNT(*) FROM fare_matrix').fetchone()[0]:>9,} rows")

    cur.executescript("""
        DROP TABLE IF EXISTS zone_centroid;
        CREATE TABLE zone_centroid AS
        SELECT zone_id,
               AVG(CAST(stop_lat AS REAL)) AS lat,
               AVG(CAST(stop_lon AS REAL)) AS lon,
               COUNT(*)                    AS n_stops
        FROM stops
        WHERE zone_id IS NOT NULL AND zone_id <> ''
          AND stop_lat <> '' AND stop_lon <> ''
        GROUP BY zone_id;
    """)

    cur.executescript("""
        DROP TABLE IF EXISTS route_stats;
        CREATE TABLE route_stats AS
        SELECT route_id, COUNT(*) AS n_trips FROM trips GROUP BY route_id;
        CREATE INDEX IF NOT EXISTS ix_rs_stats ON route_stats(route_id);
    """)

    # readable join surface used by route cards / debugging
    cur.executescript("""
        DROP VIEW IF EXISTS route_stops_full;
        CREATE VIEW route_stops_full AS
        SELECT rs.route_id, r.route_short_name, r.route_long_name,
               rs.direction_id, rs.stop_id, s.stop_name,
               CAST(s.stop_lat AS REAL) AS stop_lat, CAST(s.stop_lon AS REAL) AS stop_lon,
               rs.seq
        FROM route_stops rs
        JOIN routes r ON r.route_id = rs.route_id
        JOIN stops  s ON s.stop_id  = rs.stop_id;
    """)
    con.commit()


def build_metro(con: sqlite3.Connection) -> None:
    _log("D2  Namma Metro (BMRCL) GTFS")
    cur = con.cursor()
    for t in ["agency", "routes", "stops", "trips", "fare_attributes", "fare_rules"]:
        path = RAW_GTFS_BMRCL / f"{t}.txt"
        if not path.exists():
            continue
        load_csv(con, path, f"metro_{t}")
    load_csv(con, RAW_GTFS_BMRCL / "stop_times.txt", "metro_stop_times")

    cur.executescript("""
        DROP TABLE IF EXISTS metro_route_stops;
        -- Use the LONGEST trip per (route, direction) as the canonical line order.
        -- Namma Metro runs short-loop services; taking MIN(seq) across every trip
        -- interleaves those loops and produces a scrambled station order.
        CREATE TABLE metro_route_stops AS
        WITH trip_len AS (
            SELECT t.route_id, COALESCE(t.direction_id,'0') AS dir, st.trip_id, COUNT(*) AS n
            FROM metro_stop_times st JOIN metro_trips t ON t.trip_id = st.trip_id
            GROUP BY t.route_id, dir, st.trip_id
        ), rep AS (
            SELECT route_id, dir, trip_id FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY route_id, dir ORDER BY n DESC) AS rn
                FROM trip_len
            ) WHERE rn = 1
        )
        SELECT rep.route_id AS route_id,
               rep.dir      AS direction_id,
               COALESCE(s.parent_station, st.stop_id) AS station_id,
               CAST(st.stop_sequence AS INTEGER)      AS seq
        FROM rep
        JOIN metro_stop_times st ON st.trip_id = rep.trip_id
        LEFT JOIN metro_stops s ON s.stop_id = st.stop_id
        GROUP BY rep.route_id, rep.dir, COALESCE(s.parent_station, st.stop_id);
        CREATE INDEX IF NOT EXISTS ix_mrs_station ON metro_route_stops(station_id);
        CREATE INDEX IF NOT EXISTS ix_mrs_route   ON metro_route_stops(route_id, direction_id, seq);

        DROP TABLE IF EXISTS metro_fare_matrix;
        CREATE TABLE metro_fare_matrix AS
        SELECT fr.origin_id                  AS from_station,
               fr.destination_id             AS to_station,
               MIN(CAST(fa.price AS REAL))   AS fare_inr,
               MAX(CAST(fa.price AS REAL))   AS fare_max_inr
        FROM metro_fare_rules fr
        JOIN metro_fare_attributes fa ON fa.fare_id = fr.fare_id
        WHERE fr.origin_id <> '' AND fr.destination_id <> ''
        GROUP BY fr.origin_id, fr.destination_id;
        CREATE INDEX IF NOT EXISTS ix_mfm_pair ON metro_fare_matrix(from_station, to_station);
    """)
    con.commit()
    for tbl in ["metro_stops", "metro_route_stops", "metro_fare_matrix"]:
        _log(f"  ok    {tbl:<18} {cur.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]:>9,} rows")


def build_meta(con: sqlite3.Connection, counts: dict[str, int]) -> None:
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS meta")
    cur.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    meta = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bmtc_feed_start": "2026-09-07",
        "bmtc_feed_end": "2027-09-07",
        "bmrcl_feed_start": "2026-08-17",
        "bmrcl_feed_end": "2027-08-17",
        **{f"n_{k}": str(v) for k, v in counts.items()},
    }
    cur.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    con.commit()


def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
        _log(f"removed old {DB_PATH.name}")
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")

    build_bmtc(con)
    build_metro(con)

    cur = con.cursor()
    counts = {
        "stops": cur.execute("SELECT COUNT(*) FROM stops").fetchone()[0],
        "routes": cur.execute("SELECT COUNT(*) FROM routes").fetchone()[0],
        "trips": cur.execute("SELECT COUNT(*) FROM trips").fetchone()[0],
        "stop_times": cur.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0],
        "route_stops": cur.execute("SELECT COUNT(*) FROM route_stops").fetchone()[0],
        "fare_rules": cur.execute("SELECT COUNT(*) FROM fare_rules").fetchone()[0],
        "metro_stations": cur.execute(
            "SELECT COUNT(*) FROM metro_stops WHERE location_type='1'").fetchone()[0],
    }
    build_meta(con, counts)
    con.commit()
    con.close()

    size_mb = DB_PATH.stat().st_size / 1e6
    _log(f"\nDone -> {DB_PATH}  ({size_mb:.1f} MB)")
    for k, v in counts.items():
        _log(f"  {k:<16} {v:>9,}")


if __name__ == "__main__":
    sys.exit(main())
