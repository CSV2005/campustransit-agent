"""Startup configuration validation.

Runs once when the server boots and is available afterwards as
``GET /api/v1/config/health``. It answers one question honestly: *is this process
configured in a way that can work?* It checks shapes, conflicts and missing values -- it
does not call any billable endpoint, and it never echoes a secret.

Nothing here is fatal. A missing Google key is a normal way to run this app; a key with the
wrong shape is worth a loud warning because it will fail later at the worst moment.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
ENVS = ("development", "test", "production")
GOOGLE_KEY = re.compile(r"^AIza[0-9A-Za-z_\-]{30,}$")
ORS_KEY = re.compile(r"^[0-9a-zA-Z]{30,}$")
MAP_PROVIDERS = ("auto", "google", "osm", "svg")


def _v(name: str) -> str:
    return (os.getenv(name) or "").strip()


def validate() -> dict:
    checks: list[dict] = []

    def add(name: str, status: str, detail: str, env_var: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "env_var": env_var})

    # ---------------------------------------------------------------- environment
    app_env = (_v("APP_ENV") or "development").lower()
    if app_env not in ENVS:
        add("APP_ENV", "warn", f"unknown value '{app_env}'; expected one of {', '.join(ENVS)}",
            "APP_ENV")
    else:
        add("APP_ENV", "ok", f"running as {app_env}", "APP_ENV")

    env_file = ROOT / ".env"
    gitignore = ROOT / ".gitignore"
    if env_file.exists():
        ignored = gitignore.exists() and any(
            line.strip() in (".env", "/.env", "*.env")
            for line in gitignore.read_text(encoding="utf-8", errors="ignore").splitlines())
        add(".env", "ok" if ignored else "error",
            "present and git-ignored" if ignored else
            ".env exists but is NOT in .gitignore - add it before committing", ".env")
    else:
        add(".env", "ok", "no .env file; using the process environment", ".env")

    # ------------------------------------------------------------------ credentials
    google_server = _v("GOOGLE_MAPS_SERVER_API_KEY") or _v("GOOGLE_MAPS_SERVER_KEY")
    google_browser = _v("GOOGLE_MAPS_BROWSER_KEY") or _v("GOOGLE_MAPS_API_KEY")
    browser_key_explicit = _v("GOOGLE_MAPS_BROWSER_KEY")
    legacy = _v("GOOGLE_MAPS_API_KEY")

    if google_server:
        add("Google server key", "ok" if GOOGLE_KEY.match(google_server) else "warn",
            "present (value never logged)" if GOOGLE_KEY.match(google_server) else
            "present, but the shape is not a Google API key (expected 'AIza...')",
            "GOOGLE_MAPS_SERVER_API_KEY")
    else:
        add("Google server key", "ok", "not set - Places, Geocoding and Routes are off; "
            "the app uses the local index and GTFS", "GOOGLE_MAPS_SERVER_API_KEY")

    if google_browser:
        env_name = "GOOGLE_MAPS_BROWSER_KEY" if browser_key_explicit else "GOOGLE_MAPS_API_KEY"
        add("Google browser key", "ok" if GOOGLE_KEY.match(google_browser) else "warn",
            "present; must be HTTP-referrer restricted in the Google Cloud console"
            if GOOGLE_KEY.match(google_browser) else "present, shape is not a Google key",
            env_name)
    else:
        add("Google browser key", "ok", "not set - the map renders with Leaflet/OSM or the "
            "built-in GTFS map", "GOOGLE_MAPS_BROWSER_KEY")

    if legacy:
        if browser_key_explicit:
            add("Google key aliases", "warn",
                "Both GOOGLE_MAPS_BROWSER_KEY and GOOGLE_MAPS_API_KEY are set; the explicit "
                "browser key wins and GOOGLE_MAPS_API_KEY is ignored.",
                "GOOGLE_MAPS_API_KEY")
        else:
            add("Google key aliases", "warn",
                "GOOGLE_MAPS_API_KEY is set and treated as the legacy browser-key alias. "
                "Prefer GOOGLE_MAPS_BROWSER_KEY for clarity and to avoid confusing it with "
                "the server key.",
                "GOOGLE_MAPS_API_KEY")

    ors = _v("OPENROUTESERVICE_API_KEY")
    add("OpenRouteService key", "ok" if not ors else
        ("ok" if ORS_KEY.match(ors) else "warn"),
        "not set - ORS is not in the routing chain" if not ors else
        ("present (value never logged)" if ORS_KEY.match(ors) else
         "present but shorter than any issued ORS token"), "OPENROUTESERVICE_API_KEY")

    tomtom = _v("TOMTOM_API_KEY")
    add("TomTom key", "ok", "not set - TomTom is not in the routing chain" if not tomtom
        else "present (value never logged)", "TOMTOM_API_KEY")

    # --------------------------------------------------------------------- choices
    provider = (_v("MAP_PROVIDER") or "auto").lower()
    add("MAP_PROVIDER", "ok" if provider in MAP_PROVIDERS else "warn",
        f"{provider}" + ("" if provider in MAP_PROVIDERS else
                         f" is not one of {', '.join(MAP_PROVIDERS)}"), "MAP_PROVIDER")

    chain = (_v("ROAD_ROUTING_CHAIN") or "google_routes,openrouteservice,tomtom")
    known = {"google_routes", "openrouteservice", "tomtom", "google", "ors"}
    unknown = [p.strip() for p in chain.split(",") if p.strip().lower() not in known]
    add("ROAD_ROUTING_CHAIN", "ok" if not unknown else "warn",
        chain if not unknown else f"unknown provider(s): {', '.join(unknown)}",
        "ROAD_ROUTING_CHAIN")

    pinned = _v("ROAD_ROUTING_PROVIDER")
    if pinned:
        add("ROAD_ROUTING_PROVIDER", "ok" if pinned.lower() in known else "warn",
            f"pinned to {pinned}; fallback to other providers is disabled",
            "ROAD_ROUTING_PROVIDER")

    # -------------------------------------------------------------- realtime feed
    # A URL is not evidence of a feed, so this section only checks the *shape* of the
    # settings. Whether buses actually appear is decided by reading the feed once, and
    # that answer is shown on the map and in Settings rather than here.
    rt_url = _v("GTFS_RT_VEHICLE_URL")
    if rt_url:
        bad_scheme = not rt_url.lower().startswith(("http://", "https://"))
        insecure = rt_url.lower().startswith("http://") and not rt_url.startswith(
            ("http://127.0.0.1", "http://localhost"))
        add("GTFS_RT_VEHICLE_URL", "warn" if (bad_scheme or insecure) else "ok",
            "set; the feed is read over HTTPS" if not (bad_scheme or insecure) else
            ("not an http(s) URL" if bad_scheme else
             "plain http to a remote host - the feed and any token travel in clear text"),
            "GTFS_RT_VEHICLE_URL")
    else:
        add("GTFS_RT_VEHICLE_URL", "ok",
            "not set - live vehicle positions are unavailable, and the map says so "
            "instead of drawing buses", "GTFS_RT_VEHICLE_URL")

    try:
        interval = float(_v("GTFS_RT_POLL_INTERVAL_S") or 20)
        add("GTFS_RT_POLL_INTERVAL_S", "ok" if interval >= 15 else "warn",
            f"{interval:.0f}s (raised to 15s if lower: providers are not a free tap)",
            "GTFS_RT_POLL_INTERVAL_S")
    except ValueError:
        add("GTFS_RT_POLL_INTERVAL_S", "warn", "not a number; falling back to 20",
            "GTFS_RT_POLL_INTERVAL_S")

    auth = _v("GTFS_RT_AUTH_HEADER")
    if auth:
        shaped = ":" in auth and "changeme" not in auth.lower() \
            and "your-" not in auth.lower() and "<" not in auth
        add("GTFS_RT_AUTH_HEADER", "ok" if shaped else "warn",
            "present (value never logged)" if shaped else
            "looks like a placeholder or has no 'Name: value' form",
            "GTFS_RT_AUTH_HEADER")

    # ------------------------------------------------------------------- LLM path
    enabled = (_v("LLM_ENABLED") or "auto").lower()
    add("LLM_ENABLED", "ok" if enabled in ("auto", "on", "off", "true", "false", "1", "0")
        else "warn", f"{enabled}", "LLM_ENABLED")

    try:
        timeout = float(_v("OLLAMA_TIMEOUT_S") or 120)
        add("OLLAMA_TIMEOUT_S", "ok" if timeout >= 10 else "warn",
            f"{timeout:.0f}s per model call", "OLLAMA_TIMEOUT_S")
    except ValueError:
        add("OLLAMA_TIMEOUT_S", "warn", "not a number; falling back to 120",
            "OLLAMA_TIMEOUT_S")

    try:
        budget = float(_v("CHAT_BUDGET_S") or 150)
        add("CHAT_BUDGET_S", "ok" if budget >= 30 else "warn",
            f"{budget:.0f}s wall-clock per chat turn", "CHAT_BUDGET_S")
    except ValueError:
        add("CHAT_BUDGET_S", "warn", "not a number; falling back to 150", "CHAT_BUDGET_S")

    errors = sum(1 for c in checks if c["status"] == "error")
    warns = sum(1 for c in checks if c["status"] == "warn")
    return {
        "app_env": app_env,
        "checks": checks,
        "errors": errors,
        "warnings": warns,
        "ok": errors == 0,
        "summary": (f"{len(checks)} checks, {errors} error(s), {warns} warning(s)"),
        "note": "No secret value appears in this response; only names and shapes.",
    }


def log_summary(logger) -> dict:
    """Write the summary to the server log at startup. Values are never included."""
    result = validate()
    for c in result["checks"]:
        if c["status"] == "error":
            logger.error("config: %s - %s", c["name"], c["detail"])
        elif c["status"] == "warn":
            logger.warning("config: %s - %s", c["name"], c["detail"])
    logger.info("config: %s (APP_ENV=%s)", result["summary"], result["app_env"])
    return result
