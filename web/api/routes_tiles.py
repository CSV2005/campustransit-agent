"""OpenStreetMap raster tile proxy.

Why a proxy exists at all: the app's preview iframe has no network access, so a
``tile.openstreetmap.org`` URL in the browser simply fails and the map would fall back to
the SVG renderer. The backend *can* reach the tiles, so it fetches them and serves them
from our own origin. The browser then never needs to talk to openstreetmap.org.

Obligations that come with this, stated rather than assumed:
  * OSM's tile usage policy requires a valid HTTP User-Agent identifying the application,
    a Referer, and no bulk downloading. We send a descriptive UA and cache tiles for a
    week, which keeps repeat traffic to a minimum.
  * Attribution "© OpenStreetMap contributors" is rendered on the map itself and cannot
    be switched off.
  * This is fine for a single-user demo. It is not a substitute for a tile CDN, and the
    README says so.

If the network is unavailable, the endpoint returns 503 with a JSON body so the frontend
can fall back to the SVG renderer instead of showing broken image tiles.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Response

from src.services.providers import env

router = APIRouter(prefix="/api/tiles", tags=["map"])

TILE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache" / "tiles"
TILE_DIR.mkdir(parents=True, exist_ok=True)
UPSTREAM = env("OSM_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
USER_AGENT = env("OSM_USER_AGENT",
                 "CampusTransit/1.0 (Bengaluru transit assistant; educational project)")
MAX_TILES = 4000          # ~a few MB at typical sizes; oldest are evicted past this
TTL_S = 7 * 24 * 3600
_lock = threading.Lock()
_stats = {"served": 0, "upstream": 0, "errors": 0, "cached_bytes": 0}


def _path(z: int, x: int, y: int) -> Path:
    return TILE_DIR / str(z) / str(x) / f"{y}.png"


def _valid(z: int, x: int, y: int) -> bool:
    if not (0 <= z <= 19):
        return False
    n = 1 << z
    return 0 <= x < n and 0 <= y < n


def _evict_if_needed() -> None:
    files = sorted(TILE_DIR.rglob("*.png"), key=lambda p: p.stat().st_mtime)
    if len(files) > MAX_TILES:
        for f in files[:len(files) - MAX_TILES]:
            try:
                f.unlink()
            except OSError:
                pass


def tile_stats() -> dict:
    files = list(TILE_DIR.rglob("*.png"))
    total = sum(f.stat().st_size for f in files)
    return {**_stats, "tiles_on_disk": len(files), "bytes_on_disk": total,
            "max_tiles": MAX_TILES, "upstream": UPSTREAM,
            "attribution": "© OpenStreetMap contributors"}


@router.get("/{z}/{x}/{y}.png")
def tile(z: int, x: int, y: int) -> Response:
    if not _valid(z, x, y):
        raise HTTPException(400, "tile coordinates out of range")
    p = _path(z, x, y)
    if p.exists():
        age = time.time() - p.stat().st_mtime
        if age < TTL_S:
            with _lock:
                _stats["served"] += 1
            return Response(p.read_bytes(), media_type="image/png",
                            headers={"Cache-Control": "public, max-age=604800",
                                     "X-Tile-Source": "cache"})

    url = UPSTREAM.format(z=z, x=x, y=y)
    try:
        with httpx.Client(timeout=10.0,
                          headers={"User-Agent": USER_AGENT,
                                   "Referer": "http://localhost/"}) as client:
            r = client.get(url)
    except httpx.HTTPError as exc:
        with _lock:
            _stats["errors"] += 1
        raise HTTPException(503, detail={
            "error": "upstream tile fetch failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "fallback": "the client should use the SVG renderer",
        }) from exc

    if r.status_code != 200 or not r.content:
        with _lock:
            _stats["errors"] += 1
        raise HTTPException(503, detail={
            "error": f"upstream returned HTTP {r.status_code}",
            "fallback": "the client should use the SVG renderer"})

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.content)
    with _lock:
        _stats["upstream"] += 1
        _stats["served"] += 1
        _evict_if_needed()
    return Response(r.content, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=604800",
                             "X-Tile-Source": "upstream",
                             "X-Attribution": "© OpenStreetMap contributors"})
