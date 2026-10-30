"""Write a small GTFS-Realtime Vehicle Positions feed with a *current* timestamp.

Purpose: let a developer exercise the live layer end to end without a credential and
without pretending a real feed exists. Everything it writes is fabricated on purpose, so
it prints a DEMO DATA banner and must never be pointed at the app as if it were BMTC.

    python tests/make_fixture_feed.py --out /tmp/feed/vp.pb
    python -m http.server 8099 --bind 127.0.0.1 -d /tmp/feed
    GTFS_RT_VEHICLE_URL=http://127.0.0.1:8099/vp.pb python -m uvicorn web.server:app --port 7862

Three vehicles, all on real Bengaluru corridors so the markers land somewhere sensible:
one fresh, one a few seconds old, and one deliberately 240 s old to exercise the
"delayed" path.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from google.transit import gtfs_realtime_pb2 as pb

# (vehicle id, route label, route id, lat, lon, bearing deg, speed m/s, age seconds)
DEMO_VEHICLES = [
    ("KA01F1234", "500-D", "500D", 12.9716, 77.5946, 95.0, 9.4, 0),
    ("KA01F5678", "335-E", "335E", 12.9352, 77.6245, 12.0, 6.1, 8),
    ("KA01F9012", "KBS-9", "KBS9", 12.9956, 77.5730, 200.0, 0.0, 240),
]


def build(now: int | None = None) -> bytes:
    now = int(now if now is not None else time.time())
    msg = pb.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.incrementality = pb.FeedHeader.FULL_DATASET
    msg.header.timestamp = now
    for vid, label, rid, lat, lon, bearing, speed, age in DEMO_VEHICLES:
        entity = msg.entity.add()
        entity.id = vid
        v = entity.vehicle
        v.vehicle.id = vid
        v.vehicle.label = label
        v.trip.route_id = rid
        v.trip.trip_id = f"{rid}-trip"
        v.position.latitude = lat
        v.position.longitude = lon
        v.position.bearing = bearing
        v.position.speed = speed
        v.timestamp = now - age
    return msg.SerializeToString()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="fixture_vp.pb", help="path to write")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    blob = build()
    out.write_bytes(blob)
    print("DEMO DATA -- fabricated positions, not BMTC.")
    print(f"wrote {len(blob)} bytes to {out} ({len(DEMO_VEHICLES)} vehicles, 1 deliberately stale)")


if __name__ == "__main__":
    main()
