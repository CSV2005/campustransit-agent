"""Build the retrieval corpus from curated documents and searchable source files.

This is the table-to-text step from the build guide: structured rows are rendered as
short natural-language cards so they embed cleanly against conversational questions.
Policy, route cards, and readable raw/curated/processed source files are chunked for
semantic retrieval. The full processed transit database is indexed separately in SQLite
FTS so exact operational data does not have to be embedded as prose.

Run:  python -m src.build_corpus
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import zipfile
from io import BytesIO
from pathlib import Path

from src.config import CORPUS_PATH, CURATED, DB_PATH, POLICY_DOCS, PROCESSED, RAW

CHUNK_SIZE = 700
CHUNK_OVERLAP = 120
ROUTE_CARD_LIMIT = 600
SOURCE_CHUNK_CHARS = 1600
SEARCHABLE_SUFFIXES = {".csv", ".geojson", ".json", ".jsonl", ".md", ".ql", ".svg",
                       ".txt", ".xml", ".yaml", ".yml"}
GENERATED_PROCESSED_FILES = {
    "corpus.jsonl", "eval_report.md", "eval_results.json", "index_meta.json",
}


def pass_cards() -> list[dict]:
    passes = json.loads((CURATED / "passes.json").read_text(encoding="utf-8"))
    docs = []
    for p in passes:
        price = ("no cost (Rs 0)" if p.get("price_inr") == 0
                 else f"Rs {p['price_inr']:,.0f}" if p.get("price_inr") is not None
                 else "price not published")
        text = (
            f"PASS: {p['name']} (operator: {p['operator']}).\n"
            f"Cost: {price}. {p.get('price_note','')}\n"
            f"Validity: {p.get('validity_note') or p.get('validity_days') or 'see official source'}.\n"
            f"Covers: {', '.join(p['service_classes'])}. Unlimited rides: "
            f"{'yes' if p.get('unlimited_rides') else 'no'}.\n"
            f"Coverage area: {p['geographic_scope']}.\n"
            f"Who is eligible: {p['eligibility']}\n"
            f"Documents required: {'; '.join(p['documents_required'])}.\n"
            f"How and where to apply: {p['where_to_apply']} ({p['application_url']}).\n"
            f"Notes: {p.get('notes','')}\n"
            f"Last verified: {p.get('last_verified')} | Status: {p.get('verification_status')}"
        )
        docs.append({
            "id": f"pass::{p['pass_id']}",
            "text": text,
            "metadata": {"doc_type": "pass", "operator": p["operator"],
                         "pass_id": p["pass_id"],
                         "source_url": p.get("source_url", ""),
                         "last_verified": str(p.get("last_verified")),
                         "verification_status": p.get("verification_status", "")},
        })
    return docs


def _paragraph_blocks(sec: str) -> list[str]:
    """Split a section into paragraph blocks that never break a table.

    Consecutive lines with no blank line between them stay together, which keeps a
    markdown table (and its header) in one piece instead of slicing it mid-cell.
    """
    blocks, cur = [], []
    for line in sec.splitlines():
        if not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return blocks


def markdown_chunks() -> list[dict]:
    """Header-aware chunking: split on headings, then pack whole paragraphs/tables."""
    docs = []
    for f in sorted(POLICY_DOCS.glob("*.md")):
        raw = f.read_text(encoding="utf-8")
        sections = re.split(r"\n(?=#{1,3}\s)", raw)
        for si, sec in enumerate(sections):
            sec = sec.strip()
            if not sec:
                continue
            heading = sec.split("\n", 1)[0].lstrip("# ").strip()[:90]
            blocks = _paragraph_blocks(sec)
            if blocks and blocks[0].lstrip().startswith("#"):
                # Strip only the heading LINE. The body usually sits on the very next
                # line with no blank line between, so dropping the whole block would
                # silently delete the section's content.
                lines = blocks[0].splitlines()
                rest = "\n".join(lines[1:]).strip()
                if rest:
                    blocks[0] = rest
                else:
                    blocks = blocks[1:]
            pieces, cur, size = [], [], 0
            for b in blocks:
                if cur and size + len(b) > CHUNK_SIZE:
                    pieces.append("\n\n".join(cur))
                    cur, size = [], 0
                cur.append(b)
                size += len(b) + 2
            if cur:
                pieces.append("\n\n".join(cur))
            for pi, piece in enumerate(pieces):
                # Prefix every chunk with its section heading: the embedding then
                # carries the topic even for chunks deep inside a long section.
                text = f"# {heading}\n\n{piece}" if heading else piece
                docs.append({
                    "id": f"doc::{f.stem}::{si}::{pi}",
                    "text": text,
                    "metadata": {"doc_type": "policy", "source_file": f.name,
                                 "heading": heading,
                                 "source_url": "data/curated/policy_docs/" + f.name},
                })
    return docs


def faq_cards() -> list[dict]:
    """One document per question - FAQ entries are already atomic."""
    path = CURATED / "faq.md"
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    docs = []
    for i, block in enumerate(re.split(r"\n(?=###\s)", raw)):
        block = block.strip()
        if not block.startswith("###"):
            continue
        title = block.split("\n", 1)[0].lstrip("# ").strip()
        docs.append({
            "id": f"faq::{i}",
            "text": block,
            "metadata": {"doc_type": "policy", "source_file": "faq.md",
                         "heading": title, "source_url": "data/curated/faq.md"},
        })
    return docs


def route_cards(limit: int = ROUTE_CARD_LIMIT) -> list[dict]:
    """One card per busy route, rendered as a sentence with head and tail stops.

    Built in Python rather than with GROUP_CONCAT so the stop order is guaranteed to
    follow the sequence rather than SQLite's aggregation order. Only the busiest
    `limit` routes are carded; the rest are still fully queryable in SQLite.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    # busiest routes, and for each the direction with the most stops (a route's
    # two directions would otherwise be interleaved into one nonsense path)
    best = con.execute("""
        WITH dir_sizes AS (
            SELECT route_id, direction_id, COUNT(*) AS n
            FROM route_stops GROUP BY route_id, direction_id
        ), ranked AS (
            SELECT route_id, direction_id,
                   ROW_NUMBER() OVER (PARTITION BY route_id ORDER BY n DESC) AS rn
            FROM dir_sizes
        )
        SELECT r.route_id, r.route_short_name, r.route_long_name, ranked.direction_id,
               (SELECT n_trips FROM route_stats st WHERE st.route_id = r.route_id) AS n_trips
        FROM routes r
        JOIN ranked ON ranked.route_id = r.route_id AND ranked.rn = 1
        ORDER BY n_trips DESC
        LIMIT ?
    """, (limit,)).fetchall()

    docs = []
    for row in best:
        stops = [r["stop_name"] for r in con.execute("""
            SELECT s.stop_name FROM route_stops rs JOIN stops s ON s.stop_id = rs.stop_id
            WHERE rs.route_id = ? AND rs.direction_id = ? ORDER BY rs.seq
        """, (row["route_id"], row["direction_id"]))]
        if not stops:
            continue
        text = (
            f"BUS ROUTE {row['route_short_name']}: {row['route_long_name']}.\n"
            f"Operator: BMTC. Serves about {len(stops)} stops, {row['n_trips']} trips a day.\n"
            f"Origin side: {' -> '.join(stops[:7])}.\n"
            f"Destination side: {' -> '.join(stops[-5:])}.\n"
            f"Route runs {stops[0]} to {stops[-1]}."
        )
        docs.append({
            "id": f"route::{row['route_id']}",
            "text": text,
            "metadata": {"doc_type": "route", "route_id": row["route_id"],
                         "route_short_name": row["route_short_name"] or "",
                         "operator": "BMTC", "n_trips": int(row["n_trips"] or 0)},
        })
    con.close()
    return docs


