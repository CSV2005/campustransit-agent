"""Thin, honest HTTP layer for the optional Google APIs.

Three rules, all of them load-bearing:

1. A failure is *reported*, never smoothed over. Callers get ``{ok: False, error: ...}``
   with the provider's own message, so the UI can say "Google said REQUEST_DENIED"
   instead of inventing a duration.
2. Every attempt lands in ``api_call_log`` so ``/api/data-sources`` can show real evidence
   of what was contacted and when.
3. Responses are cached in-process for a short TTL. Geocoding the same string twice in one
   session is wasteful and, with a metered key, expensive.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

import httpx

from src.config import DB_PATH
from src.services.providers import providers

_cache: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()
CACHE_TTL_S = 300.0


def _log(provider: str, endpoint: str, ok: bool, status: int | None,
         latency_ms: int, detail: str = "") -> None:
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        con.execute(
            "INSERT INTO api_call_log(provider, endpoint, ok, status_code, latency_ms, "
            "detail, called_at) VALUES (?,?,?,?,?,?,?)",
            (provider, endpoint, 1 if ok else 0, status, latency_ms, detail[:400],
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
        con.commit()
        con.close()
    except sqlite3.Error:
        pass  # logging must never break a request


def log_call(provider: str, endpoint: str, ok: bool, status: int | None,
             latency_ms: int, detail: str = "") -> None:
    """Public wrapper so probes elsewhere can use the same audit trail."""
    _log(provider, endpoint, ok, status, latency_ms, detail)


def _cache_get(key: str) -> Any | None:
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        if hit:
            _cache.pop(key, None)
    return None


def _cache_put(key: str, value: Any) -> None:
    with _lock:
        if len(_cache) > 512:
            _cache.clear()
        _cache[key] = (time.time(), value)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


class GoogleNotConfigured(RuntimeError):
    pass


def server_key() -> str:
    key = providers().server_key
    if not key:
        raise GoogleNotConfigured(
            "GOOGLE_MAPS_SERVER_API_KEY is not set — this integration is unavailable")
    return key


def get_json(url: str, params: dict, *, provider: str, endpoint: str,
             headers: dict | None = None, ttl: float = CACHE_TTL_S) -> dict:
    """GET with retry + logging. Returns ``{ok, status, data, error, latency_ms, cached}``."""
    cfg = providers()
    cache_key = f"{endpoint}|{json.dumps(params, sort_keys=True)}"
    if ttl:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    last_err = ""
    started = time.time()
    status: int | None = None
    for attempt in range(cfg.max_retries + 1):
        try:
            with httpx.Client(timeout=cfg.timeout_s) as client:
                r = client.get(url, params=params, headers=headers or {})
            status = r.status_code
            latency = int((time.time() - started) * 1000)
            if r.status_code == 200:
                data = r.json()
                body_status = str(data.get("status", "OK"))
                ok = body_status in ("OK", "ZERO_RESULTS")
                out = {"ok": ok, "status": r.status_code, "data": data,
                       "error": "" if ok else f"{body_status}: {data.get('error_message', '')}".strip(),
                       "latency_ms": latency, "cached": False,
                       "provider_status": body_status}
                _log(provider, endpoint, ok, r.status_code, latency,
                     "" if ok else out["error"])
                if ttl:
                    _cache_put(cache_key, out)
                return out
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (400, 401, 403, 404):
                break  # client errors will not fix themselves on retry
        except httpx.HTTPError as exc:      # transport, timeout, DNS
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(min(0.4 * (attempt + 1), 1.5))

    latency = int((time.time() - started) * 1000)
    _log(provider, endpoint, False, status, latency, last_err)
    return {"ok": False, "status": status, "data": None, "error": last_err,
            "latency_ms": latency, "cached": False}


def post_json(url: str, body: dict, *, provider: str, endpoint: str,
              headers: dict | None = None, ttl: float = 0.0) -> dict:
    """POST variant (Routes API uses POST). Same contract as :func:`get_json`."""
    cfg = providers()
    cache_key = f"{endpoint}|{json.dumps(body, sort_keys=True)}"
    if ttl:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    last_err = ""
    started = time.time()
    status: int | None = None
    hdrs = {"Content-Type": "application/json", "X-Goog-FieldMask": "*"}
    hdrs.update(headers or {})
    for attempt in range(cfg.max_retries + 1):
        try:
            with httpx.Client(timeout=cfg.timeout_s) as client:
                r = client.post(url, json=body, headers=hdrs)
            status = r.status_code
            latency = int((time.time() - started) * 1000)
            if r.status_code == 200:
                data = r.json()
                out = {"ok": True, "status": 200, "data": data, "error": "",
                       "latency_ms": latency, "cached": False}
                _log(provider, endpoint, True, 200, latency)
                if ttl:
                    _cache_put(cache_key, out)
                return out
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (400, 401, 403, 404):
                break
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(min(0.4 * (attempt + 1), 1.5))

    latency = int((time.time() - started) * 1000)
    _log(provider, endpoint, False, status, latency, last_err)
    return {"ok": False, "status": status, "data": None, "error": last_err,
            "latency_ms": latency, "cached": False}


def recent_calls(limit: int = 25) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT provider, endpoint, ok, status_code, latency_ms, detail, called_at "
        "FROM api_call_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]
