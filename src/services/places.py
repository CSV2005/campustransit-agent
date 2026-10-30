"""Place search: fast local autocomplete, with Google as an optional amplifier.

The local index is the 13,071 rows of ``places`` (BMTC stops, metro stations, OSM POIs and
campuses) plus the 9,960-row ``stops`` table. It answers in a couple of milliseconds,
works with no network and no key, and is the reason autocomplete keeps working with every
external provider switched off.

When ``GOOGLE_MAPS_SERVER_API_KEY`` is present, ``/api/places/autocomplete`` may additionally
ask Google Places for suggestions. Those results are merged, marked
``source="google_places"`` and carry Google's ``place_id`` so a follow-up Details call can
resolve the exact coordinates. Google never contributes a fare — see ``src/routing/fares.py``.
"""
from __future__ import annotations

import functools
import re
import sqlite3
from typing import Any

from rapidfuzz import fuzz

from src.config import DB_PATH
from src.services import http
from src.services.providers import providers

KIND_LABEL = {
    "bmtc_stop": "BMTC stop",
    "metro_station": "Metro station",
    "osm_bus_stop": "Bus stop (OSM)",
    "osm_metro_station": "Metro station (OSM)",
    "osm_campus": "Campus",
    "osm_place": "Place",
    "campus": "Campus",
}
# Kinds that can be used directly as a routing endpoint.
ROUTABLE = {"bmtc_stop", "metro_station", "osm_bus_stop", "osm_metro_station", "osm_campus",
            "osm_place", "campus"}
KIND_BONUS = {"bmtc_stop": 3, "metro_station": 2, "campus": 1.5, "osm_campus": 1.5}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())


