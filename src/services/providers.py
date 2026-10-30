"""Provider configuration and capability detection.

The application talks to three optional external providers (Google Maps JS, Google Places,
Google Routes) and one optional map tile provider (OpenStreetMap). None of them is required
for the app to work, so the question "is X available?" is answered *once*, from the
environment, and then reported honestly through the API rather than being guessed at the
call site.

Nothing here reads a key into a response. `browser_key()` is the only public one and it is
referrer-restricted by design; the server key never leaves this module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def flag(name: str, default: bool = False) -> bool:
    raw = env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Capability:
    """One optional integration and whether it is usable right now."""
    key: str
    label: str
    available: bool
    reason: str
    needs: str = ""
    docs: str = ""

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "available": self.available,
                "reason": self.reason, "needs": self.needs, "docs": self.docs}


@dataclass(frozen=True)
class Providers:
    server_key: str = ""
    browser_key: str = ""
    map_id: str = ""
    map_provider: str = "auto"
    fallback_svg: bool = True
    timeout_s: float = 8.0
    max_retries: int = 2
    caps: dict[str, Capability] = field(default_factory=dict)

    # ---- convenience -------------------------------------------------------
    @property
    def google_server(self) -> bool:
        return bool(self.server_key)

    def google_public(self) -> dict:
        """Safe to serialise to the browser. Never includes the server key."""
        return {
            "browser_key": self.browser_key,
            "map_id": self.map_id,
            "map_provider": self.map_provider,
            "maps_js": self.caps["google_maps_js"].available,
        }

    def capabilities(self) -> list[dict]:
        return [c.as_dict() for c in self.caps.values()]


def _build() -> Providers:
    server = env("GOOGLE_MAPS_SERVER_API_KEY") or env("GOOGLE_MAPS_SERVER_KEY")
    browser = env("GOOGLE_MAPS_BROWSER_KEY") or env("GOOGLE_MAPS_API_KEY")
    map_id = env("GOOGLE_MAPS_MAP_ID")
    provider = (env("MAP_PROVIDER", "auto") or "auto").lower()

    caps: dict[str, Capability] = {}

    caps["google_maps_js"] = Capability(
        key="google_maps_js", label="Google Maps JavaScript API",
        available=bool(browser),
        reason=("browser key configured" if browser else
                "no GOOGLE_MAPS_BROWSER_KEY — the app draws OpenStreetMap tiles or its "
                "own GTFS map instead"),
        needs="GOOGLE_MAPS_BROWSER_KEY (referrer-restricted)",
        docs="https://developers.google.com/maps/documentation/javascript",
    )
    caps["google_places"] = Capability(
        key="google_places", label="Google Places API",
        available=bool(server),
        reason=("server key configured" if server else
                "no server key — autocomplete falls back to the local 13,071-place index "
                "built from BMTC + BMRCL GTFS and the OSM extract"),
        needs="GOOGLE_MAPS_SERVER_API_KEY",
        docs="https://developers.google.com/maps/documentation/places/web-service",
    )
    caps["google_geocoding"] = Capability(
        key="google_geocoding", label="Google Geocoding API",
        available=bool(server),
        reason=("server key configured" if server else
                "no server key — geocoding uses the local place index and GTFS stop "
                "coordinates"),
        needs="GOOGLE_MAPS_SERVER_API_KEY",
        docs="https://developers.google.com/maps/documentation/geocoding",
    )
    caps["google_routes"] = Capability(
        key="google_routes", label="Google Routes API",
        available=bool(server),
        reason=("server key configured" if server else
                "no server key — transit routes come from the GTFS engine; road routes and "
                "live traffic-aware durations are unavailable"),
        needs="GOOGLE_MAPS_SERVER_API_KEY",
        docs="https://developers.google.com/maps/documentation/routes",
    )
    caps["google_traffic"] = Capability(
        key="google_traffic", label="Traffic-aware routing (Google)",
        available=bool(server),
        reason=("available through the Routes API" if server else
                "requires the Routes API — traffic is reported as unavailable rather than "
                "estimated"),
        needs="GOOGLE_MAPS_SERVER_API_KEY",
        docs="https://developers.google.com/maps/documentation/javascript/routes/traffic-options",
    )
    caps["osm_tiles"] = Capability(
        key="osm_tiles", label="OpenStreetMap raster tiles",
        available=True,
        reason="no key required; subject to the OSM tile usage policy",
        needs="",
        docs="https://operations.osmfoundation.org/policies/tiles/",
    )
    caps["overpass"] = Capability(
        key="overpass", label="Overpass API",
        available=flag("OVERPASS_ENABLED", True),
        reason=("enabled" if flag("OVERPASS_ENABLED", True) else
                "disabled with OVERPASS_ENABLED=0"),
        needs="OVERPASS_ENABLED=1 (public instance is rate-limited; not for production)",
        docs="https://wiki.openstreetmap.org/wiki/Overpass_API",
    )
    caps["gtfs_static"] = Capability(
        key="gtfs_static", label="GTFS static (BMTC + BMRCL)",
        available=True, reason="imported and indexed locally",
        docs="https://github.com/Vonter/bmtc-gtfs",
    )
    caps["gtfs_realtime"] = Capability(
        key="gtfs_realtime", label="GTFS-Realtime feeds",
        available=bool(env("GTFS_RT_VEHICLE_URL") or env("GTFS_RT_TRIPUPDATES_URL")),
        reason=("endpoint configured" if env("GTFS_RT_VEHICLE_URL") else
                "no authorised BMTC/BMRCL realtime endpoint is configured — live vehicle "
                "positions are not available and are never simulated"),
        needs="GTFS_RT_VEHICLE_URL / GTFS_RT_TRIPUPDATES_URL",
        docs="https://gtfs.org/documentation/realtime/",
    )
    # The LLM row reports what is actually reachable, not what an env var implies. A
    # server that is down, or the wrong model pulled, is the difference between "the model
    # answers" and "the rules engine answers", and the UI should not have to guess.
    from src.services import ollama_client
    llm = ollama_client.status(timeout=0.5)
    caps["llm"] = Capability(
        key="llm", label=f"LLM (Ollama · {llm['model']})",
        available=bool(llm["available"] and llm["model_pulled"]),
        reason=llm["reason"] or "not checked",
        needs=llm.get("needs") or "",
        docs="https://github.com/ollama/ollama/blob/main/docs/api.md",
    )
    caps["rag"] = Capability(
        key="rag", label="Policy retrieval (Chroma)",
        available=True, reason="733 documents indexed locally", docs="")

    return Providers(server_key=server, browser_key=browser, map_id=map_id,
                     map_provider=provider,
                     fallback_svg=flag("MAP_FALLBACK_SVG", True),
                     timeout_s=float(env("HTTP_TIMEOUT_SECONDS", "8") or 8),
                     max_retries=int(env("HTTP_MAX_RETRIES", "2") or 2),
                     caps=caps)


@lru_cache(maxsize=1)
def providers() -> Providers:
    return _build()


def refresh() -> Providers:
    """Re-read the environment (used by tests that monkeypatch it)."""
    providers.cache_clear()
    return _build()
