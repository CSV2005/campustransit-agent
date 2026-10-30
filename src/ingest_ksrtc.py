"""D4 -> KSRTC corridor table in SQLite.

data.gov.in (the dataset the build guide specifies for D4) was unreachable from
the build environment on 2026-10-22: `www.data.gov.in` returned HTTP 503,
`data.gov.in` and `api.data.gov.in` returned no response, and the Karnataka
mirror `karnataka.data.gov.in` did not resolve. `kstrc.karnataka.gov.in` was also
unreachable, and the KSRTC booking site (www.ksrtc.in) is a JS app whose
availability endpoint 302-redirects unauthenticated clients.

So D4 is satisfied the only defensible way: a small, hand-curated corridor table
where every fare carries `fare_basis`, `source_url`, `published_date` and
`verification_status`. Published fares are kept as-is; the one interpolated fare
is labelled UNVERIFIED_DERIVED and the agent is forced to say so.

Run:  python -m src.ingest_ksrtc
"""
from __future__ import annotations

import csv
import sqlite3
import sys

from src.config import CURATED, DB_PATH


def main() -> None:
    path = CURATED / "ksrtc_corridor.csv"
    if not path.exists():
        raise SystemExit(f"missing {path}")
    if not DB_PATH.exists():
        raise SystemExit("transit.db missing - run `python -m src.ingest_gtfs` first")

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DROP TABLE IF EXISTS ksrtc_corridor")
    cur.execute("""
        CREATE TABLE ksrtc_corridor(
            corridor_id TEXT PRIMARY KEY,
            service_class TEXT, from_place TEXT, to_place TEXT, via TEXT,
            approx_km REAL, fare_inr REAL, fare_basis TEXT, source_url TEXT,
            publisher TEXT, published_date TEXT, last_verified TEXT,
            verification_status TEXT, notes TEXT
        )""")
    cur.executemany(
        "INSERT OR REPLACE INTO ksrtc_corridor VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["corridor_id"], r["service_class"], r["from_place"], r["to_place"], r["via"],
          float(r["approx_km"]) if r["approx_km"] else None,
          float(r["fare_inr"]) if r["fare_inr"] else None,
          r["fare_basis"], r["source_url"], r["publisher"], r["published_date"],
          r["last_verified"], r["verification_status"], r["notes"]) for r in rows])
    con.commit()
    print(f"ksrtc_corridor: {len(rows)} rows")
    for r in cur.execute("SELECT corridor_id, fare_inr, fare_basis, verification_status "
                         "FROM ksrtc_corridor"):
        print(f"  {r[0]:<32} Rs {str(r[1]):>5}  {r[2]:<22} {r[3]}")
    con.close()


if __name__ == "__main__":
    sys.exit(main())
