"""Live vehicle positions (GTFS-Realtime).

    GET /api/transit/vehicle-positions?limit=&route=&bbox=
    GET /api/transit/trip-updates?limit=

Status codes are deliberate:

* **200** with ``available: false`` when no feed is configured. Nothing failed -- there is
  simply no authorised source, and the body says which variable would fix it.
* **502** when a feed *is* configured but the fetch or decode failed. That is an upstream
  problem and should be visible as one, not disguised as "no data".
* **422** for a malformed ``bbox``.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from src.services import gtfs_rt
from src.services.redact import redact_obj

router = APIRouter(prefix="/api/transit", tags=["live"])


def _parse_bbox(raw: str) -> tuple[float, float, float, float] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise HTTPException(422, "bbox must be south,west,north,east")
    try:
        south, west, north, east = (float(p) for p in parts)
    except ValueError:
        raise HTTPException(422, "bbox values must be numbers") from None
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180):
        raise HTTPException(422, "bbox out of range or inverted")
    return south, west, north, east


@router.get("/vehicle-positions")
def vehicle_positions(limit: int = Query(default=800, ge=1, le=5000),
                      route: str = Query(default="", max_length=64),
                      bbox: str = Query(default="", max_length=120)):
    out = gtfs_rt.positions(limit=limit, route=route, bbox=_parse_bbox(bbox))
    body = redact_obj(out)
    if out.get("available"):
        return JSONResponse(body, headers={"Cache-Control": "no-store"})
    if out.get("kind") == "not_configured":
        # Not an error: there is no source, and saying so is the correct answer.
        return JSONResponse(body, status_code=200)
    return JSONResponse(body, status_code=502)


@router.get("/trip-updates")
def trip_updates(limit: int = Query(default=200, ge=1, le=2000)):
    out = gtfs_rt.trip_updates(limit=limit)
    body = redact_obj(out)
    if out.get("available") or out.get("kind") == "not_configured":
        return JSONResponse(body, status_code=200)
    return JSONResponse(body, status_code=502)


@router.get("/realtime-status")
def realtime_status() -> dict:
    """One cheap call for the UI: is a live feed configured, and is it readable?"""
    return redact_obj(gtfs_rt.status())
