"""User state: saved routes, travel history, preferences, data-source status.

Single-user by design (``user_id`` defaults to ``local``) but written with the column in
place so nothing has to be migrated later. Every write validates through Pydantic at the
API layer; this module only touches SQLite.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from src.config import DB_PATH
from src.db.extra_schema import now_iso


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


# ------------------------------------------------------------------ saved routes
def save_route(*, origin: str, destination: str, route_id: str = "", title: str = "",
               payload: dict, user_id: str = "local") -> dict:
    con = _con()
    cur = con.execute(
        "INSERT INTO saved_routes(user_id, origin, destination, route_id, title, "
        "payload_json, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, origin, destination, route_id, title or f"{origin} → {destination}",
         json.dumps(payload, separators=(",", ":")), now_iso()))
    rid = cur.lastrowid
    con.commit()
    con.close()
    return {"id": rid, "origin": origin, "destination": destination,
            "route_id": route_id, "title": title or f"{origin} → {destination}",
            "created_at": now_iso()}


def list_saved(user_id: str = "local", limit: int = 50) -> list[dict]:
    con = _con()
    rows = con.execute(
        "SELECT id, origin, destination, route_id, title, created_at FROM saved_routes "
        "WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_saved(save_id: int, user_id: str = "local") -> dict | None:
    con = _con()
    row = con.execute("SELECT * FROM saved_routes WHERE id=? AND user_id=?",
                      (save_id, user_id)).fetchone()
    con.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
    except json.JSONDecodeError:
        d["payload"] = {}
        d["payload_error"] = "stored payload was not valid JSON"
    return d


def delete_saved(save_id: int, user_id: str = "local") -> bool:
    con = _con()
    changed = con.execute("DELETE FROM saved_routes WHERE id=? AND user_id=?",
                          (save_id, user_id)).rowcount
    con.commit()
    con.close()
    return bool(changed)


# ----------------------------------------------------------------- travel history
def add_history(*, origin: str, destination: str, departure_time: str = "",
                selected_mode: str = "", fare: float | None = None,
                duration: int | None = None, route_id: str = "",
                user_id: str = "local") -> dict:
    con = _con()
    cur = con.execute(
        "INSERT INTO travel_history(user_id, origin, destination, departure_time, "
        "selected_mode, estimated_fare_inr, estimated_duration, route_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, origin, destination, departure_time, selected_mode, fare, duration,
         route_id, now_iso()))
    hid = cur.lastrowid
    con.commit()
    con.close()
    return {"id": hid, "origin": origin, "destination": destination,
            "created_at": now_iso()}


def list_history(user_id: str = "local", limit: int = 50, offset: int = 0) -> list[dict]:
    con = _con()
    rows = con.execute(
        "SELECT id, origin, destination, departure_time, selected_mode, "
        "estimated_fare_inr, estimated_duration, route_id, created_at "
        "FROM travel_history WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def history_stats(user_id: str = "local") -> dict:
    con = _con()
    row = con.execute(
        "SELECT COUNT(*) AS trips, COALESCE(SUM(estimated_fare_inr),0) AS spend, "
        "COALESCE(AVG(estimated_fare_inr),0) AS avg_fare, "
        "COALESCE(SUM(estimated_duration),0) AS minutes "
        "FROM travel_history WHERE user_id=?", (user_id,)).fetchone()
    modes = con.execute(
        "SELECT COALESCE(selected_mode,'unknown') AS mode, COUNT(*) AS n "
        "FROM travel_history WHERE user_id=? GROUP BY 1 ORDER BY 2 DESC",
        (user_id,)).fetchall()
    top = con.execute(
        "SELECT destination, COUNT(*) AS n FROM travel_history WHERE user_id=? "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 3", (user_id,)).fetchall()
    con.close()
    return {"trips_logged": row["trips"],
            "total_fare_inr": round(row["spend"], 2),
            "average_fare_inr": round(row["avg_fare"], 2),
            "total_minutes": row["minutes"],
            "by_mode": [dict(m) for m in modes],
            "top_destinations": [dict(t) for t in top]}


def clear_history(user_id: str = "local") -> int:
    con = _con()
    n = con.execute("DELETE FROM travel_history WHERE user_id=?", (user_id,)).rowcount
    con.commit()
    con.close()
    return n


# ------------------------------------------------------------------- preferences
DEFAULTS = {
    "default_preference": "balanced",
    "default_origin": "",
    "map_provider": "auto",
    "theme": "dark",
    "units": "metric",
    "show_traffic_overlay": "false",
}


def get_prefs(user_id: str = "local") -> dict:
    con = _con()
    rows = con.execute("SELECT key, value FROM preferences WHERE user_id=?",
                       (user_id,)).fetchall()
    con.close()
    prefs = dict(DEFAULTS)
    prefs.update({r["key"]: r["value"] for r in rows})
    return prefs


def set_prefs(values: dict, user_id: str = "local") -> dict:
    con = _con()
    ts = now_iso()
    for k, v in values.items():
        if k not in DEFAULTS:
            continue
        con.execute(
            "INSERT INTO preferences(user_id, key, value, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at", (user_id, k, str(v), ts))
    con.commit()
    con.close()
    return get_prefs(user_id)


# ------------------------------------------------------------- data source status
def record_source(*, source_name: str, data_type: str, url: str, status: str,
                  detail: str = "", licence: str = "", rows: int | None = None,
                  checked: bool = True) -> None:
    con = _con()
    ts = now_iso()
    success = ts if status in ("ok", "available", "imported", "operational") else None
    con.execute(
        "INSERT INTO data_source_status(source_name, data_type, url, last_checked, "
        "last_success, status, detail, licence, rows) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(source_name) DO UPDATE SET "
        "  last_checked=excluded.last_checked, "
        "  last_success=COALESCE(excluded.last_success, data_source_status.last_success), "
        "  status=excluded.status, detail=excluded.detail, rows=excluded.rows",
        (source_name, data_type, url, ts if checked else None, success, status,
         detail, licence, rows))
    con.commit()
    con.close()


def list_sources() -> list[dict]:
    con = _con()
    rows = con.execute(
        "SELECT source_name, data_type, url, last_checked, last_success, status, detail, "
        "licence, rows FROM data_source_status ORDER BY source_name").fetchall()
    con.close()
    return [dict(r) for r in rows]
