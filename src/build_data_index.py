"""Build a local full-text index of the processed transit database.

Run after the transit database is built:
    python -m src.build_data_index
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

from src.config import DB_PATH, PROCESSED

SEARCH_DB_PATH = PROCESSED / "dataset_search.sqlite3"
BATCH_SIZE = 2000
EXCLUDED_TABLES = {
    "api_call_log", "data_source_status", "integration_checks", "integration_prefs",
    "preferences", "saved_routes", "travel_history",
}


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_text(table: str, columns: list[str], row: tuple[Any, ...]) -> str:
    fields = []
    for column, value in zip(columns, row):
        if value is None:
            continue
        if isinstance(value, bytes):
            value = f"[binary {len(value)} bytes]"
        fields.append(f"{column}={value}")
    return f"table={table}; " + "; ".join(fields)


def build_index(source_path: Path = DB_PATH,
                output_path: Path = SEARCH_DB_PATH) -> int:
    """Create an atomic FTS5 index for all non-private transit database tables."""
    if not source_path.exists():
        raise FileNotFoundError(f"Transit database does not exist: {source_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(output_path.name + ".tmp")
    if temp_path.exists():
        temp_path.unlink()

    source_uri = source_path.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    destination = sqlite3.connect(temp_path)
    total = 0
    try:
        destination.execute(
            "CREATE VIRTUAL TABLE records USING fts5(source UNINDEXED, content)")
        tables = [
            row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            if row[0] not in EXCLUDED_TABLES and not row[0].startswith("sqlite_")
        ]
        for table in tables:
            quoted_table = _quote_identifier(table)
            columns = [row[1] for row in source.execute(
                f"PRAGMA table_info({quoted_table})")]
            if not columns:
                continue
            cursor = source.execute(f"SELECT * FROM {quoted_table}")
            batch = []
            while rows := cursor.fetchmany(BATCH_SIZE):
                for row in rows:
                    text = _row_text(table, columns, row)
                    if text.strip():
                        batch.append((f"transit.db::{table}", text[:12000]))
                if batch:
                    destination.executemany(
                        "INSERT INTO records(source, content) VALUES (?, ?)", batch)
                    total += len(batch)
                    batch.clear()
            destination.commit()
            print(f"  indexed {table}", flush=True)
    finally:
        destination.close()
        source.close()

    temp_path.replace(output_path)
    return total


def main() -> None:
    count = build_index()
    print(f"Indexed {count:,} transit database records -> {SEARCH_DB_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Dataset index failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