@functools.lru_cache(maxsize=1)
def _index() -> list[dict]:
    """One flat list of searchable places, loaded once per process."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT place_id, name, norm_name, kind, lat, lon, stop_id, aliases, source "
        "FROM places WHERE kind IN (%s)" % ",".join("?" * len(ROUTABLE)),
        tuple(ROUTABLE))]
    con.close()
    for r in rows:
        r["norm"] = r["norm_name"] or _norm(r["name"])
        r["label"] = KIND_LABEL.get(r["kind"], r["kind"])
        r["routable"] = r["kind"] in ROUTABLE
        r["tokens"] = set(r["norm"].split())
    return rows


def index_size() -> int:
    return len(_index())


@functools.lru_cache(maxsize=1)
def _inverted() -> tuple[dict[str, list[int]], list[str]]:
    """token -> row ids, plus the flat list of normalised names for prefix scanning.

    Without this, every keystroke ran WRatio over 13k strings (~30-90 ms). Restricting the
    fuzzy pass to rows that share a token or a 3-character prefix brings it to ~2 ms.
    """
    tokens: dict[str, list[int]] = {}
    names: list[str] = []
    for i, row in enumerate(_index()):
        names.append(row["norm"])
        for tok in row["norm"].split():
            tokens.setdefault(tok, []).append(i)
            # 3-char prefix keys so "jaya" finds "jayanagar" without a full scan
            if len(tok) > 3:
                tokens.setdefault(tok[:3], []).append(i)
    return tokens, names


def _candidates(q: str, q_tokens: set[str], cap: int = 400) -> list[int]:
    tokens, _ = _inverted()
    hit: dict[int, None] = {}
    for tok in q_tokens | {q[:3]}:
        for idx in tokens.get(tok, ()):
            hit[idx] = None
            if len(hit) >= cap:
                return list(hit)
    return list(hit)


def _score(q: str, q_tokens: set[str], row: dict) -> float:
    name = row["norm"]
    if name == q:
        s = 100.0
    elif name.startswith(q):
        s = 96.0
    elif q in row["tokens"]:
        s = 92.0
    elif q in name:
        s = 84.0
    else:
        s = fuzz.partial_ratio(q, name) * 0.72
    if row.get("aliases") and q in _norm(row["aliases"]):
        s = max(s, 95.0)
    overlap = q_tokens & row["tokens"]
    if overlap:
        s += 2.0 * len(overlap)
    s += KIND_BONUS.get(row["kind"], 0)
    if len(q_tokens) > 1 and not overlap and name != q:
        s -= 12.0
    return s


def autocomplete(query: str, limit: int = 8, kinds: list[str] | None = None) -> dict:
    """Local suggestions. Deterministic, ~2 ms, no network."""
    q = _norm(query)
    if len(q) < 2:
        return {"ok": True, "query": query, "suggestions": [],
                "source": "local", "note": "type at least 2 characters"}
    q_tokens = set(q.split())
    allowed = set(kinds) if kinds else ROUTABLE
    idx = _index()
    pool = _candidates(q, q_tokens)
    scored: list[tuple[float, dict]] = []
    for i in pool:
        row = idx[i]
        if row["kind"] not in allowed:
            continue
        if not (q in row["norm"] or q_tokens & row["tokens"]
                or fuzz.WRatio(q, row["norm"]) >= 82):
            continue
        s = _score(q, q_tokens, row)
        if s < 55:
            continue
        scored.append((s, row))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    out: list[dict] = []
    seen: set[str] = set()
    for s, row in scored:
        # One entry per distinct place name; a BMTC stop outranks an OSM duplicate
        # because it carries a stop_id the router can use directly.
        dedupe = row["norm"]
        if dedupe in seen:
            continue
        seen.add(dedupe)
        out.append({
            "name": row["name"], "label": row["label"], "kind": row["kind"],
            "lat": row["lat"], "lon": row["lon"], "stop_id": row["stop_id"],
            "place_id": row["place_id"], "source": row["source"] or "local",
            "confidence": round(min(s, 100.0), 1), "routable": row["routable"],
        })
        if len(out) >= limit:
            break
    return {"ok": True, "query": query, "suggestions": out, "source": "local",
            "indexed_places": index_size()}


def google_autocomplete(query: str, *, session_token: str = "", limit: int = 5) -> dict:
    """Places Autocomplete, only attempted when a server key exists."""
    cfg = providers()
    if not cfg.google_server:
        return {"ok": False, "configured": False,
                "error": "GOOGLE_MAPS_SERVER_API_KEY not set"}
    params = {"input": query, "components": "country:in",
              "location": "12.9716,77.5946", "radius": "50000",
              "key": cfg.server_key, "language": "en"}
    if session_token:
        params["sessiontoken"] = session_token
    res = http.get_json("https://maps.googleapis.com/maps/api/place/autocomplete/json",
                        params, provider="google_places", endpoint="place_autocomplete")
    if not res["ok"]:
        return {"ok": False, "configured": True, "error": res["error"]}
    preds = []
    for p in (res["data"].get("predictions") or [])[:limit]:
        preds.append({
            "name": p.get("structured_formatting", {}).get("main_text")
                    or p.get("description", ""),
            "description": p.get("description", ""),
            "place_id": p.get("place_id"),
            "label": "Google Places",
            "source": "google_places",
            "kind": "google_place",
            "routable": False,       # needs a Details call for coordinates
            "lat": None, "lon": None, "stop_id": None,
        })
    return {"ok": True, "configured": True, "predictions": preds,
            "provider_status": res.get("provider_status")}


def place_details(place_id: str) -> dict:
    cfg = providers()
    if not cfg.google_server:
        return {"ok": False, "configured": False,
                "error": "GOOGLE_MAPS_SERVER_API_KEY not set"}
    res = http.get_json("https://maps.googleapis.com/maps/api/place/details/json",
                        {"place_id": place_id, "fields": "geometry,formatted_address,name,type",
                         "key": cfg.server_key},
                        provider="google_places", endpoint="place_details")
    if not res["ok"]:
        return {"ok": False, "configured": True, "error": res["error"]}
    d = res["data"].get("result") or {}
    loc = (d.get("geometry") or {}).get("location") or {}
    return {"ok": True, "name": d.get("name"), "address": d.get("formatted_address"),
            "lat": loc.get("lat"), "lon": loc.get("lng"), "place_id": place_id,
            "source": "google_places"}


def geocode(address: str) -> dict:
    """Forward geocode. Google when configured, local resolution otherwise.

    Ambiguity is surfaced rather than resolved silently: Google often returns several
    candidates for "DSU" or "MG Road", and picking the first one without saying so is how a
    planner ends up routing to the wrong campus. Callers get ``candidates`` and an
    ``ambiguous`` flag; the top result is still filled in for the simple path.
    """
    cfg = providers()
    if cfg.google_server:
        res = http.get_json("https://maps.googleapis.com/maps/api/geocode/json",
                            {"address": address, "region": "in", "key": cfg.server_key},
                            provider="google_geocoding", endpoint="geocode")
        if res["ok"] and res["data"].get("results"):
            results = res["data"]["results"][:5]
            cands = []
            for r in results:
                loc = ((r.get("geometry") or {}).get("location") or {})
                cands.append({
                    "formatted": r.get("formatted_address", address),
                    "lat": loc.get("lat"), "lon": loc.get("lng"),
                    "place_id": r.get("place_id"),
                    "types": r.get("types") or [],
                })
            top = results[0]
            loc = top["geometry"]["location"]
            return {"ok": True, "lat": loc["lat"], "lon": loc["lng"],
                    "formatted": top.get("formatted_address", address),
                    "source": "google_geocoding", "place_id": top.get("place_id"),
                    "candidates": cands,
                    "ambiguous": len(cands) > 1,
                    "ambiguity_note": (
                        f"{len(cands)} distinct places match; the first is used unless the "
                        f"caller picks another" if len(cands) > 1 else "")}
        if not res["ok"]:
            # fall through to local rather than failing the request
            local = _local_resolve(address)
            local["google_error"] = res["error"]
            return local
    local = _local_resolve(address)
    local["ambiguous"] = False
    local["candidates"] = ([{"formatted": local.get("formatted"),
                             "lat": local.get("lat"), "lon": local.get("lon")}]
                           if local.get("ok") else [])
    return local


def reverse_geocode(lat: float, lon: float) -> dict:
    """Coordinates -> address. Google when configured, nearest indexed place otherwise.

    The local fallback is honest about what it is: the nearest *indexed* place (a GTFS stop
    or an OSM campus), not a street address, and the distance to it is included.
    """
    cfg = providers()
    if cfg.google_server:
        res = http.get_json("https://maps.googleapis.com/maps/api/geocode/json",
                            {"latlng": f"{lat},{lon}", "region": "in", "key": cfg.server_key},
                            provider="google_geocoding", endpoint="reverse_geocode")
        if res["ok"] and res["data"].get("results"):
            r0 = res["data"]["results"][0]
            loc = ((r0.get("geometry") or {}).get("location") or {})
            cands = [{"formatted": r.get("formatted_address"),
                      "lat": ((r.get("geometry") or {}).get("location") or {}).get("lat"),
                      "lon": ((r.get("geometry") or {}).get("location") or {}).get("lng")}
                     for r in res["data"]["results"][:5]]
            return {"ok": True, "latitude": lat, "longitude": lon,
                    "name": r0.get("formatted_address"),
                    "formatted": r0.get("formatted_address"),
                    "lat": loc.get("lat"), "lon": loc.get("lng"),
                    "source": "google_geocoding", "precision": "street address",
                    "candidates": cands, "ambiguous": len(cands) > 1}
        if not res["ok"]:
            local = _local_reverse(lat, lon)
            local["google_error"] = res["error"]
            return local
    return _local_reverse(lat, lon)


def _local_reverse(lat: float, lon: float) -> dict:
    best = None
    for row in _index():
        if row["lat"] is None or row["lon"] is None:
            continue
        d = (row["lat"] - lat) ** 2 + ((row["lon"] - lon) * 0.974) ** 2
        if best is None or d < best[0]:
            best = (d, row)
    if not best:
        return {"ok": False, "error": "no indexed place near that coordinate",
                "source": "local"}
    d_km = best[0] ** 0.5 * 111.0
    return {"ok": True, "latitude": lat, "longitude": lon, "name": best[1]["name"],
            "kind": best[1]["kind"], "stop_id": best[1]["stop_id"],
            "distance_km": round(d_km, 2), "source": "local index",
            "precision": "nearest indexed place, not a street address"}


def _local_resolve(address: str) -> dict:
    from src.tools.common import resolve_place
    r = resolve_place(address)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "no match"), "source": "local"}
    return {"ok": True, "lat": r.get("lat"), "lon": r.get("lon"),
            "formatted": r.get("name"), "source": "local",
            "stop_id": r.get("stop_id"), "kind": r.get("kind"),
            "snapped": r.get("snapped", False),
            "snap_distance_km": r.get("snap_distance_km")}


def search(query: str, *, limit: int = 8, include_google: bool = True) -> dict:
    """Autocomplete + optional Google merge, with a truthful ``providers_used`` list."""
    local = autocomplete(query, limit=limit)
    used = ["local"]
    google_note = ""
    if include_google and providers().google_server and len(_norm(query)) >= 3:
        g = google_autocomplete(query, limit=max(2, limit // 2))
        if g.get("ok"):
            used.append("google_places")
            known = {s["name"] for s in local["suggestions"]}
            extra = [p for p in g["predictions"] if p["name"] not in known]
            local["suggestions"] = local["suggestions"] + extra
        else:
            google_note = g.get("error", "")
    local["providers_used"] = used
    local["google_note"] = google_note
    local["suggestions"] = local["suggestions"][:limit]
    return local


def kinds_summary() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT kind, COUNT(*) FROM places GROUP BY kind ORDER BY 2 DESC").fetchall()
    con.close()
    return [{"kind": k, "count": n, "label": KIND_LABEL.get(k, k)} for k, n in rows]
