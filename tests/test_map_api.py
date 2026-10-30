"""API tests for the map/route endpoints. No Google, no browser, no network.

    pytest tests/test_map_api.py -v
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from web.server import app

client = TestClient(app)


def test_index_serves_the_app_with_the_map_container():
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="map"' in body          # the map exists on load, before any query
    assert "/static/js/map.js" in body
    assert "/static/js/app.js" in body
    assert 'id="route-form"' in body


def test_static_assets_are_served():
    for path in ("/static/js/map.js", "/static/js/app.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "CampusMap" in r.text or "App" in r.text


def test_map_config_never_selects_google_without_a_key(monkeypatch):
    """With no browser key the app must not reach for Google, whatever the preference."""
    monkeypatch.delenv("GOOGLE_MAPS_BROWSER_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    from src.services import providers
    providers.refresh()
    data = client.get("/api/map/config").json()
    assert data["renderer"] != "google"
    assert data["renderer"] in ("osm", "svg")
    assert data["browser_key"] == ""
    assert data["server_key_exposed"] is False
    assert data["default_center"]["lat"] == pytest.approx(12.9716)
    assert "svg" in [data["renderer"], *data["fallback_chain"]]


def test_map_config_pins_svg_when_the_preference_says_so(monkeypatch):
    """MAP_PROVIDER=svg must keep the offline, no-network renderer available."""
    from src.services import store
    store.set_prefs({"map_provider": "svg"})
    data = client.get("/api/map/config").json()
    assert data["renderer"] == "svg"
    store.set_prefs({"map_provider": "auto"})


def test_map_config_switches_to_google_when_a_key_is_set(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_BROWSER_KEY", "test-key-123")
    from src.services import providers
    providers.refresh()
    data = client.get("/api/map/config").json()
    assert data["renderer"] == "google"
    assert data["browser_key"] == "test-key-123"
    monkeypatch.delenv("GOOGLE_MAPS_BROWSER_KEY", raising=False)
    providers.refresh()


def test_config_health_recognizes_the_legacy_google_browser_alias(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIza" + "Q" * 35)
    from src.services import config_check
    checks = {c["name"]: c for c in config_check.validate()["checks"]}
    browser = checks["Google browser key"]
    assert browser["status"] == "ok"
    assert browser["env_var"] == "GOOGLE_MAPS_API_KEY"
    alias = checks["Google key aliases"]
    assert "GOOGLE_MAPS_API_KEY is set" in alias["detail"]
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)


def test_basemap_has_all_four_metro_lines():
    data = client.get("/api/map/basemap").json()
    ids = {line["id"] for line in data["metro_lines"]}
    assert ids == {"GREEN", "PURPLE", "YELLOW", "PINK"}
    for line in data["metro_lines"]:
        assert len(line["stations"]) > 10
        for st in line["stations"]:
            assert -90 < st["lat"] < 90 and -180 < st["lng"] < 180


def test_route_rejects_blank_input():
    r = client.post("/api/route", json={"origin": "  ", "destination": "Majestic"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_route_returns_priced_itineraries_with_geometry():
    r = client.post("/api/route", json={"origin": "Jayanagar", "destination": "Majestic"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["itineraries"], "expected at least one itinerary"

    prices = [it["total_price_inr"] for it in data["itineraries"]]
    assert prices == sorted(prices), "itineraries must be price-sorted"

    for it in data["itineraries"]:
        assert it["rank"] >= 1
        assert it["mode_sequence"]
        assert it["total_price_inr"] > 0
        ride_legs = [l for l in it["legs"] if l["mode"] in ("bus", "metro")]
        assert ride_legs, "an itinerary must contain a paid leg"
        assert any(l["geometry"] for l in it["legs"]), "geometry must be present"


def test_route_geometry_is_real_stop_coordinates_not_a_straight_line():
    """The polyline must follow stops, so it needs more than 2 points for a multi-stop leg."""
    data = client.post("/api/route",
                       json={"origin": "Majestic",
                             "destination": "Whitefield"}).json()
    assert data["ok"] is True
    multi = [l for it in data["itineraries"] for l in it["legs"]
             if l["mode"] == "metro" and len(l["geometry"]) > 2]
    assert multi, "expected a metro leg with more than two coordinate pairs"


def test_route_unknown_place_fails_honestly():
    r = client.post("/api/route",
                    json={"origin": "Zzzz Nowhere Place", "destination": "Majestic"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["itineraries"] == []
    assert data["warnings"], "a failure must explain itself"
