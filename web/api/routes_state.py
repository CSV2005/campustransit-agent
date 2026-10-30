"""User-state endpoints: saved routes, travel history, preferences (v1)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from src.services import store
from web.api.schemas import (HistoryRequest, PreferencesUpdate, SaveRouteRequest,
                             SavedRouteOut)

router = APIRouter(prefix="/api/v1", tags=["user state"])


# ------------------------------------------------------------------ saved routes
@router.get("/saved-routes", response_model=list[SavedRouteOut])
def list_saved(limit: int = Query(default=50, ge=1, le=200)) -> list[dict]:
    return store.list_saved(limit=limit)


@router.post("/saved-routes", response_model=SavedRouteOut, status_code=201)
def create_saved(req: SaveRouteRequest) -> dict:
    return store.save_route(origin=req.origin, destination=req.destination,
                            route_id=req.route_id, title=req.title,
                            payload=req.itinerary)


@router.get("/saved-routes/{save_id}", response_model=SavedRouteOut)
def read_saved(save_id: int) -> dict:
    row = store.get_saved(save_id)
    if not row:
        raise HTTPException(404, f"saved route {save_id} not found")
    return row


@router.delete("/saved-routes/{save_id}")
def delete_saved(save_id: int) -> dict:
    if not store.delete_saved(save_id):
        raise HTTPException(404, f"saved route {save_id} not found")
    return {"deleted": save_id}


# ----------------------------------------------------------------- travel history
@router.get("/history")
def list_history(limit: int = Query(default=50, ge=1, le=200),
                 offset: int = Query(default=0, ge=0)) -> dict:
    return {"entries": store.list_history(limit=limit, offset=offset),
            "stats": store.history_stats()}


@router.post("/history", status_code=201)
def add_history(req: HistoryRequest) -> dict:
    return store.add_history(origin=req.origin, destination=req.destination,
                             departure_time=req.departure_time,
                             selected_mode=req.selected_mode,
                             fare=req.estimated_fare_inr,
                             duration=req.estimated_duration,
                             route_id=req.route_id)


@router.delete("/history")
def clear_history() -> dict:
    return {"deleted": store.clear_history()}


@router.get("/history/stats")
def history_stats() -> dict:
    return store.history_stats()


# ------------------------------------------------------------------- preferences
@router.get("/preferences")
def get_preferences() -> dict:
    return store.get_prefs()


@router.put("/preferences")
def put_preferences(req: PreferencesUpdate) -> dict:
    values = {k: v for k, v in req.model_dump(exclude_none=True).items()}
    if not values:
        raise HTTPException(422, "no recognised preference fields supplied")
    for k, v in values.items():
        if isinstance(v, bool):
            values[k] = "true" if v else "false"
    return store.set_prefs(values)
