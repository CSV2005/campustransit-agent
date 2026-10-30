"""KSRTC intercity corridors — quoted, never routed.

The router searches BMTC + Namma Metro, because those are the only operators this repo
has stop-level GTFS for. KSRTC is different: there is no feed, only a handful of
published corridor fares in `ksrtc_corridor`. So the honest thing to do with KSRTC is:

* quote the fare that a source actually published, with that source and its verification
  status attached;
* refuse to quote anything that is not published (`fare_inr IS NULL`);
* never synthesise a stop sequence, a timetable or a polyline for a corridor, and never
  put a KSRTC option into the ranked itinerary list as if it had been routed.

Matching is by the text the user typed against the corridor's own endpoint names, plus the
resolved coordinate, so "Kanakapura Road" inside Bengaluru does not silently become
"Kanakapura" 35 km south of it.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from src.config import DB_PATH
from src.tools.common import haversine_km

# Corridor endpoint text -> the place in the local index that endpoint actually means.
# Kanakapura is the town in the index (12.545, 77.4199); the several "Kanakapura Road"
# stops are roads inside Bengaluru and must not be used as a stand-in for it.
ENDPOINT_PLACE_HINTS: dict[str, str] = {
    "Bengaluru Kempegowda Bus Station (KBS)": "Kempegowda Bus Station",
    "Kanakapura": "Kanakapura",
    "Harohalli (DSU)": "Dayananda Sagar University — Harohalli Campus",
    # Mysuru is outside the Bengaluru place index; it can be quoted, never located.
    "Mysuru": "",
}

# Tokens that identify an endpoint in typed text. Deliberately narrow: "road", "layout"
# and other generic words would make "Kanakapura Road" match the town.
ENDPOINT_TOKENS: dict[str, tuple[str, ...]] = {
    "Bengaluru Kempegowda Bus Station (KBS)": ("kbs", "kempegowda bus station", "majestic"),
    "Kanakapura": ("kanakapura",),
    "Harohalli (DSU)": ("harohalli", "dsu"),
    "Mysuru": ("mysuru", "mysore"),
}

MAX_ENDPOINT_KM = 3.0        # how far a resolved place may sit from the corridor's endpoint


def _corridors() -> list[dict[str, Any]]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM ksrtc_corridor ORDER BY corridor_id").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [dict(r) for r in rows]


def _endpoint_coord(name: str) -> dict[str, float] | None:
    """Exact-name lookup for a corridor endpoint; never a fuzzy one."""
    hint = ENDPOINT_PLACE_HINTS.get(name)
    if not hint:
        return None
    from src.services import places
    target = hint.strip().lower()
    best = None
    for row in places._index():                       # noqa: SLF001 - same package
        if (row.get("name") or "").strip().lower() != target:
            continue
        if row.get("lat") is None or row.get("lon") is None:
            continue
        best = {"lat": row["lat"], "lon": row["lon"], "name": row["name"]}
        break
    return best


# Words that add nothing when matching a place name, plus the suffixes that turn a town
# into a road ("Kanakapura" vs "Kanakapura Road" are 35 km apart and must not be confused).
_NOISE = {"stop", "stops", "bus", "stand", "station", "bs", "kbs", "the", "at", "near"}
_ROAD_WORDS = {"road", "rd", "layout", "cross", "circle", "junction", "gate", "main"}


def _tokens(text: str) -> list[str]:
    clean = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return [w for w in clean.split() if w]


def _normal(text: str) -> str:
    return " ".join(w for w in _tokens(text) if w not in _NOISE)


def _mentions(typed: str, endpoint: str) -> bool:
    """Whole-name match only.

    Substring matching is what turns "Kanakapura Road" (a road in south Bengaluru) into
    "Kanakapura" (a town 35 km further on, on the same road). A typed place has to equal
    the endpoint's name or one of its documented aliases; anything else has to earn its
    match through coordinates.
    """
    norm = _normal(typed)
    if not norm:
        return False
    if norm == _normal(endpoint):
        return True
    return norm in {tok for tok in ENDPOINT_TOKENS.get(endpoint, ())}


def _looks_like_a_road(typed: str) -> bool:
    return any(w in _ROAD_WORDS for w in _tokens(typed))


def _near(coord: dict[str, float] | None, resolved: dict[str, Any], km: float) -> bool:
    if not coord or not resolved.get("ok"):
        return False
    return haversine_km(coord["lat"], coord["lon"],
                        resolved["lat"], resolved["lon"]) <= km


def options_for(origin_text: str, destination_text: str,
                origin: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any]:
    """KSRTC corridors that plausibly serve this pair, priced only where a source exists.

    A corridor is offered when one end matches KBS and the other matches the corridor's
    outer terminal, either by the text the user typed or by the coordinate it resolved to.
    Anything less is left out rather than guessed at.
    """
    corridors = _corridors()
    matched: list[dict[str, Any]] = []

    for c in corridors:
        kbs_place = c["from_place"]
        outer_place = c["to_place"]
        kbs_coord = _endpoint_coord(kbs_place)
        outer_coord = _endpoint_coord(outer_place)

        kbs_o = _mentions(origin_text, kbs_place) or _near(kbs_coord, origin, MAX_ENDPOINT_KM)
        outer_d = (_mentions(destination_text, outer_place)
                   or _near(outer_coord, destination, MAX_ENDPOINT_KM))
        # "from Harohalli to Majestic" is the same corridor in the other direction.
        kbs_d = _mentions(destination_text, kbs_place) or _near(kbs_coord, destination,
                                                                 MAX_ENDPOINT_KM)
        outer_o = (_mentions(origin_text, outer_place)
                   or _near(outer_coord, origin, MAX_ENDPOINT_KM))

        forward = kbs_o and outer_d
        backward = outer_o and kbs_d
        if not (forward or backward):
            continue

        verified = c["verification_status"] in ("VERIFIED_PRIMARY", "VERIFIED_SECONDARY")
        fare = c["fare_inr"]
        if fare is None:
            fare_block = {
                "amount": None, "currency": "INR", "status": "unavailable",
                "source": c["publisher"],
                "note": ("No published fare for this service class was reachable, so no "
                         "amount is shown rather than a guessed one."),
            }
        elif c["verification_status"] == "UNVERIFIED_DERIVED":
            fare_block = {
                "amount": fare, "currency": "INR", "status": "estimated",
                "source": c["publisher"],
                "note": c["notes"] or "Derived from a published fare, not quoted from KSRTC.",
            }
        else:
            fare_block = {
                "amount": fare, "currency": "INR", "status": "verified",
                "source": f"{c['publisher']} ({c['published_date']})",
                "note": c["notes"] or "Published KSRTC fare.",
            }

        warnings = []
        # The text matched but the coordinate did not: either the name is ambiguous in the
        # index or it is a different place with a similar name. Say so instead of silently
        # pricing a ride to somewhere else.
        resolved = origin if outer_o else destination
        typed = origin_text if outer_o else destination_text
        if outer_coord is not None and resolved.get("ok") and not _near(
                outer_coord, resolved, MAX_ENDPOINT_KM) and not _looks_like_a_road(typed):
            warnings.append(
                f"'{typed}' resolved to {resolved.get('formatted', typed)} "
                f"({resolved.get('lat'):.3f}, {resolved.get('lon'):.3f}), which is not the "
                f"{outer_place} on this corridor. The fare below is for {outer_place}; "
                f"confirm the stop before buying a ticket.")
        located = outer_coord is not None
        if not located:
            warnings.append(
                f"{outer_place} is outside the Bengaluru place index, so it can be priced "
                f"but not drawn or routed from here.")
        if not verified and fare is not None:
            warnings.append(
                "The fare for this corridor is derived, not published — confirm it before "
                "relying on the number.")

        matched.append({
            "corridor_id": c["corridor_id"],
            "operator": "KSRTC",
            "service_class": c["service_class"],
            "from_place": kbs_place if forward else outer_place,
            "to_place": outer_place if forward else kbs_place,
            "via": c["via"],
            "distance_km": c["approx_km"],
            "fare": fare_block,
            "verification_status": c["verification_status"],
            "usable_as_fare_quote": bool(verified and fare is not None),
            "source_url": c["source_url"],
            "last_verified": c["last_verified"],
            "routed": False,
            "geometry": None,
            "warnings": warnings,
        })

    if not matched:
        return {
            "available": False,
            "corridors": [],
            "in_routing_graph": False,
            "note": ("No KSRTC corridor in this build serves that pair. KSRTC is quoted "
                     "corridor by corridor from published fares; it is not part of the "
                     "BMTC / Namma Metro route search."),
        }

    return {
        "available": True,
        "corridors": matched,
        "in_routing_graph": False,
        "note": ("KSRTC is quoted, not routed: these are published corridor fares with the "
                 "board and alight points named. There is no KSRTC stop-level GTFS in this "
                 "build, so no timetable, stop sequence or line is synthesised."),
    }
