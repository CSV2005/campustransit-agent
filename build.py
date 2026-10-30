#!/usr/bin/env python3
"""Build (or rebuild) the whole data layer with one command.

    python build.py               # download anything missing, then build everything
    python build.py --data-only    # skip corpus + vector index
    python build.py --force        # rebuild even if artefacts look healthy

Everything here is reproducible from public sources. The only large download is the
BMTC GTFS zip (~44 MB) from github.com/Vonter/bmtc-gtfs; the BMRCL feed and the curated
policy documents ship with the repository.

Run this after a fresh clone, and any time `data/processed/transit.db` is missing.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GTFS = ROOT / "data" / "raw" / "gtfs"
BMTC_DIR = GTFS / "bmtc"
BMTC_ZIP_URL = "https://raw.githubusercontent.com/Vonter/bmtc-gtfs/main/gtfs/bmtc.zip"
BMTC_FILES = ["agency.txt", "routes.txt", "trips.txt", "stops.txt", "stop_times.txt",
              "fare_attributes.txt", "fare_rules.txt", "calendar.txt", "shapes.txt"]


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_bmtc(force: bool = False) -> None:
    have = [f for f in BMTC_FILES if (BMTC_DIR / f).exists()]
    if len(have) == len(BMTC_FILES) and not force:
        log(f"  ✓ BMTC GTFS present ({len(have)} files)")
        return
    BMTC_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = BMTC_DIR / "bmtc.zip"
    if force or not zip_path.exists() or zip_path.stat().st_size < 10_000_000:
        log(f"  ↓ downloading BMTC GTFS (~44 MB) …")
        t0 = time.time()
        with urllib.request.urlopen(BMTC_ZIP_URL, timeout=180) as r, \
                open(zip_path, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        log(f"    got {zip_path.stat().st_size / 1e6:.1f} MB in {time.time() - t0:.1f}s")
    log("  ⇣ extracting …")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(BMTC_DIR)
    missing = [f for f in BMTC_FILES if not (BMTC_DIR / f).exists()]
    if missing:
        raise SystemExit(f"BMTC archive is missing {missing}")


def run(module: str) -> None:
    log(f"\n▶ python -m {module}")
    t0 = time.time()
    proc = subprocess.run([sys.executable, "-m", module], cwd=ROOT)
    if proc.returncode != 0:
        raise SystemExit(f"{module} failed with exit code {proc.returncode}")
    log(f"  done in {time.time() - t0:.1f}s")


def slim_db() -> None:
    """Repack the two tables that dominate the file size.

    `stop_times` is 1.54 M rows and the app only ever asks it one question: *does some
    trip on this route visit A before B?* It never needs the clock, because those times
    were already aggregated into `route_segment` by ingest_routing_graph.

    Keeping it with two TEXT columns plus two indexes costs 76 MB. Repacked onto integer
    keys with a clustered primary key it costs a fraction of that, which matters because
    the whole point of this build is that the repository travels.
    """
    import sqlite3

    from src.config import DB_PATH

    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")

    has_old = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stop_times'").fetchone()
    if has_old:
        log("  · repacking stop_times onto integer keys")
        con.executescript("""
            DROP TABLE IF EXISTS trip_key;
            DROP TABLE IF EXISTS stop_key;
            DROP TABLE IF EXISTS trip_stops;
            CREATE TABLE trip_key(k INTEGER PRIMARY KEY, trip_id TEXT);
            INSERT INTO trip_key(k, trip_id)
              SELECT ROW_NUMBER() OVER (ORDER BY trip_id) - 1, trip_id FROM trips;
            CREATE UNIQUE INDEX ix_trip_key ON trip_key(trip_id);
            CREATE TABLE stop_key(k INTEGER PRIMARY KEY, stop_id TEXT);
            INSERT INTO stop_key(k, stop_id)
              SELECT ROW_NUMBER() OVER (ORDER BY stop_id) - 1, stop_id FROM stops;
            CREATE UNIQUE INDEX ix_stop_key ON stop_key(stop_id);
            CREATE TABLE trip_stops(
              trip INTEGER NOT NULL, seq INTEGER NOT NULL, stop INTEGER NOT NULL,
              PRIMARY KEY (trip, seq)) WITHOUT ROWID;
            INSERT INTO trip_stops(trip, seq, stop)
              SELECT tk.k, CAST(st.stop_sequence AS INTEGER), sk.k
              FROM stop_times st
              JOIN trip_key tk ON tk.trip_id = st.trip_id
              JOIN stop_key sk ON sk.stop_id = st.stop_id;
            DROP TABLE stop_times;
            DROP INDEX ix_trip_key;
            DROP INDEX ix_stop_key;
        """)
        con.commit()

    con.execute("VACUUM")
    con.close()
    log(f"  · database is {DB_PATH.stat().st_size / 1e6:.0f} MB")


def prune() -> None:
    """Keep the repository small enough to travel.

    The BMTC archive extracts to 238 MB, most of it `shapes.txt` (114 MB of polyline
    geometry this agent never draws) and `stop_times.txt` (81 MB, already aggregated into
    `route_segment`). Neither is needed at runtime, and both are one download away — so
    they are deleted after a successful build. `build.py` re-fetches them automatically
    if you ever rebuild.
    """
    import sqlite3

    from src.config import DB_PATH

    freed = 0
    for name in ("shapes.txt", "stop_times.txt", "bmtc.zip"):
        p = BMTC_DIR / name
        if p.exists():
            freed += p.stat().st_size
            p.unlink()
    bmrcl_shapes = GTFS / "bmrcl" / "shapes.txt"
    if bmrcl_shapes.exists():
        freed += bmrcl_shapes.stat().st_size
        bmrcl_shapes.unlink()

    con = sqlite3.connect(DB_PATH)
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                   "name='metro_stop_times'").fetchone():
        con.execute("DROP TABLE metro_stop_times")   # unused at runtime
    con.execute("VACUUM")
    con.close()
    log(f"  · pruned {freed / 1e6:.0f} MB of raw GTFS the app does not read")


def health() -> bool:
    from src.config import CHROMA_DIR, DB_PATH, PROCESSED
    return (DB_PATH.exists() and DB_PATH.stat().st_size > 50_000_000
            and (PROCESSED / "corpus.jsonl").exists()
            and (PROCESSED / "qwen_sft.jsonl").exists()
            and (PROCESSED / "dataset_search.sqlite3").exists()
            and (PROCESSED / "index_meta.json").exists()
            and CHROMA_DIR.exists())


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the CampusTransit data layer.")
    ap.add_argument("--force", action="store_true", help="rebuild even if healthy")
    ap.add_argument("--data-only", action="store_true", help="skip corpus, search indexes and vector index")
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()

    if health() and not args.force:
        log("Data layer already built — nothing to do (use --force to rebuild).")
        return 0

    log("CampusTransit — building the data layer")
    log("=" * 62)
    if not args.skip_download:
        ensure_bmtc(force=args.force)

    for module in ("src.ingest_gtfs", "src.ingest_places", "src.ingest_ksrtc",
                   "src.ingest_metro_pink", "src.ingest_bmrcl_official",
                   "src.ingest_routing_graph"):
        run(module)

    log("\n▶ slimming + vacuuming the database")
    slim_db()
    log("▶ pruning raw files the app never reads")
    prune()

    if not args.data_only:
        run("src.build_corpus")
        run("src.export_qwen_sft")
        run("src.build_index")
        run("src.build_data_index")
    run("src.make_manifest")

    from src.config import DB_PATH
    log("\n" + "=" * 62)
    log(f"Done.  {DB_PATH.stat().st_size / 1e6:.0f} MB database, "
        f"{(ROOT / 'data/processed/corpus.jsonl').stat().st_size / 1e3:.0f}k corpus.")
    log("Start the app with:  python run.py --open")
    return 0


if __name__ == "__main__":
    sys.exit(main())
