#!/usr/bin/env python3
"""One-command launcher for the CampusTransit web app.

    python run.py                  # http://localhost:7860
    python run.py --port 8080      # or any free port
    python run.py --open           # also open a browser tab

Serves a single-page app on `/` and a JSON API on `/api/*`. No Node, no build step,
no CDN — the whole UI is one HTML file with inline CSS/JS, so it works offline and in
any modern browser (Chrome, Safari, Firefox, Edge, and on phones over the LAN).

The app binds to 0.0.0.0 by default, so other devices on your network can reach it at
the LAN address printed on startup.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# .env before uvicorn imports anything that reads the environment.
try:                                        # pragma: no cover
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:                         # pragma: no cover
    pass


def _lan_ip() -> str | None:
    """Best-effort local network address (never sends a packet)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def preflight() -> list[str]:
    """Check the artefacts the app needs and return a list of problems."""
    problems = []
    from src.config import CHROMA_DIR, DB_PATH, PROCESSED

    if not DB_PATH.exists() or DB_PATH.stat().st_size < 1024 * 1024:
        problems.append(
            f"transit database missing or empty: {DB_PATH}\n"
            "    rebuild it with:\n"
            "      python -m src.ingest_gtfs\n"
            "      python -m src.ingest_places\n"
            "      python -m src.ingest_ksrtc\n"
            "      python -m src.ingest_metro_pink\n"
            "      python -m src.ingest_bmrcl_official\n"
            "      python -m src.ingest_routing_graph")
    if not CHROMA_DIR.exists():
        problems.append(f"vector index missing: {CHROMA_DIR}\n"
                        "    rebuild it with:\n"
                        "      python -m src.build_corpus\n"
                        "      python -m src.build_index")
    if not (PROCESSED / "corpus.jsonl").exists():
        problems.append(f"corpus missing: {PROCESSED / 'corpus.jsonl'}\n"
                        "    rebuild it with: python -m src.build_corpus")
    return problems


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def banner(host: str, port: int) -> None:
    lan = _lan_ip()
    line = "─" * 62
    print(f"\n{line}")
    print("  CampusTransit Agent")
    print(f"{line}")
    print(f"  Local     http://localhost:{port}")
    if host in ("0.0.0.0", "::") and lan:
        print(f"  Network   http://{lan}:{port}   (same Wi-Fi, from a phone)")
    print(f"  API       http://localhost:{port}/api/health")
    print(f"  Docs      http://localhost:{port}/docs   (FastAPI, auto-generated)")
    print(f"{line}")
    print("  Ctrl+C to stop\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the CampusTransit web app.")
    ap.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=7860, help="port (default 7860)")
    ap.add_argument("--open", action="store_true", help="open a browser tab when ready")
    ap.add_argument("--reload", action="store_true", help="auto-reload on file changes")
    args = ap.parse_args()

    problems = preflight()
    if problems:
        print("Cannot start — the data layer is not built:\n")
        for p in problems:
            print(f"  • {p}\n")
        return 1

    port = args.port
    if not port_free(args.host, port):
        for candidate in range(port + 1, port + 20):
            if port_free(args.host, candidate):
                print(f"  port {port} is busy, using {candidate}")
                port = candidate
                break
        else:
            print(f"  ports {port}-{port + 19} are all busy; pass --port")
            return 1

    banner(args.host, port)
    if args.open:
        threading.Thread(target=lambda: (time.sleep(1.2),
                                         webbrowser.open(f"http://localhost:{port}")),
                         daemon=True).start()

    import uvicorn
    uvicorn.run("web.server:app", host=args.host, port=port,
                reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
