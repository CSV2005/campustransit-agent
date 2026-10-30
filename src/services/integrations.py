"""Integration catalog: one record per external dependency, with a state machine.

The problem this solves is concrete. Before this module, a capability was "available"
whenever its environment variable was non-empty. That is not a health check -- a revoked
key, a disabled API in the Google Cloud console, an expired ORS token and a typo all look
identical, and the UI cheerfully said "ready".

So every integration here carries:

* what it is, who runs it, what it is *for*, and where its official console lives;
* which environment variables it needs (names only -- values never leave the process);
* whether it costs money, and what the safe test is;
* a **state** derived from configuration *and* the last recorded check, never from an env
  var alone.

States (brief section 4):

    not_configured | configured_untested | ready | disabled
    auth_failure | quota_or_billing | provider_unavailable | unsupported

Storage is two small SQLite tables created on first use, so an upgrade needs no migration
step: ``integration_checks`` (one row per integration, last result) and ``integration_prefs``
(the enable/disable switch).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from src.config import DB_PATH
from src.services.redact import redact

# --------------------------------------------------------------------------- states
NOT_CONFIGURED = "not_configured"
CONFIGURED_UNTESTED = "configured_untested"
READY = "ready"
DISABLED = "disabled"
AUTH_FAILURE = "auth_failure"
QUOTA_OR_BILLING = "quota_or_billing"
PROVIDER_UNAVAILABLE = "provider_unavailable"
UNSUPPORTED = "unsupported"

STATE_ORDER = (READY, CONFIGURED_UNTESTED, NOT_CONFIGURED, DISABLED,
               AUTH_FAILURE, QUOTA_OR_BILLING, PROVIDER_UNAVAILABLE, UNSUPPORTED)

STATE_LABELS = {
    READY: "Ready",
    CONFIGURED_UNTESTED: "Configured, untested",
    NOT_CONFIGURED: "Not configured",
    DISABLED: "Disabled",
    AUTH_FAILURE: "Authentication failed",
    QUOTA_OR_BILLING: "Quota or billing problem",
    PROVIDER_UNAVAILABLE: "Provider unavailable",
    UNSUPPORTED: "Unsupported here",
}

# A success older than this is not evidence of a working key today.
CHECK_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class Integration:
    """One external dependency, described once and rendered everywhere."""
    id: str
    name: str
    provider: str
    purpose: str
    category: str                        # maps | geocoding | routing | traffic | transit | llm | data
    env_vars: tuple[str, ...] = ()       # required
    optional_env: tuple[str, ...] = ()
    credential: str = "none"             # server | browser | none
    optional: bool = True
    available_in_build: bool = True
    billable: bool = False
    test_kind: str = "local"             # local | free | quota | billable | browser | none
    setup_url: str = ""
    docs_url: str = ""
    restrictions: tuple[str, ...] = ()
    notes: str = ""
    live_check: Callable[[], dict] | None = field(default=None, repr=False, compare=False)


# ------------------------------------------------------------------------ catalogue
def _catalog() -> list[Integration]:
    return [
        Integration(
            id="google_maps_js",
            name="Google Maps JavaScript API",
            provider="Google Maps Platform",
            purpose="Optional basemap and vector rendering in the browser.",
            category="maps",
            env_vars=("GOOGLE_MAPS_BROWSER_KEY",),
            optional_env=("GOOGLE_MAPS_MAP_ID",),
            credential="browser",
            billable=True,
            test_kind="browser",
            setup_url="https://console.cloud.google.com/google/maps-apis/credentials",
            docs_url="https://developers.google.com/maps/documentation/javascript/get-api-key",
            restrictions=(
                "The browser key MUST be restricted by HTTP referrer; treat it as public.",
                "Map loads are billed per 1,000 (Dynamic Maps SKU) on the pay-as-you-go plan.",
                "The server key is never used for this renderer.",
            ),
            notes="Verified by loading the map in a browser; there is no server-side check "
                  "that can validate a referrer-restricted key without billing a map load.",
        ),
        Integration(
            id="google_places",
            name="Google Places API",
            provider="Google Maps Platform",
            purpose="Destination autocomplete and place lookup, merged over the local index.",
            category="geocoding",
            env_vars=("GOOGLE_MAPS_SERVER_API_KEY",),
            credential="server",
            billable=True,
            test_kind="billable",
            setup_url="https://console.cloud.google.com/google/maps-apis/api-list",
            docs_url="https://developers.google.com/maps/documentation/places/web-service/autocomplete",
            restrictions=(
                "Autocomplete requests are billed per 1,000 (Autocomplete - Per Request).",
                "Session tokens reduce cost when a prediction is followed by a Details call; "
                "this app does not use them, so each keystroke-backed request is billed.",
                "Results are merged with the local index, never replace it; local stays the "
                "fallback when the provider fails.",
            ),
        ),
        Integration(
            id="google_geocoding",
            name="Google Geocoding API",
            provider="Google Maps Platform",
            purpose="Forward and reverse geocoding of free-text addresses.",
            category="geocoding",
            env_vars=("GOOGLE_MAPS_SERVER_API_KEY",),
            credential="server",
            billable=True,
            test_kind="billable",
            setup_url="https://console.cloud.google.com/google/maps-apis/api-list",
            docs_url="https://developers.google.com/maps/documentation/geocoding/requests-geocoding",
            restrictions=(
                "Billed per 1,000 requests; results are cached in-process for 5 minutes.",
                "Ambiguous results are returned as a list and never silently collapsed to one.",
            ),
        ),
        Integration(
            id="google_routes",
            name="Google Routes API",
            provider="Google Maps Platform",
            purpose="Road routes: distance, duration, alternatives and encoded geometry.",
            category="routing",
            env_vars=("GOOGLE_MAPS_SERVER_API_KEY",),
            credential="server",
            billable=True,
            test_kind="billable",
            setup_url="https://console.cloud.google.com/google/maps-apis/api-list",
            docs_url="https://developers.google.com/maps/documentation/routes/compute_route_directions",
            restrictions=(
                "Billed per 1,000 requests, SKU depends on the routing preference used.",
                "Traffic-aware routing costs more than traffic-unaware; the app asks for it "
                "only when the caller wants a time-sensitive answer.",
                "Durations are the provider's; fares are never taken from Google.",
            ),
        ),
        Integration(
            id="google_traffic",
            name="Traffic-aware durations (Google Routes)",
            provider="Google Maps Platform",
            purpose="Live vs free-flow duration for a road corridor.",
            category="traffic",
            env_vars=("GOOGLE_MAPS_SERVER_API_KEY",),
            credential="server",
            billable=True,
            test_kind="billable",
            setup_url="https://console.cloud.google.com/google/maps-apis/api-list",
            docs_url="https://developers.google.com/maps/documentation/routes/traffic",
            restrictions=(
                "Requires routingPreference=TRAFFIC_AWARE_OPTIMAL; billed at the "
                "traffic-aware SKU.",
                "Traffic is reported only for the corridor actually requested. Nothing is "
                "interpolated to other modes or times.",
            ),
        ),
        Integration(
            id="openrouteservice",
            name="OpenRouteService",
            provider="HeiGIT / Heidelberg Institute",
            purpose="Optional road routing when Google is not configured.",
            category="routing",
            env_vars=("OPENROUTESERVICE_API_KEY",),
            credential="server",
            billable=False,
            test_kind="free",
            setup_url="https://account.heigit.org/signup",
            docs_url="https://openrouteservice.org/dev/#/api-docs/v2/directions/{profile}/get",
            restrictions=(
                "Free tier is rate-limited (40 requests/minute, 2,000 directions/day at the "
                "time of writing); check the current plan before relying on it.",
                "No live traffic: ORS drives a static OSM network. Durations are model "
                "estimates, so this app never labels an ORS duration as traffic-aware.",
                "Geometry is a decoded GeoJSON LineString from the provider.",
            ),
            notes="The key validation itself needs a directions call, which consumes quota.",
        ),
        Integration(
            id="tomtom",
            name="TomTom Routing",
            provider="TomTom",
            purpose="Optional road routing with live-traffic durations.",
            category="routing",
            env_vars=("TOMTOM_API_KEY",),
            credential="server",
            billable=True,
            test_kind="billable",
            setup_url="https://developer.tomtom.com/user/register",
            docs_url="https://developer.tomtom.com/routing-api/documentation/routing/calculate-route",
            restrictions=(
                "Free tier is a monthly request allowance; every call is metered.",
                "Traffic-aware durations are returned when the request asks for them; the "
                "response is labelled traffic_aware=true only in that case.",
                "Attribution to TomTom is mandatory wherever its results are displayed.",
            ),
            notes="Region availability: routing works for India; check the current coverage "
                  "map before trusting it outside Bengaluru.",
        ),
        Integration(
            id="osm_tiles",
            name="OpenStreetMap raster tiles",
            provider="OpenStreetMap Foundation",
            purpose="Basemap for the Leaflet renderer, proxied and cached by this server.",
            category="maps",
            env_vars=(),
            optional_env=("OSM_TILE_URL", "OSM_USER_AGENT"),
            billable=False,
            test_kind="local",
            setup_url="https://www.openstreetmap.org/",
            docs_url="https://operations.osmfoundation.org/policies/tiles/",
            restrictions=(
                "No bulk downloading; the proxy caches to disk with a 7-day TTL and a "
                "4,000-tile cap.",
                "A descriptive User-Agent identifies the application on every request.",
                "Heavy or commercial use requires a third-party tile provider.",
            ),
        ),
        Integration(
            id="gtfs_static",
            name="GTFS static (BMTC + BMRCL)",
            provider="Bengaluru Metropolitan Transport Corporation / BMRCL",
            purpose="Stops, routes, schedules and metro line geometry.",
            category="transit",
            env_vars=(),
            billable=False,
            test_kind="local",
            setup_url="https://github.com/Vonter/bmtc-gtfs",
            docs_url="https://gtfs.org/documentation/schedule/reference/",
            restrictions=(
                "Schedules are as published in the feed; the BMTC extract carries no "
                "departure times, so journey times are estimated from segment geometry.",
                "Never re-timed or smoothed by the app.",
            ),
        ),
        Integration(
            id="gtfs_realtime",
            name="GTFS-Realtime (vehicle positions, trip updates)",
            provider="BMTC / BMRCL",
            purpose="Live arrival predictions and vehicle positions.",
            category="transit",
            env_vars=("GTFS_RT_VEHICLE_URL",),
            optional_env=("GTFS_RT_TRIPUPDATES_URL", "GTFS_RT_AUTH_HEADER",
                          "GTFS_RT_POLL_INTERVAL_S", "GTFS_RT_STALE_AFTER_S"),
            credential="server",
            optional=True,
            billable=False,
            test_kind="free",
            setup_url="https://gtfs.org/documentation/realtime/reference/",
            docs_url="https://gtfs.org/documentation/realtime/reference/",
            restrictions=(
                "The app ships no feed. BMTC's realtime data is published through IUDX to "
                "approved consumers; there is no open, unauthenticated URL, and nothing here "
                "invents one.",
                "Feed responses are cached for at least 15 s and concurrent readers share one "
                "upstream request, so the provider sees at most one call per interval.",
                "A vehicle is marked live only when its own GTFS-RT timestamp is within "
                "GTFS_RT_STALE_AFTER_S; older positions are served as delayed, never refreshed.",
                "The URL is a server-side setting. Tokens go in GTFS_RT_AUTH_HEADER and are "
                "redacted from every response and log line.",
            ),
            notes="Point GTFS_RT_VEHICLE_URL at a feed you are licensed to read. The decoder "
                  "handles protobuf and GTFS-RT JSON and is tested against a local fixture.",
        ),
        Integration(
            id="bmtc_unofficial_api",
            name="BMTC unofficial WebAPI (bmtcmobileapi)",
            provider="BMTC, via a reverse-engineered mobile endpoint",
            purpose="Per-route live bus coordinates, if it were permitted.",
            category="transit",
            env_vars=(),
            optional=True,
            available_in_build=False,
            billable=False,
            test_kind="none",
            setup_url="",
            docs_url="https://github.com/PratyushBalaji/BMTC-API",
            restrictions=(
                "Undocumented, unversioned and not an integration contract: the response "
                "shape can change without notice.",
                "It is a per-route screen-scrape target, not a bulk feed. Polling it from a "
                "dashboard would put avoidable load on a government server that never "
                "published these endpoints for this use.",
                "Its own documentation says educational use only. Not wired up by default; "
                "see docs/GTFS_REALTIME.md for what would have to change first.",
            ),
            notes="Deliberately unsupported. The supported path is an authorised GTFS-RT "
                  "feed via GTFS_RT_VEHICLE_URL.",
        ),
        Integration(
            id="ollama",
            name="Ollama (local LLM)",
            provider="Ollama, running on this machine",
            purpose="Natural-language answers that call the same tools as the UI.",
            category="llm",
            env_vars=(),
            optional_env=("OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_TIMEOUT_S", "LLM_ENABLED"),
            billable=False,
            test_kind="free",
            setup_url="https://ollama.com/download",
            docs_url="https://github.com/ollama/ollama/blob/main/docs/api.md",
            restrictions=(
                "Runs locally: no external LLM key and no request leaves the machine.",
                "The rules engine answers whenever the model is missing, slow or ungrounded.",
            ),
        ),
        Integration(
            id="policy_rag",
            name="Policy retrieval (Chroma)",
            provider="Chroma, local index",
            purpose="Cited answers about passes, fares rules and student concessions.",
            category="data",
            env_vars=(),
            billable=False,
            test_kind="local",
            setup_url="https://docs.trychroma.com/",
            docs_url="https://docs.trychroma.com/",
            restrictions=("Answers cite the indexed document; no answer is generated from "
                          "the model's memory."),
        ),
        Integration(
            id="nominatim",
            name="Nominatim public API",
            provider="OpenStreetMap Foundation",
            purpose="Geocoding the app deliberately does not call.",
            category="geocoding",
            env_vars=(),
            available_in_build=False,
            test_kind="none",
            setup_url="https://nominatim.org/",
            docs_url="https://operations.osmfoundation.org/policies/nominatim/",
            restrictions=(
                "The public instance forbids autocomplete-style querying (one request per "
                "keystroke) and caps throughput at 1 req/s.",
                "OSM data here is a baked extract; geocoding uses Google (if configured) or "
                "the local index.",
            ),
            notes="Listed so the Settings page can say honestly that it is unsupported by "
                  "policy rather than merely missing.",
        ),
    ]


CATALOG: dict[str, Integration] = {i.id: i for i in _catalog()}


# ------------------------------------------------------------------------- storage
def _con() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS integration_checks("
        " integration_id TEXT PRIMARY KEY, state TEXT NOT NULL, ok INTEGER NOT NULL,"
        " detail TEXT, latency_ms INTEGER, http_status INTEGER, kind TEXT, checked_at TEXT)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS integration_prefs("
        " integration_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, updated_at TEXT)")
    return con


def last_check(integration_id: str) -> dict | None:
    con = _con()
    row = con.execute("SELECT * FROM integration_checks WHERE integration_id=?",
                      (integration_id,)).fetchone()
    con.close()
    if not row:
        return None
    d = dict(row)
    d["stale"] = _is_stale(d.get("checked_at") or "")
    return d


def record_check(integration_id: str, *, state: str, ok: bool, detail: str,
                 latency_ms: int | None = None, http_status: int | None = None,
                 kind: str = "") -> dict:
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    con = _con()
    con.execute(
        "INSERT INTO integration_checks(integration_id, state, ok, detail, latency_ms,"
        " http_status, kind, checked_at) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(integration_id) DO UPDATE SET state=excluded.state, ok=excluded.ok,"
        " detail=excluded.detail, latency_ms=excluded.latency_ms,"
        " http_status=excluded.http_status, kind=excluded.kind, checked_at=excluded.checked_at",
        (integration_id, state, 1 if ok else 0, redact(detail)[:600], latency_ms,
         http_status, kind, ts))
    con.commit()
    con.close()
    return last_check(integration_id) or {}


def _is_stale(checked_at: str) -> bool:
    if not checked_at:
        return True
    try:
        when = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - when > CHECK_TTL


def is_enabled(integration_id: str) -> bool:
    con = _con()
    row = con.execute("SELECT enabled FROM integration_prefs WHERE integration_id=?",
                      (integration_id,)).fetchone()
    con.close()
    return True if row is None else bool(row["enabled"])


def set_enabled(integration_id: str, enabled: bool) -> dict:
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    con = _con()
    con.execute(
        "INSERT INTO integration_prefs(integration_id, enabled, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(integration_id) DO UPDATE SET enabled=excluded.enabled,"
        " updated_at=excluded.updated_at",
        (integration_id, 1 if enabled else 0, ts))
    con.commit()
    con.close()
    return status(integration_id) or {}


def clear_checks(integration_id: str | None = None) -> None:
    con = _con()
    if integration_id:
        con.execute("DELETE FROM integration_checks WHERE integration_id=?", (integration_id,))
    else:
        con.execute("DELETE FROM integration_checks")
    con.commit()
    con.close()


# -------------------------------------------------------------------- env helpers
def env_set(name: str) -> bool:
    import os
    return bool((os.getenv(name) or "").strip())


def is_configured(defn: Integration) -> bool:
    if defn.credential == "none" and not defn.env_vars:
        return True
    return all(env_set(v) for v in defn.env_vars)


# ------------------------------------------------------------------------ statuses
def _derive_state(defn: Integration, *, check: dict | None, enabled: bool,
                  live: dict | None) -> tuple[str, str, bool]:
    """Return (state, reason, healthy). The order of these tests is the policy."""
    if not defn.available_in_build:
        return UNSUPPORTED, defn.notes or "not supported in this build", False
    if not enabled:
        return DISABLED, "switched off in Settings", False
    if not is_configured(defn):
        missing = [v for v in defn.env_vars if not env_set(v)]
        return (NOT_CONFIGURED,
                "set " + ", ".join(missing) if missing else "no credentials configured",
                False)

    # Local integrations are cheap to check on every read; doing so is what makes their
    # "ready" a fact rather than an assumption.
    if live is not None:
        if live.get("ok"):
            return READY, live.get("detail", "checked locally"), True
        return PROVIDER_UNAVAILABLE, live.get("detail", "local check failed"), False

    if check:
        state = check.get("state") or CONFIGURED_UNTESTED
        if check.get("stale") and state == READY:
            return (CONFIGURED_UNTESTED,
                    f"last successful check was {check.get('checked_at', 'unknown')} "
                    f"(older than {int(CHECK_TTL.total_seconds() // 3600)} h)", False)
        if state == READY:
            return READY, check.get("detail") or "last check passed", True
        return state, check.get("detail") or STATE_LABELS.get(state, state), False

    return (CONFIGURED_UNTESTED,
            "credentials present, never tested - run the connection test", False)


def status(integration_id: str) -> dict | None:
    defn = CATALOG.get(integration_id)
    if not defn:
        return None
    enabled = is_enabled(defn.id)
    check = last_check(defn.id)
    live = None
    check_fn = defn.live_check or _live_check_for(defn.id)
    if check_fn and enabled and is_configured(defn) and defn.available_in_build:
        try:
            live = check_fn()
        except Exception as exc:                      # a health check never raises upward
            live = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    state, reason, healthy = _derive_state(defn, check=check, enabled=enabled, live=live)
    return {
        "id": defn.id,
        "name": defn.name,
        "provider": defn.provider,
        "purpose": defn.purpose,
        "category": defn.category,
        "state": state,
        "state_label": STATE_LABELS[state],
        "healthy": healthy,
        "reason": redact(reason),
        "enabled": enabled,
        "configured": is_configured(defn),
        "credential": defn.credential,
        "optional": defn.optional,
        "billable": defn.billable,
        "env_vars": [{"name": v, "set": env_set(v), "required": True} for v in defn.env_vars]
                    + [{"name": v, "set": env_set(v), "required": False}
                       for v in defn.optional_env],
        "setup_url": defn.setup_url,
        "docs_url": defn.docs_url,
        "restrictions": list(defn.restrictions),
        "notes": redact(defn.notes),
        "test": {
            "kind": defn.test_kind,
            "supported": defn.test_kind in ("local", "free", "quota", "billable"),
            "requires_confirmation": defn.test_kind in ("quota", "billable"),
            "warning": test_warning(defn),
        },
        "last_check": _public_check(check),
    }


def _live_check_for(integration_id: str):
    """Local, free, cheap checks live in ``integration_tests``; import lazily to avoid a
    cycle (that module imports this one for the catalogue and the state constants)."""
    try:
        from src.services.integration_tests import LIVE_CHECKS
    except Exception:                                  # pragma: no cover - import guard
        return None
    return LIVE_CHECKS.get(integration_id)


def test_warning(defn: "Integration") -> str:
    if defn.test_kind == "billable":
        return ("This test makes one real request and is billed by the provider. "
                "Nothing runs until you confirm.")
    if defn.test_kind == "quota":
        return "This test consumes one request from the provider's quota."
    if defn.test_kind == "browser":
        return "Open the Map view in a browser; the key cannot be validated server-side."
    if defn.test_kind == "local":
        return "Checked locally on every page load; nothing is sent to a provider."
    if defn.test_kind == "free":
        return "Uses a free endpoint (no quota consumed)."
    return defn.notes or "No test available for this integration."


def _public_check(check: dict | None) -> dict | None:
    if not check:
        return None
    return {
        "state": check.get("state"),
        "state_label": STATE_LABELS.get(check.get("state", ""), check.get("state")),
        "ok": bool(check.get("ok")),
        "detail": redact(check.get("detail") or ""),
        "latency_ms": check.get("latency_ms"),
        "http_status": check.get("http_status"),
        "checked_at": check.get("checked_at"),
        "stale": bool(check.get("stale")),
    }


def registry() -> dict:
    """Every integration, its state, and the counts the Settings header shows."""
    items = [s for s in (status(i) for i in CATALOG) if s]
    counts = {state: 0 for state in STATE_ORDER}
    for it in items:
        counts[it["state"]] = counts.get(it["state"], 0) + 1
    return {
        "integrations": items,
        "counts": counts,
        "summary": {
            "total": len(items),
            "ready": counts[READY],
            "needs_setup": counts[NOT_CONFIGURED] + counts[CONFIGURED_UNTESTED],
            "problems": counts[AUTH_FAILURE] + counts[QUOTA_OR_BILLING]
                        + counts[PROVIDER_UNAVAILABLE],
            "unsupported": counts[UNSUPPORTED],
            "billable_enabled": sum(1 for i in items
                                    if i["billable"] and i["state"] == READY),
        },
        "note": ("A provider is 'ready' only when its last check passed inside the last "
                 "24 hours, or when it is verified locally on every page load. An "
                 "environment variable on its own is never enough."),
    }


def by_category() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for it in (s for s in (status(i) for i in CATALOG) if s):
        out.setdefault(it["category"], []).append(it)
    return out
