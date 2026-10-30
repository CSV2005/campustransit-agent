"""Planning, places and traffic endpoints (v1)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from src.routing.search import OBJECTIVE_LABEL, PROFILES
from src.services import places, traffic
from src.services.planner import ALL_OBJECTIVES, plan
from web.api.schemas import AutocompleteResponse, PlanRequest, PlanResponse

router = APIRouter(prefix="/api/v1", tags=["planning"])


@router.post("/routes/plan", response_model=PlanResponse)
def routes_plan(req: PlanRequest) -> dict:
    """Plan a journey and return every viable itinerary with fares and ranking.

    A 200 means the planner ran. ``ok: false`` in the body means it ran and found nothing
    — that is different from a 4xx/5xx, which means the request itself was wrong.
    """
    result = plan(req.origin, req.destination, arrival_by=req.arrival_by,
                  preference=req.preference, max_itineraries=req.max_itineraries)
    return result


@router.get("/routes/objectives")
def objectives() -> dict:
    return {
        "objectives": [
            {"key": k, "label": OBJECTIVE_LABEL[k],
             "weighting": {"minutes": PROFILES[k]["minutes"], "inr": PROFILES[k]["inr"],
                           "transfers": PROFILES[k]["transfers"],
                           "walk": PROFILES[k].get("walk", 0.0)}}
            for k in ALL_OBJECTIVES],
        "note": ("Weights are the search cost function, shown so the ranking is auditable. "
                 "Fares themselves always come from the published charts, never from these "
                 "weights."),
    }


@router.get("/routes/compare")
def compare(origin: str = Query(min_length=2, max_length=120),
            destination: str = Query(min_length=2, max_length=120)) -> dict:
    """Run A*, Dijkstra and RRT on the same pair and report what each actually did.

    Included because the brief asked for all three algorithms in the routing path and a
    comparison is the only honest way to show RRT's limits.
    """
    from src.tools.plan_tool import compare_routing_algorithms
    try:
        return compare_routing_algorithms(origin, destination)
    except Exception as exc:                                  # noqa: BLE001
        raise HTTPException(500, f"comparison failed: {type(exc).__name__}: {exc}") from exc


@router.get("/places/autocomplete", response_model=AutocompleteResponse)
def autocomplete(q: str = Query(default="", max_length=120),
                 limit: int = Query(default=8, ge=1, le=20),
                 include_google: bool = True) -> dict:
    return places.search(q, limit=limit, include_google=include_google)


@router.get("/places/resolve")
def resolve(q: str = Query(min_length=1, max_length=120)) -> dict:
    r = places.geocode(q)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "place not found"))
    return r


@router.get("/places/kinds")
def place_kinds() -> dict:
    return {"kinds": places.kinds_summary(),
            "indexed_places": places.index_size(),
            "index_source": "places table (BMTC + BMRCL GTFS, OSM extract)"}


@router.get("/traffic")
def traffic_panel(origin: str = Query(default="", max_length=120),
                  destination: str = Query(default="", max_length=120)) -> dict:
    """Live traffic (Google, when configured) plus scheduled network supply (GTFS).

    Each block carries its own ``available`` flag and ``reason``. An unavailable block is
    never replaced by an estimate.
    """
    return traffic.panel(origin, destination)
