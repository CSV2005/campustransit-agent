"""Metadata endpoints: capabilities, data sources, coverage (v1)."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Query

from src.config import DB_PATH
from src.services import http, places, source_audit, store
from src.services.providers import providers

router = APIRouter(prefix="/api/v1", tags=["metadata"])


@router.get("/capabilities")
def capabilities() -> dict:
    """What is configured, what is not, and why — for every optional integration."""
    cfg = providers()
    return {
        "providers": cfg.capabilities(),
        "map": cfg.google_public(),
        "summary": {
            "available": [c["key"] for c in cfg.capabilities() if c["available"]],
            "unavailable": [c["key"] for c in cfg.capabilities() if not c["available"]],
        },
        "note": ("An integration listed as unavailable is absent, not degraded: the app "
                 "reports the gap instead of substituting an estimate."),
    }


@router.get("/data-sources")
def data_sources(include_calls: bool = Query(default=True),
                 probe: bool = Query(default=False)) -> dict:
    """Registry of every dataset and provider this app depends on, with status.

    ``seed`` derives status from row counts already in SQLite. ``probe=true`` also asks
    each upstream URL over the network and records the real HTTP answer, which is slower
    and is why it is not the default.
    """
    rows = source_audit.probe() if probe else source_audit.seed()
    con = sqlite3.connect(DB_PATH)
    counts = {}
    for table in ("stops", "routes", "trips", "route_segment", "places", "metro_stops",
                  "trip_stops", "ksrtc_corridor"):
        try:
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            counts[table] = None
    fare_row = con.execute(
        "SELECT source_document, n_matched, n_pairs, n_mismatched, checked_at "
        "FROM metro_fare_verification ORDER BY checked_at DESC LIMIT 1"
    ).fetchone() if _has(con, "metro_fare_verification") else None
    con.close()

    latest_success = {r["source_name"]: r["last_success"] for r in rows}
    return {
        "sources": rows,
        "row_counts": counts,
        "places_by_kind": places.kinds_summary(),
        "fare_verification": (
            {"label": fare_row[0], "matched": fare_row[1], "pairs": fare_row[2],
             "mismatches": fare_row[3], "checked_at": fare_row[4],
             "all_matched": fare_row[1] == fare_row[2]} if fare_row else None),
        "recent_api_calls": http.recent_calls(25) if include_calls else [],
        "last_success": latest_success,
        "gtfs_note": ("BMTC GTFS carries no shapes.txt or stop_times.txt in this import, so "
                      "leg geometry is drawn stop-to-stop from real coordinates rather than "
                      "along road centrelines."),
    }


def _has(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone() is not None


@router.get("/coverage")
def coverage() -> dict:
    """Which transport modes and operators the data actually covers."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        lines = [dict(r) for r in con.execute(
            "SELECT * FROM metro_line_status ORDER BY line_name")]
    except sqlite3.Error:
        lines = []
    ksrtc = []
    try:
        ksrtc = [dict(r) for r in con.execute("SELECT * FROM ksrtc_corridor")]
    except sqlite3.Error:
        pass
    con.close()
    return {
        "bus": {"operator": "BMTC", "in_scope": True,
                "source": "Vonter/bmtc-gtfs (GTFS static)"},
        "metro": {"operator": "BMRCL (Namma Metro)", "in_scope": True,
                  "lines": lines},
        "intercity": {"operator": "KSRTC", "in_scope": "corridors only", "rows": ksrtc},
        "autos_and_cabs": {"in_scope": False,
                           "reason": "no licensed fare or availability data in the repo"},
        "walking": {"in_scope": True,
                    "note": "walk edges are generated in the routing graph from stop "
                            "coordinates; there are no footpath geometries"},
        "note": ("Pink Line is listed as under_construction and is never returned as an "
                 "operational route. It appears in planning only where the brief requires "
                 "the proposed alignment to be shown, and is always labelled."),
    }
