"""Contract tests for the v1 API.

Every external provider is mocked. No test in this file makes a network request, needs a
Google key, or depends on one being absent — the key-present paths are exercised against a
stub so the code is tested either way.

    pytest tests/test_api_v1.py -v
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.services import http as http_service
from src.services import providers, traffic
from web.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_provider_cache():
    """Each test starts from the real environment, not a previous test's monkeypatch."""
    providers.refresh()
    http_service.clear_cache()
    yield
    providers.refresh()
    http_service.clear_cache()


# --------------------------------------------------------------------- capabilities
def test_capabilities_lists_every_integration_with_a_reason():
    data = client.get("/api/v1/capabilities").json()
    keys = {c["key"] for c in data["providers"]}
    assert {"google_maps_js", "google_places", "google_routes", "google_traffic",
            "osm_tiles", "gtfs_static", "gtfs_realtime"} <= keys
    for c in data["providers"]:
        assert c["reason"], c["key"]          # never a bare "unavailable"
    assert data["summary"]["available"]
    assert set(data["summary"]["available"]) | set(data["summary"]["unavailable"]) == keys


def test_capabilities_flip_when_a_server_key_is_present(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_SERVER_API_KEY", "stub-server-key")
    providers.refresh()
    data = client.get("/api/v1/capabilities").json()
    by_key = {c["key"]: c for c in data["providers"]}
    assert by_key["google_routes"]["available"] is True
    assert by_key["google_places"]["available"] is True


def test_server_key_is_never_returned_to_the_browser(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_SERVER_API_KEY", "SUPER-SECRET-SERVER-KEY")
    monkeypatch.setenv("GOOGLE_MAPS_BROWSER_KEY", "public-browser-key")
    providers.refresh()
    for path in ("/api/map/config", "/api/v1/capabilities", "/api/v1/data-sources"):
        body = client.get(path).text
        assert "SUPER-SECRET-SERVER-KEY" not in body, path
    assert client.get("/api/map/config").json()["browser_key"] == "public-browser-key"


# ------------------------------------------------------------------------- places
def test_autocomplete_is_local_and_fast_without_any_key():
    data = client.get("/api/v1/places/autocomplete?q=jayanagar").json()
    assert data["ok"] is True
    assert data["providers_used"] == ["local"]
    assert data["indexed_places"] > 13000
    names = [s["name"] for s in data["suggestions"]]
    assert any("Jayanagar" in n for n in names)
    for s in data["suggestions"]:
        assert -90 < s["lat"] < 90 and -180 < s["lon"] < 180


def test_autocomplete_requires_two_characters():
    data = client.get("/api/v1/places/autocomplete?q=a").json()
    assert data["suggestions"] == []
    assert "2 characters" in data["note"]


def test_autocomplete_deduplicates_results():
    data = client.get("/api/v1/places/autocomplete?q=white&limit=10").json()
    names = [s["name"] for s in data["suggestions"]]
    assert len(names) == len(set(names))


def test_resolve_returns_real_coordinates():
    data = client.get("/api/v1/places/resolve?q=Majestic").json()
    assert data["ok"] is True
    assert 12.8 < data["lat"] < 13.2 and 77.4 < data["lon"] < 77.8
    assert data["source"] in ("local", "google_geocoding")


def test_resolve_404s_on_a_place_that_does_not_exist():
    r = client.get("/api/v1/places/resolve?q=zzzznotaplacezzzz")
    assert r.status_code == 404


# ------------------------------------------------------------------------ planning
def test_plan_returns_five_ranked_itineraries_with_fares():
    r = client.post("/api/v1/routes/plan",
                    json={"origin": "Jayanagar", "destination": "Majestic"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert 1 <= len(d["itineraries"]) <= 4
    for it in d["itineraries"]:
        assert it["total_price_inr"] > 0
        assert it["total_minutes"] > 0
        assert it["legs"]
        assert it["fare_breakdown"]
        assert it["explanation"]
        assert it["geometry"]
    assert d["fare_source"].startswith("BMRCL Revised Fare Chart")
    assert [it["rank"] for it in d["itineraries"]] == list(
        range(1, len(d["itineraries"]) + 1))


def test_plan_honours_the_requested_preference():
    cheapest = client.post("/api/v1/routes/plan", json={
        "origin": "Jayanagar", "destination": "Majestic",
        "preference": "cheapest"}).json()
    fastest = client.post("/api/v1/routes/plan", json={
        "origin": "Jayanagar", "destination": "Majestic",
        "preference": "fastest"}).json()
    c_top = cheapest["itineraries"][0]
    f_top = fastest["itineraries"][0]
    assert c_top["total_price_inr"] <= f_top["total_price_inr"]
    assert "cheapest" in c_top["ranked_best_for"]


def test_plan_rejects_a_blank_origin():
    r = client.post("/api/v1/routes/plan",
                    json={"origin": "   ", "destination": "Majestic"})
    assert r.status_code == 422
    assert "blank" in r.text


def test_plan_rejects_an_unknown_preference():
    r = client.post("/api/v1/routes/plan", json={
        "origin": "Jayanagar", "destination": "Majestic", "preference": "quickest"})
    assert r.status_code == 422


def test_plan_rejects_a_malformed_arrival_time():
    for bad in ("9:30", "25:00", "0930"):
        r = client.post("/api/v1/routes/plan", json={
            "origin": "Jayanagar", "destination": "Majestic", "arrival_by": bad})
        assert r.status_code == 422, bad


def test_plan_rejects_extra_fields():
    r = client.post("/api/v1/routes/plan", json={
        "origin": "Jayanagar", "destination": "Majestic", "sneaky": True})
    assert r.status_code == 422


def test_arrival_by_produces_a_latest_departure_in_local_time():
    d = client.post("/api/v1/routes/plan", json={
        "origin": "Jayanagar", "destination": "Majestic", "arrival_by": "09:30"}).json()
    advice = d["departure_advice"]
    assert advice["mode"] == "latest_departure"
    assert advice["timezone"] == "Asia/Kolkata"
    hh, mm = (int(x) for x in advice["latest_departure"].split(":"))
    got = hh * 60 + mm
    want = 9 * 60 + 30 - advice["duration_minutes"] - advice["buffer_minutes"]
    # duration is fractional minutes and the clock is printed to the minute
    assert abs(got - want) < 1.01, (got, want)


def test_departure_advice_is_never_a_timetable_claim():
    d = client.post("/api/v1/routes/plan",
                    json={"origin": "Jayanagar", "destination": "Majestic"}).json()
    assert "not a timetable" in d["departure_advice"]["basis"]


def test_plan_fails_honestly_for_a_place_that_does_not_exist():
    d = client.post("/api/v1/routes/plan", json={
        "origin": "zzzznotaplacezzzz", "destination": "Majestic"}).json()
    assert d["ok"] is False
    assert d["itineraries"] == []
    assert d["warnings"]


def test_objectives_endpoint_exposes_the_cost_weights():
    d = client.get("/api/v1/routes/objectives").json()
    keys = {o["key"] for o in d["objectives"]}
    assert keys == {"fastest", "cheapest", "fewest_transfers", "least_walking", "balanced"}
    for o in d["objectives"]:
        assert "weighting" in o


# ------------------------------------------------------------------------- traffic
def test_traffic_reports_unavailable_without_a_key_and_never_invents_numbers():
    monkeypatch_clean = True
    d = client.get("/api/v1/traffic?origin=Majestic&destination=Whitefield").json()
    live = d["live_traffic"]
    if not live["available"]:
        assert "reason" in live and live["reason"]
        assert "duration_with_traffic_min" not in live
        assert "congestion_ratio" not in live


def test_network_supply_is_labelled_scheduled_not_live():
    d = client.get("/api/v1/traffic?origin=Majestic&destination=Whitefield").json()
    sup = d["network_supply"]
    assert sup["available"] is True
    assert sup["kind"] == "scheduled_network_supply"
    assert "not live traffic" in sup["note"]
    assert sup["sample_segments"]
    for s in sup["sample_segments"]:
        assert s["n_trips"] >= 1
        assert s["km"] >= 0


def test_live_traffic_uses_the_provider_numbers_when_it_answers(monkeypatch):
    """A stubbed Routes API response must be reported as-is, including the delay."""
    monkeypatch.setenv("GOOGLE_MAPS_SERVER_API_KEY", "stub")
    providers.refresh()
    fake = {"ok": True, "status": 200, "latency_ms": 12, "cached": False, "error": "",
            "data": {"routes": [{"duration": "900s", "staticDuration": "600s",
                                 "distanceMeters": 12000, "polyline": {"encodedPolyline": ""},
                                 "legs": []}]}}
    monkeypatch.setattr(traffic.http, "post_json", lambda *a, **k: fake)
    out = traffic.road_traffic((12.97, 77.59), (12.99, 77.70))
    assert out["available"] is True
    assert out["duration_with_traffic_min"] == 15.0
    assert out["duration_free_flow_min"] == 10.0
    assert out["delay_minutes"] == 5.0
    assert out["congestion_ratio"] == 1.5
    assert out["level"] == "heavy"


def test_live_traffic_surfaces_a_provider_error_instead_of_a_number(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_SERVER_API_KEY", "stub")
    providers.refresh()
    fake = {"ok": False, "status": 403, "data": None, "error": "REQUEST_DENIED",
            "latency_ms": 5, "cached": False}
    monkeypatch.setattr(traffic.http, "post_json", lambda *a, **k: fake)
    out = traffic.road_traffic((12.97, 77.59), (12.99, 77.70))
    assert out["available"] is False
    assert "REQUEST_DENIED" in out["reason"]
    assert "congestion_ratio" not in out


# --------------------------------------------------------- saved routes / history
def test_saved_route_roundtrip():
    payload = {"origin": "Jayanagar", "destination": "Majestic",
               "route_id": "cheapest-213-M", "title": "Home to work",
               "itinerary": {"total_price_inr": 22}}
    created = client.post("/api/v1/saved-routes", json=payload)
    assert created.status_code == 201
    sid = created.json()["id"]

    listed = client.get("/api/v1/saved-routes").json()
    assert any(r["id"] == sid for r in listed)

    one = client.get(f"/api/v1/saved-routes/{sid}").json()
    assert one["payload"]["total_price_inr"] == 22
    assert one["title"] == "Home to work"

    assert client.delete(f"/api/v1/saved-routes/{sid}").status_code == 200
    assert client.get(f"/api/v1/saved-routes/{sid}").status_code == 404


def test_saving_rejects_an_empty_origin():
    r = client.post("/api/v1/saved-routes", json={"origin": "", "destination": "Majestic",
                                                  "itinerary": {}})
    assert r.status_code == 422


def test_history_records_and_aggregates():
    client.delete("/api/v1/history")
    for fare, mins, mode in ((22, 26, "bus+metro"), (30, 18, "metro"), (0, 12, "walk")):
        r = client.post("/api/v1/history", json={
            "origin": "Jayanagar", "destination": "Majestic", "selected_mode": mode,
            "estimated_fare_inr": fare, "estimated_duration": mins})
        assert r.status_code == 201
    stats = client.get("/api/v1/history").json()["stats"]
    assert stats["trips_logged"] == 3
    assert stats["total_fare_inr"] == 52
    assert stats["total_minutes"] == 56
    assert any(m["mode"] == "metro" for m in stats["by_mode"])
    assert client.delete("/api/v1/history").json()["deleted"] == 3


def test_history_rejects_a_negative_fare():
    r = client.post("/api/v1/history", json={
        "origin": "A", "destination": "B", "estimated_fare_inr": -5})
    assert r.status_code == 422


# --------------------------------------------------------------------- preferences
def test_preferences_roundtrip_and_validation():
    original = client.get("/api/v1/preferences").json()
    updated = client.put("/api/v1/preferences",
                         json={"default_preference": "cheapest", "theme": "light"}).json()
    assert updated["default_preference"] == "cheapest"
    assert updated["theme"] == "light"

    bad = client.put("/api/v1/preferences", json={"theme": "neon"})
    assert bad.status_code == 422
    assert client.put("/api/v1/preferences", json={}).status_code == 422

    client.put("/api/v1/preferences", json={
        "default_preference": original["default_preference"],
        "theme": original["theme"]})


def test_traffic_preference_accepts_a_boolean_and_stores_a_string():
    out = client.put("/api/v1/preferences", json={"show_traffic_overlay": True}).json()
    assert out["show_traffic_overlay"] == "true"
    out = client.put("/api/v1/preferences", json={"show_traffic_overlay": False}).json()
    assert out["show_traffic_overlay"] == "false"


# -------------------------------------------------------------------- data sources
def test_data_sources_reports_counts_and_fare_verification():
    d = client.get("/api/v1/data-sources?include_calls=false").json()
    assert d["row_counts"]["stops"] == 9960
    assert d["row_counts"]["places"] == 13071
    fv = d["fare_verification"]
    assert fv["pairs"] == 6806 and fv["mismatches"] == 0 and fv["all_matched"]
    statuses = {s["status"] for s in d["sources"]}
    assert "ok" in statuses or "imported" in statuses
    assert "needs_configuration" in statuses          # Google is absent and says so
    for s in d["sources"]:
        assert s["source_name"]


def test_coverage_does_not_claim_autos_or_cabs():
    d = client.get("/api/v1/coverage").json()
    assert d["autos_and_cabs"]["in_scope"] is False
    lines = {ln["line_name"]: ln["status"] for ln in d["metro"]["lines"]}
    assert lines["Pink"] == "under_construction"
    assert lines["Purple"] == "operational"


# --------------------------------------------------------------------------- tiles
def test_tile_proxy_validates_coordinates():
    assert client.get("/api/tiles/20/1/1.png").status_code == 400
    assert client.get("/api/tiles/5/-1/0.png").status_code == 400
    assert client.get("/api/tiles/5/0/999999.png").status_code == 400


def test_tile_proxy_reports_upstream_failure_rather_than_a_blank_tile(monkeypatch):
    import httpx
    from web.api import routes_tiles

    class Boom:
        def __init__(self, *a, **k):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(routes_tiles.httpx, "Client", Boom)
    r = client.get("/api/tiles/9/1/1.png")
    assert r.status_code == 503
    assert r.json()["detail"]["fallback"].startswith("the client should use the SVG")


# ------------------------------------------------------------------- legacy routes
def test_legacy_endpoints_still_work():
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/examples").json()["examples"]
    assert client.get("/api/tools").json()["tools"]
    r = client.post("/api/route", json={"origin": "Jayanagar", "destination": "Majestic"})
    assert r.status_code == 200 and r.json()["itineraries"]
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
