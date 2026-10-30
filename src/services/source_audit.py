"""Build the data-source registry from what the database and environment actually contain.

Two modes:

* ``seed()`` — offline. Writes a row per dataset using row counts read from SQLite and the
  verification status already recorded by the ingest pipeline. No network, no guessing.
* ``probe()`` — additionally issues a HEAD/GET against each upstream URL and records the
  real HTTP status. Slow, so it is opt-in via ``/api/v1/data-sources?probe=true``.

The distinction matters: a source is only marked ``ok`` when something was actually
confirmed. Otherwise it is ``imported`` (rows present) or ``unavailable`` (absent).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx

from src.config import DB_PATH
from src.services import store
from src.services.providers import providers

ROOT = Path(__file__).resolve().parents[2]

# name, data_type, url, licence, table-or-env
DATASETS = [
    ("BMTC GTFS (static)", "gtfs", "https://github.com/Vonter/bmtc-gtfs",
     "ODbL-ish community extract; see repo", "stops"),
    ("BMRCL GTFS (static)", "gtfs", "https://github.com/Vonter/bmrcl-gtfs",
     "Community extract; see repo", "metro_stops"),
    ("BMRCL fare chart 14.02.2025", "fares",
     "https://english.bmrc.co.in:8282/English/uploads/news/english//fileuploads/"
     "Revised_Fare_Chart_for_webiste.pdf", "BMRCL published chart", "metro_fare_matrix"),
    ("OSM places extract (Overpass)", "poi", "https://overpass-api.de/api",
     "ODbL 1.0", "places"),
    ("KSRTC corridor fares", "fares", "https://ksrtc.in", "KSRTC published fares",
     "ksrtc_corridor"),
    ("Metro line status (Wikipedia)", "reference", "https://en.wikipedia.org/wiki/Pink_Line_(Namma_Metro)",
     "CC BY-SA 4.0", "metro_line_status"),
    ("Policy corpus + Chroma index", "rag", "local", "mixed, per document",
     "chroma"),
]

PROVIDERS = [
    ("Google Maps JavaScript API", "google_maps_js"),
    ("Google Places API", "google_places"),
    ("Google Routes API", "google_routes"),
    ("OpenStreetMap raster tiles", "osm_tiles"),
]


def _count(table: str) -> int | None:
    if table == "chroma":
        idx = ROOT / "data" / "processed" / "index_meta.json"
        if not idx.exists():
            return None
        import json
        try:
            meta = json.loads(idx.read_text())
            return int(meta.get("count") or meta.get("documents") or 0)
        except (ValueError, OSError):
            return None
    try:
        con = sqlite3.connect(DB_PATH)
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        con.close()
        return n
    except sqlite3.Error:
        return None


def _verification(table: str) -> str:
    """Read the provenance tag the ingest pipeline wrote, if there is one."""
    try:
        con = sqlite3.connect(DB_PATH)
        if table == "metro_fare_matrix":
            row = con.execute(
                "SELECT n_matched, n_pairs, n_mismatched, checked_at, source_document "
                "FROM metro_fare_verification ORDER BY checked_at DESC LIMIT 1").fetchone()
            con.close()
            if row:
                return (f"{row[4]}: {row[0]}/{row[1]} pairs match, {row[2]} mismatches, "
                        f"checked {row[3]}")
            return "no verification row"
        if table == "ksrtc_corridor":
            rows = con.execute("SELECT verification_status, COUNT(*) FROM ksrtc_corridor "
                               "GROUP BY 1").fetchall()
            con.close()
            return ", ".join(f"{s}={n}" for s, n in rows) or "no rows"
        if table == "metro_line_status":
            rows = con.execute("SELECT status, COUNT(*) FROM metro_line_status GROUP BY 1").fetchall()
            con.close()
            return ", ".join(f"{s}={n}" for s, n in rows)
        con.close()
    except sqlite3.Error:
        pass
    return ""


def seed() -> list[dict]:
    """Offline: rows present in the DB become ``imported`` / ``ok``, absent stay unknown."""
    for name, dtype, url, licence, table in DATASETS:
        n = _count(table)
        if n is None:
            status, detail = "unavailable", f"table '{table}' missing or empty"
        elif n == 0:
            status, detail = "unavailable", f"table '{table}' is empty"
        elif table == "metro_fare_matrix":
            status, detail = "ok", _verification(table)
        else:
            status, detail = "imported", f"{n:,} rows from {table}"
        store.record_source(source_name=name, data_type=dtype, url=url, status=status,
                            detail=detail, licence=licence, rows=n)

    cfg = providers()
    for label, key in PROVIDERS:
        cap = cfg.caps.get(key)
        if not cap:
            continue
        status = "available" if cap.available else "needs_configuration"
        store.record_source(source_name=label, data_type="api",
                            url=cap.docs or "", status=status,
                            detail=cap.reason, licence="Google Maps Platform ToS"
                            if key.startswith("google") else "OSM tile usage policy")
    return store.list_sources()


def probe(timeout: float = 6.0) -> list[dict]:
    """Live: ask each upstream URL what it says, and record the actual answer."""
    seed()
    rows = store.list_sources()
    for r in rows:
        url = (r.get("url") or "").strip()
        if not url or not url.startswith("http"):
            continue
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True,
                              headers={"User-Agent": "CampusTransit/1.0 (source audit)"}) as c:
                resp = c.head(url)
                if resp.status_code >= 400:
                    resp = c.get(url)
            ok = resp.status_code < 400
            store.record_source(
                source_name=r["source_name"], data_type=r.get("data_type") or "",
                url=url, status="ok" if ok else "error",
                detail=f"HTTP {resp.status_code} from {url}"[:400],
                licence=r.get("licence") or "", rows=r.get("rows"))
        except httpx.HTTPError as exc:
            store.record_source(
                source_name=r["source_name"], data_type=r.get("data_type") or "",
                url=url, status="error",
                detail=f"{type(exc).__name__}: {exc}"[:400],
                licence=r.get("licence") or "", rows=r.get("rows"))
    return store.list_sources()
