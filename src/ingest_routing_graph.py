"""Derived tables for the graph-search routers (Dijkstra / A* / RRT).

The routers need an *edge list with a time on it*. GTFS gives us that implicitly:
every trip lists its stops in order with arrival/departure times, so the scheduled
run time of a segment is the difference between consecutive `stop_times` rows.

This script collapses 1.54 M BMTC stop_times into one row per
(route, direction, from_stop, to_stop):

    route_segment   route_id, direction_id, from_stop, to_stop, km, minutes, n_trips

and does the same for the metro network (`metro_segment`). Times are the mean across
every trip that runs the segment, which is the right statistic here: we want a typical
headway-independent run time, not a specific departure.

Run:  python -m src.ingest_routing_graph
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pandas as pd

from src.config import DB_PATH, RAW_GTFS_BMRCL, RAW_GTFS_BMTC
from src.tools.common import haversine_km


def _hms_to_sec(text: str) -> float:
    try:
        h, m, s = (int(x) for x in str(text).split(":"))
    except (ValueError, AttributeError):
        return float("nan")
    return h * 3600 + m * 60 + s


def build_segments(kind: str, stop_times_csv: Path, trips_csv: Path,
                   out_table: str) -> int:
    t0 = time.time()
    st = pd.read_csv(stop_times_csv, dtype=str,
                     usecols=["trip_id", "arrival_time", "departure_time", "stop_id",
                              "stop_sequence"])
    trips = pd.read_csv(trips_csv, dtype=str, usecols=["trip_id", "route_id", "direction_id"])

    st["seq"] = st["stop_sequence"].astype(int)
    st = st.sort_values(["trip_id", "seq"], kind="mergesort")
    st["arr"] = st["arrival_time"].map(_hms_to_sec)
    st["dep"] = st["departure_time"].map(_hms_to_sec)

    nxt_stop = st.groupby("trip_id", sort=False)["stop_id"].shift(-1)
    nxt_arr = st.groupby("trip_id", sort=False)["arr"].shift(-1)
    st["to_stop"] = nxt_stop
    st["run_sec"] = nxt_arr - st["dep"]          # dwell included in the following leg
    seg = st.dropna(subset=["to_stop", "run_sec"])
    # a handful of feeds have midnight rollover artefacts; clamp to a sane window
    seg = seg[(seg["run_sec"] > 30) & (seg["run_sec"] < 3 * 3600)]

    seg = seg.merge(trips, on="trip_id", how="inner")
    agg = (seg.groupby(["route_id", "direction_id", "stop_id", "to_stop"], sort=False)
              .agg(minutes=("run_sec", lambda s: round(float(s.mean()) / 60.0, 2)),
                   n_trips=("trip_id", "size"))
              .reset_index()
              .rename(columns={"stop_id": "from_stop"}))
    print(f"  {kind}: {len(seg):,} trip-segments -> {len(agg):,} unique "
          f"(route, direction, from, to) rows  [{time.time()-t0:.1f}s]", flush=True)
    return agg


def attach_distance(con: sqlite3.Connection, df: pd.DataFrame, stop_table: str,
                    id_col: str) -> pd.DataFrame:
    coords = pd.read_sql(f"SELECT {id_col} AS sid, CAST(stop_lat AS REAL) AS lat, "
                         f"CAST(stop_lon AS REAL) AS lon FROM {stop_table}", con)
    cmap = coords.set_index("sid")[["lat", "lon"]].to_dict("index")
    km = []
    for a, b in zip(df["from_stop"], df["to_stop"]):
        ca, cb = cmap.get(a), cmap.get(b)
        if not ca or not cb or ca["lat"] is None:
            km.append(None)
        else:
            km.append(round(haversine_km(ca["lat"], ca["lon"], cb["lat"], cb["lon"]), 3))
    df["km"] = km
    return df


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE INDEX IF NOT EXISTS ix_stops_id ON stops(stop_id)")

    bus = build_segments("BMTC", RAW_GTFS_BMTC / "stop_times.txt",
                         RAW_GTFS_BMTC / "trips.txt", "route_segment")
    bus = attach_distance(con, bus, "stops", "stop_id")
    bus.to_sql("route_segment", con, if_exists="replace", index=False)

    met = build_segments("METRO", RAW_GTFS_BMRCL / "stop_times.txt",
                         RAW_GTFS_BMRCL / "trips.txt", "metro_segment")
    met = attach_distance(con, met, "metro_stops", "stop_id")
    met.to_sql("metro_segment", con, if_exists="replace", index=False)

    con.executescript("""
        CREATE INDEX IF NOT EXISTS ix_rseg_from ON route_segment(from_stop);
        CREATE INDEX IF NOT EXISTS ix_rseg_route ON route_segment(route_id, direction_id);
        CREATE INDEX IF NOT EXISTS ix_mseg_from ON metro_segment(from_stop);
        CREATE INDEX IF NOT EXISTS ix_mseg_route ON metro_segment(route_id, direction_id);
        CREATE INDEX IF NOT EXISTS ix_stops_geo ON stops(stop_lat, stop_lon);
        CREATE INDEX IF NOT EXISTS ix_mstops_geo ON metro_stops(stop_lat, stop_lon);
    """)
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('routing_graph_built', ?)",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
    con.commit()

    for t in ("route_segment", "metro_segment"):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        miss = con.execute(f"SELECT COUNT(*) FROM {t} WHERE km IS NULL").fetchone()[0]
        print(f"  {t:16} {n:>7,} rows   {miss} without coords")
    con.close()
    print("done")


if __name__ == "__main__":
    main()