def campus_cards() -> list[dict]:
    path = CURATED / "places.json"
    if not path.exists():
        return []
    docs = []
    for p in json.loads(path.read_text(encoding="utf-8")):
        text = (
            f"CAMPUS: {p['name']}. Also known as: {', '.join(p.get('aliases', []))}.\n"
            f"Location: {p.get('lat')}, {p.get('lon')} in Bengaluru.\n"
            f"{p.get('note','')}\n"
            f"Nearest transit: {p.get('nearest_stop_hint','see route tools')}."
        )
        docs.append({
            "id": f"campus::{p['id']}",
            "text": text,
            "metadata": {"doc_type": "campus", "campus_id": p["id"],
                         "source_url": p.get("source", "")},
        })
    return docs


def _chunks(text: str, limit: int = SOURCE_CHUNK_CHARS) -> list[str]:
    """Split large source text without needlessly breaking line-oriented records."""
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if len(line) > limit:
            if current:
                pieces.append("\n".join(current))
                current, size = [], 0
            pieces.extend(line[i:i + limit] for i in range(0, len(line), limit))
            continue
        if current and size + len(line) + 1 > limit:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pieces.append("\n".join(current))
    return [p for p in pieces if p.strip()]


def _pdf_text(data: bytes, label: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF indexing needs pypdf; install requirements.txt") from exc
    reader = PdfReader(BytesIO(data))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _source_text(data: bytes, label: str) -> str:
    if Path(label).suffix.lower() == ".pdf":
        return _pdf_text(data, label)
    return data.decode("utf-8-sig", errors="replace")


def source_file_cards() -> list[dict]:
    """Index readable raw, curated and useful processed source files.

    Chroma's own files, generated reports/corpus, and the SQLite transit database are
    handled separately rather than recursively embedding implementation artefacts.
    """
    files = []
    seen_paths: set[Path] = set()
    for root in (RAW, CURATED, PROCESSED):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path in seen_paths:
                continue
            seen_paths.add(path)
            rel = path.relative_to(root.parent).as_posix()
            if root == PROCESSED:
                if "chroma" in path.relative_to(PROCESSED).parts:
                    continue
                if path.name in GENERATED_PROCESSED_FILES or path.suffix.lower() == ".db":
                    continue
            suffix = path.suffix.lower()
            if suffix in SEARCHABLE_SUFFIXES or suffix in (".pdf", ".zip", ".png"):
                files.append((path, rel))

    docs: list[dict] = []
    seen_chunks: set[str] = set()

    def add_text(label: str, text: str) -> None:
        for i, piece in enumerate(_chunks(text)):
            digest = hashlib.sha256(piece.encode("utf-8")).hexdigest()
            if digest in seen_chunks:
                continue
            seen_chunks.add(digest)
            docs.append({
                "id": f"source::{hashlib.sha256(label.encode('utf-8')).hexdigest()[:16]}::{i}",
                "text": f"SOURCE FILE: {label}\n\n{piece}",
                "metadata": {"doc_type": "source_file", "source_file": label,
                             "heading": label, "chunk": i},
            })

    for path, rel in files:
        suffix = path.suffix.lower()
        if suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                for member in sorted(archive.infolist(), key=lambda item: item.filename):
                    member_suffix = Path(member.filename).suffix.lower()
                    if member.is_dir() or member_suffix not in SEARCHABLE_SUFFIXES | {".pdf"}:
                        continue
                    label = f"{rel}!{member.filename}"
                    add_text(label, _source_text(archive.read(member), label))
        elif suffix == ".png":
            add_text(rel, f"Raster image file: {rel}. A companion SVG, when present, "
                          "contains the searchable map labels and geometry.")
        elif suffix == ".pdf":
            add_text(rel, _source_text(path.read_bytes(), rel))
        else:
            add_text(rel, _source_text(path.read_bytes(), rel))
    return docs


def main() -> None:
    docs = (pass_cards() + markdown_chunks() + faq_cards() + route_cards()
            + campus_cards() + source_file_cards())
    seen = set()
    unique = []
    for d in docs:
        if d["id"] in seen:
            continue
        seen.add(d["id"])
        unique.append(d)
    with CORPUS_PATH.open("w", encoding="utf-8") as fh:
        for d in unique:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    by_type: dict[str, int] = {}
    for d in unique:
        by_type[d["metadata"]["doc_type"]] = by_type.get(d["metadata"]["doc_type"], 0) + 1
    print(f"Wrote {len(unique):,} documents -> {CORPUS_PATH}")
    print("  " + " | ".join(f"{k}: {v:,}" for k, v in sorted(by_type.items())))


if __name__ == "__main__":
    sys.exit(main())
