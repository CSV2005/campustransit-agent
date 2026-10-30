"""Local semantic and full-text search across curated/raw/processed data.

Policy-style text uses Chroma embeddings; readable source files use a separate Chroma
filter, and transit database rows use SQLite FTS5. Dedicated route/fare tools remain
the preferred source for exact journey answers.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from src.config import CHROMA_DIR, COLLECTION, CORPUS_PATH, EMBED_BACKEND, EMBED_MODEL, PROCESSED
from src.build_data_index import SEARCH_DB_PATH

_EF = None
_COL = None


def _embedding_function():
    global _EF
    if _EF is not None:
        return _EF
    from chromadb.utils import embedding_functions
    meta_path = PROCESSED / "index_meta.json"
    backend = EMBED_BACKEND
    if meta_path.exists():
        backend = json.loads(meta_path.read_text()).get("embedding_backend", backend)
    if backend == "sentence-transformers":
        _EF = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    else:
        _EF = embedding_functions.DefaultEmbeddingFunction()
    return _EF


def _collection():
    global _COL
    if _COL is not None:
        return _COL
    import chromadb
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    _COL = client.get_collection(COLLECTION, embedding_function=_embedding_function())
    return _COL


def _fallback_search(query: str, k: int, doc_type: str) -> list[dict]:
    """Keyword fallback so the agent still answers if the vector index is missing."""
    from rapidfuzz import fuzz
    if not CORPUS_PATH.exists():
        return []
    docs = [json.loads(line) for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()]
    if doc_type != "any":
        docs = [d for d in docs if d["metadata"].get("doc_type") == doc_type]
    scored = sorted(((fuzz.token_set_ratio(query.lower(), d["text"].lower()), d) for d in docs),
                    key=lambda x: -x[0])[:k]
    return [{"text": d["text"], "metadata": d["metadata"], "score": round(s / 100, 3)}
            for s, d in scored]


STOP = {"the","a","an","is","it","do","i","to","of","for","in","on","and","or","what",
        "how","much","my","me","can","does","are","was","be","with","from","at","by",
        "student","students"}


def _tokens(text: str) -> set[str]:
    import re as _re
    return {t for t in _re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in STOP}


def _lexical_coverage(query: str, text: str) -> float:
    q = _tokens(query)
    if not q:
        return 0.0
    return len(q & _tokens(text)) / len(q)


def search_policy(query: str, doc_type: str = "any", k: int = 4) -> dict[str, Any]:
    """Search pass rules, eligibility, documents required, application steps and FAQs.

    doc_type: 'pass' (one curated pass per document), 'policy' (long-form rules and
    guides), 'route' (route descriptions), 'campus' or 'any'. Use 'pass' or 'policy'
    for almost every question about eligibility, documents, Shakti, Seva Sindhu or
    whether a pass is worth it.
    """
    # Route cards are indexed (so "tell me about route 213" can retrieve them) but are
    # excluded from the default policy search: route questions belong to find_routes,
    # where the answer comes from the graph rather than from a paragraph.
    if doc_type == "any":
        where = {"doc_type": {"$in": ["pass", "policy", "campus"]}}
    else:
        where = {"doc_type": doc_type}
    backend = EMBED_BACKEND
    meta_path = PROCESSED / "index_meta.json"
    if meta_path.exists():
        backend = json.loads(meta_path.read_text()).get("embedding_backend", backend)
    q = query
    if backend == "sentence-transformers":
        q = f"Represent this sentence for searching relevant passages: {query}"

    try:
        # Hybrid: pull a wider candidate set from the vector index, then blend the
        # cosine score with lexical coverage so exact-term questions ("Vajra student
        # monthly pass") surface the table-to-text pass card, not just long prose.
        res = _collection().query(query_texts=[q], n_results=max(k * 3, 12), where=where)
        cand = []
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            cos = 1 - float(dist)
            lex = _lexical_coverage(query, doc)
            head = meta.get("heading") or ""
            lex = max(lex, 0.6 * _lexical_coverage(query, head))
            cand.append((0.78 * cos + 0.22 * lex, cos, lex, doc, meta))
        cand.sort(key=lambda x: -x[0])
        results = [{"text": doc, "metadata": meta, "score": round(blend, 4),
                    "cosine": round(cos, 4), "lexical": round(lex, 3)}
                   for blend, cos, lex, doc, meta in cand[:k]]
        engine = "chroma+lexical"
    except Exception as exc:  # index missing or model unavailable
        results = _fallback_search(query, k, doc_type)
        engine = f"keyword-fallback ({type(exc).__name__})"

    if not results:
        return {"ok": False, "tool": "search_policy", "query": query,
                "summary": "No matching policy documents found.",
                "data": {"results": []}, "sources": []}

    summary_lines = []
    for i, r in enumerate(results, 1):
        src = (r["metadata"].get("source_url") or r["metadata"].get("source_file")
               or r["metadata"].get("pass_id") or "")
        summary_lines.append(f"[{i}] {r['text'][:400]} (source: {src})")
    return {
        "ok": True,
        "tool": "search_policy",
        "query": query,
        "summary": "\n\n".join(summary_lines),
        "data": {"results": results, "engine": engine, "k": k, "doc_type": doc_type},
        "sources": sorted({(r["metadata"].get("source_url") or r["metadata"].get("source_file") or "")
                           for r in results} - {""}),
    }


def _search_database(query: str, k: int) -> list[dict[str, Any]]:
    if not SEARCH_DB_PATH.exists():
        return []
    terms = list(dict.fromkeys(re.findall(r"[A-Za-z0-9]{2,}", query.lower())))[:12]
    if not terms:
        return []
    match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
    con = sqlite3.connect(f"{SEARCH_DB_PATH.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT source, content, bm25(records) AS rank FROM records "
            "WHERE records MATCH ? ORDER BY rank LIMIT ?",
            (match, k),
        ).fetchall()
    finally:
        con.close()
    return [{"text": row[1][:900], "source": row[0],
             "score": round(1 / (1 + abs(float(row[2]))), 4)} for row in rows]


def search_dataset(query: str, k: int = 5) -> dict[str, Any]:
    """Search raw/curated source text semantically and processed transit records lexically."""
    query = query.strip()[:500]
    if not query:
        return {"ok": False, "tool": "search_dataset", "query": query,
                "summary": "Enter a search phrase.", "data": {"results": []}, "sources": []}
    k = max(1, min(int(k), 10))

    try:
        res = _collection().query(
            query_texts=[query],
            n_results=k,
            where={"doc_type": "source_file"},
        )
        file_results = [
            {"text": text, "source": metadata.get("source_file", ""),
             "score": round(1 - float(distance), 4)}
            for text, metadata, distance in zip(
                res["documents"][0], res["metadatas"][0], res["distances"][0])
        ]
        file_engine = "chroma"
    except Exception as exc:
        file_results = _fallback_search(query, k, "source_file")
        file_engine = f"keyword-fallback ({type(exc).__name__})"

    db_results = _search_database(query, k)
    results = file_results + db_results
    if not results:
        return {
            "ok": False,
            "tool": "search_dataset",
            "query": query,
            "summary": "No matching records found. Build the corpus and dataset index first.",
            "data": {"results": [], "file_engine": file_engine,
                     "database_index_ready": SEARCH_DB_PATH.exists()},
            "sources": [],
        }

    return {
        "ok": True,
        "tool": "search_dataset",
        "query": query,
        "summary": "\n\n".join(
            f"[{i}] {result['text'][:600]} (source: {result['source']})"
            for i, result in enumerate(results[:k * 2], 1)),
        "data": {"results": results[:k * 2], "file_engine": file_engine,
                 "database_index_ready": SEARCH_DB_PATH.exists()},
        "sources": sorted({r["source"] for r in results if r.get("source")}),
    }
