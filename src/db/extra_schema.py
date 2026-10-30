"""Additional tables for the application layer (saved routes, history, source status).

Deliberately additive: every existing table from the GTFS import is untouched. These
four tables hold *user* state and *operational* state, which is why they are not part of
the ingest pipeline and survive a rebuild of the GTFS data.

Run:  python -m src.db.extra_schema
"""
from __future__ import annotations

import sqlite3
import time

from src.config import DB_PATH

DDL = """
CREATE TABLE IF NOT EXISTS saved_routes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT    NOT NULL DEFAULT 'local',
    origin          TEXT    NOT NULL,
    destination     TEXT    NOT NULL,
    route_id        TEXT,
    title           TEXT,
    payload_json    TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS travel_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT    NOT NULL DEFAULT 'local',
    origin              TEXT    NOT NULL,
    destination         TEXT    NOT NULL,
    departure_time      TEXT,
    selected_mode       TEXT,
    estimated_fare_inr  REAL,
    estimated_duration  INTEGER,
    route_id            TEXT,
    created_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS data_source_status (
    source_name     TEXT PRIMARY KEY,
    data_type       TEXT,
    url             TEXT,
    last_checked    TEXT,
    last_success    TEXT,
    status          TEXT NOT NULL,
    detail          TEXT,
    licence         TEXT,
    rows            INTEGER
);

CREATE TABLE IF NOT EXISTS api_call_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    status_code INTEGER,
    latency_ms  INTEGER,
    detail      TEXT,
    called_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_hist_user ON travel_history(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_saved_user ON saved_routes(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_apilog_provider ON api_call_log(provider, called_at DESC);

CREATE TABLE IF NOT EXISTS preferences (
    user_id     TEXT NOT NULL DEFAULT 'local',
    key         TEXT NOT NULL,
    value       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""


def migrate() -> None:
    con = sqlite3.connect(DB_PATH)
    con.executescript(DDL)
    con.commit()
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    con.close()
    print(f"schema ok — {len(tables)} tables")
    for t in ("saved_routes", "travel_history", "data_source_status",
              "api_call_log", "preferences"):
        print(f"  {t}")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    migrate()
