"""GTFS-Realtime vehicle positions and trip updates.

This module implements the *format*, not a vendor. Point it at any feed you are authorised
to read and it decodes protobuf or JSON, normalises the entities, and refuses to call a
position "live" when its own timestamp is old.

Why it is built this way:

* **No feed is invented.** With nothing configured, every entry point returns
  ``available: false`` and a reason. There is no simulated fleet anywhere in this file.
* **Bengaluru specifics.** BMTC publishes realtime data through IUDX (secure, needs
  consumer credentials) and is moving toward ONDC; BMRCL shares metro data through ONDC
  participants. The reverse-engineered ``bmtcmobileapi`` endpoints exist but are neither
  an official contract nor a bulk feed -- polling them per route would load a government
  server that never agreed to it. See ``docs/GTFS_REALTIME.md``.
* **One request per interval.** Responses are cached for ``GTFS_RT_POLL_INTERVAL_S``
  (minimum 15 s) and concurrent callers share a single in-flight request, so opening ten
  browser tabs cannot become ten upstream fetches.
* **Staleness is part of the data.** A vehicle whose ``timestamp`` is older than
  ``GTFS_RT_STALE_AFTER_S`` is returned with ``is_live: false`` and an age, not dropped and
  not relabelled.

Feed URLs and auth headers come from the environment and never leave the server.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from src.services.redact import redact

TIMEOUT_S = 15.0
MIN_POLL_S = 15.0
DEFAULT_POLL_S = 20.0
DEFAULT_STALE_S = 120.0
MAX_VEHICLES = 5000
USER_AGENT = "CampusTransit/2.0 (Bengaluru transit assistant; GTFS-Realtime reader)"


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _f(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


# ----------------------------------------------------------------------- settings
def vehicle_url() -> str:
    return _env("GTFS_RT_VEHICLE_URL")


def trip_updates_url() -> str:
    return _env("GTFS_RT_TRIPUPDATES_URL")


def poll_interval_s() -> float:
    return max(MIN_POLL_S, _f("GTFS_RT_POLL_INTERVAL_S", DEFAULT_POLL_S))


def stale_after_s() -> float:
    return max(30.0, _f("GTFS_RT_STALE_AFTER_S", DEFAULT_STALE_S))


def auth_headers() -> dict[str, str]:
    """``GTFS_RT_AUTH_HEADER`` is one ``Name: value`` pair, e.g. ``Authorization: Bearer ...``.

    Kept as a single variable so a token from IUDX (or any other issuer) can be used without
    inventing a bespoke variable per provider. It is parsed here and never serialised.
    """
    raw = _env("GTFS_RT_AUTH_HEADER")
    if not raw or ":" not in raw:
        return {}
    name, _, value = raw.partition(":")
    if not name.strip() or not value.strip():
        return {}
    return {name.strip(): value.strip(), "User-Agent": USER_AGENT}


def configured() -> bool:
    return bool(vehicle_url())


# ------------------------------------------------------------------------- cache
_cache: dict[str, dict[str, Any]] = {}
_locks: dict[str, threading.Lock] = {}
_cache_lock = threading.Lock()


def _lock_for(url: str) -> threading.Lock:
    with _cache_lock:
        return _locks.setdefault(url, threading.Lock())


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ------------------------------------------------------------------------ decode
class DecodeError(RuntimeError):
    pass


def _looks_like_json(body: bytes) -> bool:
    head = body.lstrip()[:1]
    return head in (b"{", b"[")


def decode_feed(body: bytes) -> dict:
    """Decode a feed payload into plain dicts. Raises :class:`DecodeError`."""
    if not body:
        raise DecodeError("empty body")
    if _looks_like_json(body):
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise DecodeError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict) or "entity" not in data:
            raise DecodeError("JSON body has no 'entity' array")
        return {"format": "json", "header": data.get("header") or {},
                "entity": data.get("entity") or []}

    try:
        from google.transit import gtfs_realtime_pb2 as pb          # type: ignore
    except ImportError as exc:                                     # pragma: no cover
        raise DecodeError(
            "protobuf feed received, but gtfs-realtime-bindings is not installed "
            "(pip install gtfs-realtime-bindings)") from exc

    msg = pb.FeedMessage()
    try:
        msg.ParseFromString(body)
    except Exception as exc:                                       # noqa: BLE001
        raise DecodeError(f"not a GTFS-Realtime protobuf: {type(exc).__name__}") from exc
    if not msg.header.HasField("gtfs_realtime_version"):
        raise DecodeError("protobuf parsed but has no gtfs_realtime_version header")
    return {"format": "protobuf", "header": _pb_header(msg.header),
            "entity": [_pb_entity(e) for e in msg.entity]}


def _pb_header(h) -> dict:
    return {"gtfs_realtime_version": h.gtfs_realtime_version,
            "incrementality": int(getattr(h, "incrementality", 0) or 0),
            "timestamp": int(h.timestamp or 0)}


def _pb_entity(e) -> dict:
    """Flatten a FeedEntity into the same shape as GTFS-RT JSON."""
    out: dict[str, Any] = {"id": e.id}
    if e.HasField("vehicle"):
        v = e.vehicle
        pos = v.position if v.HasField("position") else None
        out["vehicle"] = {
            "trip": ({"trip_id": v.trip.trip_id, "route_id": v.trip.route_id,
                      "direction_id": int(v.trip.direction_id or 0)}
                     if v.HasField("trip") else {}),
            "vehicle": ({"id": v.vehicle.id, "label": v.vehicle.label}
                        if v.HasField("vehicle") else {}),
            "position": ({"latitude": pos.latitude, "longitude": pos.longitude,
                          "bearing": pos.bearing if pos.HasField("bearing") else None,
                          "speed": pos.speed if pos.HasField("speed") else None}
                         if pos is not None else None),
            "current_stop_sequence": (int(v.current_stop_sequence)
                                      if v.HasField("current_stop_sequence") else None),
            "current_status": int(v.current_status) if v.HasField("current_status") else None,
            "timestamp": int(v.timestamp) if v.HasField("timestamp") else None,
            "occupancy_status": (int(v.occupancy_status)
                                 if v.HasField("occupancy_status") else None),
        }
    if e.HasField("trip_update"):
        tu = e.trip_update
        out["trip_update"] = {
            "trip": ({"trip_id": tu.trip.trip_id, "route_id": tu.trip.route_id,
                      "start_date": tu.trip.start_date}
                     if tu.HasField("trip") else {}),
            "vehicle": ({"id": tu.vehicle.id, "label": tu.vehicle.label}
                        if tu.HasField("vehicle") else {}),
            "timestamp": int(tu.timestamp) if tu.HasField("timestamp") else None,
            "delay": int(tu.delay) if tu.HasField("delay") else None,
            "stop_time_update": [
                {"stop_id": s.stop_id,
                 "stop_sequence": int(s.stop_sequence) if s.HasField("stop_sequence") else None,
                 "arrival_delay": (int(s.arrival.delay)
                                   if s.HasField("arrival") and s.arrival.HasField("delay")
                                   else None),
                 "departure_delay": (int(s.departure.delay)
                                     if s.HasField("departure")
                                     and s.departure.HasField("delay") else None),
                 "eta": (int(s.arrival.time)
                         if s.HasField("arrival") and s.arrival.HasField("time") else None)}
                for s in tu.stop_time_update
            ][:40],
        }
    return out


# ------------------------------------------------------------------------- fetch
def _classify(status: int | None, detail: str) -> str:
    text = (detail or "").lower()
    if status in (401, 403) or "unauthor" in text or "forbidden" in text:
        return "auth_failure"
    if status == 429 or "rate limit" in text or "quota" in text:
        return "quota_or_billing"
    if status is None or (status and status >= 500):
        return "provider_unavailable"
    if status >= 400:
        return "provider_unavailable"
    return "provider_unavailable"


def _fetch(url: str, *, label: str) -> dict:
    """One cached fetch. Returns ``{ok, entities, header, error, kind, latency_ms, ...}``."""
    ttl = poll_interval_s()
    now = time.time()
    cached = _cache.get(url)
    if cached and now - cached["fetched_at"] < ttl:
        return {**cached["result"], "cached": True,
                "age_s": round(now - cached["result"]["fetched_at_ts"], 1)}

    lock = _lock_for(url)
    if not lock.acquire(timeout=TIMEOUT_S + 5):
        return {"ok": False, "kind": "provider_unavailable", "entities": [],
                "error": "another request for this feed is still in flight"}
    try:                                    # re-check: another thread may have filled it
        cached = _cache.get(url)
        if cached and time.time() - cached["fetched_at"] < ttl:
            return {**cached["result"], "cached": True, "age_s": 0.0}

        started = time.time()
        headers = {"Accept": "application/x-protobuf, application/json;q=0.9",
                   "User-Agent": USER_AGENT, **auth_headers()}
        try:
            with httpx.Client(timeout=TIMEOUT_S, follow_redirects=True) as c:
                r = c.get(url, headers=headers)
            latency = int((time.time() - started) * 1000)
            if r.status_code != 200:
                detail = f"HTTP {r.status_code}: {r.text[:200]}"
                result = {"ok": False, "kind": _classify(r.status_code, detail),
                          "entities": [], "error": redact(detail),
                          "latency_ms": latency, "fetched_at_ts": time.time(),
                          "http_status": r.status_code, "label": label}
            else:
                feed = decode_feed(r.content)
                ts = int((feed["header"] or {}).get("timestamp") or 0)
                result = {"ok": True, "kind": "ready", "entities": feed["entity"],
                          "header": feed["header"], "format": feed["format"],
                          "error": "", "latency_ms": latency,
                          "fetched_at_ts": time.time(),
                          "feed_timestamp": ts,
                          "feed_age_s": (round(time.time() - ts, 1) if ts else None),
                          "http_status": 200, "label": label,
                          "bytes": len(r.content)}
        except DecodeError as exc:
            result = {"ok": False, "kind": "provider_unavailable", "entities": [],
                      "error": redact(str(exc)), "latency_ms": int((time.time() - started) * 1000),
                      "fetched_at_ts": time.time(), "label": label}
        except httpx.HTTPError as exc:
            result = {"ok": False, "kind": "provider_unavailable", "entities": [],
                      "error": redact(f"{type(exc).__name__}: {exc}"),
                      "latency_ms": int((time.time() - started) * 1000),
                      "fetched_at_ts": time.time(), "label": label}

        # A failed fetch is cached briefly too: a broken feed must not be retried on every
        # page load, and the UI needs to keep showing the same reason.
        with _cache_lock:
            _cache[url] = {"fetched_at": time.time(), "result": result}
        return {**result, "cached": False, "age_s": 0.0}
    finally:
        lock.release()


# ---------------------------------------------------------------------- normalise
def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def normalise_vehicle(entity: dict, *, now: float | None = None) -> dict | None:
    v = entity.get("vehicle") or {}
    pos = v.get("position") or {}
    lat, lon = pos.get("latitude"), pos.get("longitude")
    if lat is None or lon is None:
        return None
    if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
        return None
    if float(lat) == 0 and float(lon) == 0:
        return None                      # a null island fix is a broken GPS, not a bus
    ts = v.get("timestamp")
    now = now if now is not None else time.time()
    age = round(now - ts, 1) if ts else None
    speed = pos.get("speed")
    return {
        "entity_id": entity.get("id"),
        "vehicle_id": (v.get("vehicle") or {}).get("id"),
        "label": (v.get("vehicle") or {}).get("label"),
        "route_id": (v.get("trip") or {}).get("route_id"),
        "trip_id": (v.get("trip") or {}).get("trip_id"),
        "direction_id": (v.get("trip") or {}).get("direction_id"),
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "bearing": pos.get("bearing"),
        "speed_kmh": round(float(speed) * 3.6, 1) if speed is not None else None,
        "timestamp": ts,
        "observed_at": _iso(ts),
        "age_s": age,
        # "live" is a claim about the data, so it is computed from the provider's own
        # timestamp and nothing else.
        "is_live": (age is not None and age <= stale_after_s()),
        "stale_reason": ("" if age is None or age <= stale_after_s()
                         else f"position is {int(age)}s old"),
        "current_stop_sequence": v.get("current_stop_sequence"),
        "occupancy_status": v.get("occupancy_status"),
    }


# ------------------------------------------------------------------------- public
def _unavailable(reason: str, *, needs: str = "", kind: str = "not_configured") -> dict:
    return {"available": False, "kind": kind, "reason": reason, "needs": needs,
            "vehicles": [], "source": None, "count": 0,
            "docs": "https://gtfs.org/documentation/realtime/reference/"}


def positions(*, limit: int = MAX_VEHICLES, route: str = "",
              bbox: tuple[float, float, float, float] | None = None) -> dict:
    """Live vehicle positions, or a labelled explanation of why there are none."""
    url = vehicle_url()
    if not url:
        return _unavailable(
            "No authorised GTFS-Realtime vehicle feed is configured. BMTC does not publish "
            "an open, unauthenticated vehicle-positions feed; its realtime data is available "
            "through IUDX to approved consumers. Until a feed URL is supplied, the map shows "
            "no buses and this endpoint says so.",
            needs="GTFS_RT_VEHICLE_URL (and GTFS_RT_AUTH_HEADER if your feed needs a token)",
            kind="not_configured")

    res = _fetch(url, label="vehicle_positions")
    if not res.get("ok"):
        return {"available": False, "kind": res.get("kind", "provider_unavailable"),
                "reason": f"Feed configured but could not be read: {res.get('error', '')}",
                "needs": "", "vehicles": [], "count": 0, "source": _source_label(url),
                "http_status": res.get("http_status"),
                "docs": "https://gtfs.org/documentation/realtime/reference/"}

    now = time.time()
    vehicles = [v for v in (normalise_vehicle(e, now=now)
                            for e in res.get("entities", [])) if v]
    if route:
        rq = route.strip().lower()
        vehicles = [v for v in vehicles
                    if (v.get("route_id") or "").lower() == rq
                    or (v.get("label") or "").lower().startswith(rq)]
    if bbox:
        south, west, north, east = bbox
        vehicles = [v for v in vehicles if south <= v["lat"] <= north
                    and west <= v["lon"] <= east]
    live = sum(1 for v in vehicles if v["is_live"])
    ages = [v["age_s"] for v in vehicles if v["age_s"] is not None]
    return {
        "available": True,
        "kind": "ready",
        "source": _source_label(url),
        "feed_format": res.get("format"),
        "feed_timestamp": res.get("feed_timestamp"),
        "feed_age_s": res.get("feed_age_s"),
        "fetched_at": _iso(int(res.get("fetched_at_ts") or now)),
        "cached": bool(res.get("cached")),
        "latency_ms": res.get("latency_ms"),
        "count": len(vehicles[:limit]),
        "total_in_feed": len(vehicles),
        "live_count": live,
        "stale_count": len(vehicles) - live,
        "median_age_s": (sorted(ages)[len(ages) // 2] if ages else None),
        "stale_after_s": stale_after_s(),
        "refresh_after_s": poll_interval_s(),
        "vehicles": vehicles[:limit],
        "attribution": "Live positions from the configured GTFS-Realtime feed",
        "note": ("A vehicle is marked live only when its own timestamp is within "
                 f"{int(stale_after_s())}s. Positions are the provider's; no ETA is derived "
                 "here."),
    }


def trip_updates(*, limit: int = 200) -> dict:
    url = trip_updates_url()
    if not url:
        return _unavailable(
            "No GTFS-Realtime trip-updates feed is configured, so no arrival prediction is "
            "shown. Scheduled times come from static GTFS and are labelled as scheduled.",
            needs="GTFS_RT_TRIPUPDATES_URL", kind="not_configured")
    res = _fetch(url, label="trip_updates")
    if not res.get("ok"):
        return {"available": False, "kind": res.get("kind", "provider_unavailable"),
                "reason": f"Feed configured but could not be read: {res.get('error', '')}",
                "updates": [], "count": 0, "source": _source_label(url)}
    out = []
    for e in res.get("entities", []):
        tu = e.get("trip_update")
        if not tu:
            continue
        out.append({
            "entity_id": e.get("id"),
            "trip_id": (tu.get("trip") or {}).get("trip_id"),
            "route_id": (tu.get("trip") or {}).get("route_id"),
            "vehicle_id": (tu.get("vehicle") or {}).get("id"),
            "delay_s": tu.get("delay"),
            "timestamp": tu.get("timestamp"),
            "stop_time_updates": (tu.get("stop_time_update") or [])[:10],
        })
        if len(out) >= limit:
            break
    return {"available": True, "kind": "ready", "source": _source_label(url),
            "count": len(out), "updates": out}


def _source_label(url: str) -> str:
    """Host only: the full URL can carry a token in a query string."""
    try:
        from urllib.parse import urlsplit
        return urlsplit(url).hostname or "configured feed"
    except ValueError:                                  # pragma: no cover
        return "configured feed"


def status() -> dict:
    """Cheap summary for the registry; does not fetch unless a feed is configured."""
    if not configured():
        return {"configured": False, "ok": False, "kind": "not_configured",
                "reason": "no GTFS_RT_VEHICLE_URL", "vehicles": 0, "live": 0}
    res = _fetch(vehicle_url(), label="vehicle_positions")
    if not res.get("ok"):
        return {"configured": True, "ok": False, "kind": res.get("kind"),
                "reason": res.get("error", "feed unreadable"),
                "http_status": res.get("http_status"), "vehicles": 0, "live": 0}
    now = time.time()
    vs = [v for v in (normalise_vehicle(e, now=now) for e in res.get("entities", [])) if v]
    live = sum(1 for v in vs if v["is_live"])
    return {"configured": True, "ok": True, "kind": "ready",
            "reason": (f"{len(vs):,} vehicles in the feed, {live:,} live "
                       f"(<= {int(stale_after_s())}s old)"),
            "format": res.get("format"), "vehicles": len(vs), "live": live,
            "feed_timestamp": res.get("feed_timestamp"),
            "feed_age_s": res.get("feed_age_s"), "latency_ms": res.get("latency_ms"),
            "cached": res.get("cached")}
