"""Embed the corpus into a persistent Chroma collection.

Default backend is Chroma's bundled all-MiniLM-L6-v2 (ONNX, no PyTorch). Set
EMBED_BACKEND=sentence-transformers to use BAAI/bge-small-en-v1.5 instead, which is
stronger on short factual text but needs torch.

Run:  python -m src.build_index
"""
from __future__ import annotations

import json
import sys
import time

import chromadb
from chromadb.utils import embedding_functions

from src.config import (CHROMA_DIR, COLLECTION, CORPUS_PATH, EMBED_BACKEND, EMBED_MODEL,
                        PROCESSED)


def get_embedding_function():
    if EMBED_BACKEND == "sentence-transformers":
        print(f"  embedder: sentence-transformers / {EMBED_MODEL}")
        return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
    print("  embedder: chromadb default (all-MiniLM-L6-v2, ONNX)")
    return embedding_functions.DefaultEmbeddingFunction()


def main() -> None:
    if not CORPUS_PATH.exists():
        raise SystemExit("corpus.jsonl missing - run `python -m src.build_corpus` first")
    docs = [json.loads(line) for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()]
    print(f"Embedding {len(docs):,} documents ...")

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    try:
        client.delete_collection(COLLECTION)
        print("  removed existing collection")
    except Exception:
        pass

    ef = get_embedding_function()
    col = client.create_collection(
        name=COLLECTION,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine", "embedding_backend": EMBED_BACKEND},
    )

    t0 = time.time()
    batch = 128
    for i in range(0, len(docs), batch):
        chunk = docs[i:i + batch]
        col.add(
            ids=[d["id"] for d in chunk],
            documents=[d["text"] for d in chunk],
            metadatas=[d["metadata"] for d in chunk],
        )
        print(f"  indexed {min(i + batch, len(docs)):,}/{len(docs):,} "
              f"({time.time() - t0:.0f}s)", flush=True)

    meta = {"collection": COLLECTION, "count": col.count(),
            "embedding_backend": EMBED_BACKEND,
            "embedding_model": EMBED_MODEL if EMBED_BACKEND == "sentence-transformers"
            else "all-MiniLM-L6-v2",
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (PROCESSED / "index_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"\nCollection count: {col.count():,}  ({time.time() - t0:.0f}s)")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    sys.exit(main())
