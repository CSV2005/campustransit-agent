"""Central configuration for CampusTransit Agent.

All paths are absolute and derived from this file so the project works no matter
which directory you launch a script from.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- data paths
RAW = ROOT / "data" / "raw"
RAW_GTFS_BMTC = RAW / "gtfs" / "bmtc"        # D1
RAW_GTFS_BMRCL = RAW / "gtfs" / "bmrcl"      # D2
RAW_OSM = RAW / "osm"                        # D3
RAW_KSRTC = RAW / "ksrtc"                    # D4

CURATED = ROOT / "data" / "curated"
POLICY_DOCS = CURATED / "policy_docs"
PROCESSED = ROOT / "data" / "processed"

DB_PATH = PROCESSED / "transit.db"
CHROMA_DIR = PROCESSED / "chroma"
CORPUS_PATH = PROCESSED / "corpus.jsonl"
MANIFEST_PATH = RAW / "dataset_manifest.json"

# ---------------------------------------------------------------- models
# Embeddings: "chroma-default" uses Chroma's bundled all-MiniLM-L6-v2 (ONNX, no
# torch). Set EMBED_BACKEND="sentence-transformers" to use BAAI/bge-small-en-v1.5
# (better on short factual text, needs `pip install sentence-transformers torch`).
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "chroma-default")
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
COLLECTION = "campustransit"

# LLM (Ollama). The agent falls back to the deterministic planner when Ollama
# is not reachable, so the demo works on any machine.
# OLLAMA_MODEL is accepted as an alias because that is the name Ollama's own docs and
# most tutorials use; LLM_MODEL wins if both are set. qwen2.5:0.5b is the default for
# low-memory machines; route facts remain in the deterministic tools and local indexes.
LLM_MODEL = os.getenv("OLLAMA_MODEL") or os.getenv("LLM_MODEL", "qwen2.5:0.5b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_ENABLED = os.getenv("LLM_ENABLED", "auto")  # auto | on | off
# A local model on a CPU is slow the first time (model load) and merely slow afterwards.
# 120 s covers a cold 3B load on a laptop; the deterministic fallback is always there.
OLLAMA_TIMEOUT_S = float(os.getenv("OLLAMA_TIMEOUT_S", "120"))

# Wall-clock budget for one chat turn. A local model on a CPU can take 30-90 s; without a
# server-side deadline the only limit is MAX_TOOL_ROUNDS x OLLAMA_TIMEOUT_S, and the browser
# gives up long before the server does. The turn stops here and the rules engine answers.
CHAT_BUDGET_S = float(os.getenv("CHAT_BUDGET_S", "150"))

# development | test | production. Only affects diagnostics and how loudly bad config is
# reported; nothing in the app changes behaviour on it.
APP_ENV = (os.getenv("APP_ENV") or "development").strip().lower()

# ------------------------------------------------------------------ domain
# BMTC service classes used by the pass engine. The GTFS route_type for BMTC is
# 3 (bus); AC/Vajra identification is done from route_short_name prefixes.
AC_ROUTE_HINTS = ("V", "KIA", "KIAS", "G-", "VJ", "VAJRA", "A/C", "AC")

SERVICE_CLASS_LABELS = {
    "ordinary": "BMTC ordinary (non-AC)",
    "ac_vajra": "BMTC Vajra / AC",
    "metro_feeder": "BMTC metro feeder",
    "ksrtc_ordinary": "KSRTC ordinary",
    "metro": "Namma Metro",
}

PROCESSED.mkdir(parents=True, exist_ok=True)
