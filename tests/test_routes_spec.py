"""Contract tests for the brief §10 endpoint set (``web/api/routes_spec.py``).

These run against the FastAPI app in-process with ``TestClient``. No Google key, no
network calls, no browser: every provider is either local SQLite/GTFS or explicitly
reported as unavailable, and the tests assert *that* honesty rather than papering over it.

    pytest tests/test_routes_spec.py -v
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from web.server import app

client = TestClient(app)

CONTEXT = {"origin": "Koramangala", "destination": "RV College of Engineering",
           "transport_modes": ["bus", "metro"]}


# ---------------------------------------------------------------- config & sources

def test_public_config_exposes_no_server_key():
    data = client.get("/api/config/public").json()
    assert data["server_key_exposed"] is False
    assert "GOOGLE_MAPS_SERVER_API_KEY" not in str(data.get("browser_key", ""))


def test_data_source_status_lists_provenance():
    data = client.get("/api/data-sources/status").json()
    sources = data["sources"]
    assert sources, "the status endpoint must name the datasets in use"
    names = " ".join(str(s.get("source_name", "")) for s in sources).lower()
    assert "bmtc" in names and "gtfs" in names
    # every row carries a licence and a status, so the UI can show provenance
    for row in sources:
        assert row["status"], row["source_name"]
        assert "licence" in row


# ---------------------------------------------------------------- places

@pytest.mark.parametrize("query", ["Koramangala", "Whitefield", "Majestic"])
def test_autocomplete_returns_local_places(query):
    data = client.get("/api/locations/autocomplete", params={"q": query}).json()
    items = data["suggestions"]
    assert items, f"no local suggestions for {query}"
    assert all("name" in i and "lat" in i and "lon" in i for i in items)
    assert all(i["source"] for i in items), "a suggestion must name its source"


def test_geocode_and_reverse_geocode_round_trip():
    g = client.get("/api/locations/geocode", params={"q": "Jayanagar"}).json()
    assert g["ok"] is True
    lat, lon = g["lat"], g["lon"]
    r = client.get("/api/locations/reverse-geocode",
                   params={"lat": lat, "lng": lon}).json()
    assert r["name"]
    assert r["precision"].startswith("nearest indexed place")


def test_empty_autocomplete_query_is_not_an_error():
    r = client.get("/api/locations/autocomplete", params={"q": ""})
    assert r.status_code in (200, 422)


# ---------------------------------------------------------------- planning

def test_plan_returns_ranked_contract_routes():
    r = client.post("/api/routes/plan", json=CONTEXT)
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["routes"]

    for route in d["routes"]:
        # §11 contract keys the frontend relies on
        for key in ("id", "title", "transport_modes", "fare", "duration_minutes",
                    "distance_km", "departure_time", "arrival_time", "transfers",
                    "walking_minutes", "explanation", "steps", "geometry",
                    "traffic", "confidence"):
            assert key in route, f"{key} missing from a route"
        assert route["fare"]["status"] in ("verified", "estimated", "unavailable")
        assert route["fare"]["source"], "a fare must name its source"
        assert route["traffic"]["status"] in ("available", "unavailable",
                                              "not_applicable", "not_configured")
        assert isinstance(route["steps"], list) and route["steps"]

    # ranking is dense and ordered by the server
    ranks = sorted(r["rank"] for r in d["routes"] if "rank" in r)
    if ranks:
        assert ranks == list(range(1, len(ranks) + 1))


def test_plan_rejects_unknown_fields():
    bad = dict(CONTEXT, modes=["bus"])          # the old name, not transport_modes
    r = client.post("/api/routes/plan", json=bad)
    assert r.status_code == 422


def test_plan_rejects_a_bad_arrival_time():
    bad = dict(CONTEXT, arrival_by="25:99")
    assert client.post("/api/routes/plan", json=bad).status_code == 422


def test_plan_flags_a_place_it_had_to_guess():
    """The resolver falls back to fuzzy tokens; a wild guess must be announced."""
    bad = dict(CONTEXT, destination="Atlantis Megacity 9000")
    d = client.post("/api/routes/plan", json=bad).json()
    assert any("interpreted as" in w for w in d["warnings"]), d["warnings"]


@pytest.mark.parametrize("place", ["Hebbal", "Jayanagar", "Whitefield", "Majestic"])
def test_plan_does_not_cry_wolf_on_real_places(place):
    d = client.post("/api/routes/plan", json=dict(CONTEXT, destination=place)).json()
    assert not any("interpreted as" in w for w in d["warnings"]), (place, d["warnings"])


def test_plan_reports_a_place_it_cannot_resolve_at_all():
    d = client.post("/api/routes/plan",
                    json=dict(CONTEXT, destination="zzzqqqxxyy")).json()
    assert d["ok"] is False
    assert d["routes"] == []
    assert d["warnings"]


def test_walking_only_request_never_invents_a_fare():
    body = dict(CONTEXT, transport_modes=["walking"])
    d = client.post("/api/routes/plan", json=body).json()
    walk = [r for r in d["routes"] if r["transport_modes"] == ["walking"]]
    assert walk, "walking mode must still return a walk itinerary"
    assert walk[0]["fare"]["amount"] == 0
    assert walk[0]["duration_minutes"] > 0


def test_metro_only_empty_result_explains_the_walking_gap():
    """An empty result has to name the obstacle, not just shrug."""
    body = dict(CONTEXT, transport_modes=["metro"])
    d = client.post("/api/routes/plan", json=body).json()
    if d["ok"]:
        return                      # a pair the metro genuinely serves; nothing to assert
    joined = " ".join(d["warnings"])
    assert "metro" in joined.lower()
    assert ("walking connector" in joined or "lines do not connect" in joined
            or "within 6 km" in joined), d["warnings"]
    assert "Pink Line" in joined or "not in service" in joined          \
        or "no operational" in joined or "metro-only" in joined


def test_driving_without_a_key_is_declared_not_faked(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_SERVER_API_KEY", raising=False)
    from src.services import providers
    providers.refresh()
    body = dict(CONTEXT, transport_modes=["driving"])
    d = client.post("/api/routes/plan", json=body).json()
    drive = [r for r in d["routes"] if r["transport_modes"] == ["driving"]]
    assert drive == [], "no driving route may be produced without a routing provider"
    assert any("key" in w.lower() or "google" in w.lower() for w in d["warnings"])


def test_compare_endpoint_scores_every_objective():
    d = client.post("/api/routes/compare", json=CONTEXT).json()
    assert d["ok"] is True
    for want in ("cheapest", "fastest", "fewest_transfers", "least_walking", "balanced"):
        assert want in d["comparison"], want
        entry = d["comparison"][want]
        assert entry["fare"] >= 0 and entry["minutes"] > 0


def test_objectives_endpoint_documents_the_weights():
    d = client.get("/api/routes/objectives").json()
    weights = {o["key"]: o["weights"] for o in d["objectives"]}
    assert weights["fastest"]["minutes"] > weights["cheapest"]["minutes"]
    assert weights["cheapest"]["inr"] == pytest.approx(1.0)


# ---------------------------------------------------------------- traffic

def test_traffic_status_is_unavailable_without_a_provider(monkeypatch):
    monkeypatch.delenv("GOOGLE_MAPS_SERVER_API_KEY", raising=False)
    from src.services import providers
    providers.refresh()
    d = client.get("/api/traffic/status").json()
    assert d["available"] is False
    assert d.get("reason") or d.get("note")

    a = client.post("/api/traffic/analyze", json=CONTEXT).json()
    assert a["live_traffic"]["available"] is False
    # and it must not smuggle a made-up number in
    assert a["live_traffic"].get("delay_minutes") is None


def test_traffic_analyze_returns_gtfs_supply_not_a_fake_chart():
    d = client.post("/api/traffic/analyze", json=CONTEXT).json()
    supply = d.get("service_supply") or {}
    if supply.get("available"):
        assert supply["segments_analyzed"] >= 0
    # whatever the shape, a per-hour series must be absent or explicitly empty
    assert not d.get("hourly_traffic") or d["hourly_traffic"].get("available") is False


# ---------------------------------------------------------------- transit data

def test_nearby_stops_are_real_gtfs_stops():
    d = client.get("/api/transit/stops/nearby",
                   params={"lat": 12.9349, "lng": 77.624, "radius_km": 0.6}).json()
    stops = d.get("stops") or d.get("results") or []
    assert stops
    assert all("stop_id" in s and "distance_km" in s for s in stops)
    assert all(s["distance_km"] <= 0.6 + 1e-6 for s in stops)


def test_station_list_marks_pink_line_as_not_operational():
    d = client.get("/api/transit/stations").json()
    stations = d.get("stations") or d.get("results") or []
    assert stations
    pink = [s for s in stations if "pink" in str(s.get("line", "")).lower()]
    if pink:
        assert all(s.get("status") != "operational" for s in pink)


def test_route_catalogue_paginates():
    d = client.get("/api/transit/routes", params={"limit": 5}).json()
    routes = d.get("routes") or d.get("results") or []
    assert 0 < len(routes) <= 5


def test_route_catalogue_labels_every_route():
    d = client.get("/api/transit/routes", params={"q": "500", "limit": 5}).json()
    rows = d.get("routes") or d.get("results") or []
    assert rows, "a known BMTC route number must be findable"
    assert all(r.get("route_id") or r.get("short_name") for r in rows)


# ---------------------------------------------------------------- saved & history

def test_saved_route_lifecycle():
    body = {"origin": "Jayanagar", "destination": "Majestic",
            "route_id": "fastest-test", "title": "Jayanagar → Majestic",
            "itinerary": {"itinerary_id": "fastest-test", "total_price_inr": 22,
                          "total_minutes": 33}}
    created = client.post("/api/saved-routes", json=body)
    assert created.status_code in (200, 201)
    saved_id = created.json().get("id") or created.json().get("saved_route_id")
    assert saved_id

    rows = client.get("/api/saved-routes").json()
    items = rows["saved_routes"]
    assert any(str(i.get("id")) == str(saved_id) for i in items)

    assert client.delete(f"/api/saved-routes/{saved_id}").status_code in (200, 204)
    after = client.get("/api/saved-routes").json()
    left = after["saved_routes"]
    assert all(str(i.get("id")) != str(saved_id) for i in left)


def test_history_records_a_trip_and_aggregates():
    body = {"origin": "Jayanagar", "destination": "Majestic",
            "selected_mode": "bus", "estimated_fare_inr": 22,
            "estimated_duration": 33}
    assert client.post("/api/travel-history", json=body).status_code in (200, 201)
    d = client.get("/api/travel-history").json()
    stats = d["stats"]
    assert stats["trips_logged"] >= 1
    assert stats["total_fare_inr"] >= 22
    assert d["travel_history"]


def test_deleting_a_missing_saved_route_is_a_clean_404():
    assert client.delete("/api/saved-routes/999999999").status_code == 404


# ---------------------------------------------------------------- chat

def test_chat_answers_without_an_llm():
    r = client.post("/api/chat", json={"message": "How do I get from Jayanagar to Majestic?",
                                       "session_id": "pytest"})
    assert r.status_code == 200
    d = r.json()
    assert d["answer"].strip()
    assert d.get("engine") in ("rules", "llm", "rules+llm", None) or d.get("engine")
    # the rules engine is the fallback, so an LLM being absent is not an error
    assert "error" not in d


def test_chat_rejects_an_empty_message():
    assert client.post("/api/chat", json={"message": ""}).status_code in (400, 422)


# ---------------------------------------------------------------- shell

def test_index_ships_one_map_container_and_the_three_column_shell():
    body = client.get("/").text
    assert body.count('id="map"') == 1, "exactly one map instance may exist"
    for anchor in ('id="view-chat"', 'id="view-plan"', 'id="results-list"',
                   'id="traffic-body"', 'id="saved-list"', 'id="hist-list"',
                   'id="integrations-list"', 'id="sources-list"', 'id="toast"'):
        assert anchor in body, anchor
    # Leaflet CSS is inlined so the page needs no external stylesheet
    assert "https://unpkg.com" not in body
    assert "file://" not in body


def test_no_frontend_file_references_a_cdn():
    for path in ("web/static/js/app.js", "web/static/js/views.js",
                 "web/static/js/map.js", "web/static/js/api.js"):
        src = open(path, encoding="utf-8").read()
        for host in ("cdn.jsdelivr.net", "unpkg.com", "fonts.googleapis.com"):
            assert host not in src, f"{path} depends on {host}"


# ---------------------------------------------------------------- metro & KSRTC network
def test_metro_network_endpoint_lists_all_four_lines():
    data = client.get("/api/transit/lines").json()
    assert data["count"] == 4
    names = {l["name"]: l for l in data["lines"]}
    assert {"Purple", "Green", "Yellow", "Pink"} <= set(names)
    # Three lines carry passengers; Pink is built and tested but not yet in service.
    assert set(data["operational"]) == {"Purple", "Green", "Yellow"}
    assert data["under_construction"] == ["Pink"]
    assert names["Pink"]["status"] == "under_construction"
    assert names["Pink"]["routable"] is False
    for line in data["lines"]:
        assert line["verification"] in ("VERIFIED_PRIMARY", "VERIFIED_SECONDARY",
                                        "UNVERIFIED_DERIVED")
        assert line["station_count"] == len(line["stations"])
        for stop in line["stations"]:
            assert stop["lat"] is not None and stop["lon"] is not None


def test_ksrtc_endpoint_quotes_but_never_routes():
    data = client.get("/api/transit/ksrtc-routes").json()
    assert data["in_routing_graph"] is False
    assert data["count"] >= 4
    for c in data["corridors"]:
        # no GTFS feed for KSRTC in this build: nothing may claim a stop list or a shape
        assert c["routable"] is False
        assert c["service_class"]
        if c["fare"]["amount"] is None:
            assert c["usable_as_fare_quote"] is False
            assert c["fare"]["status"] in ("unavailable", "estimated")
            assert c["fare"]["reason"], "an absent fare must say why"
        else:
            assert c["usable_as_fare_quote"] is True
            assert c["fare"]["status"] == "verified"
            assert c["verification_status"] in ("VERIFIED_PRIMARY", "VERIFIED_SECONDARY")


def test_ksrtc_legs_are_left_unpriced_rather_than_slapped():
    """A coach priced by a city-bus slab is a wrong number, so we print no number."""
    from src.services.planner import plan
    r = plan("Majestic", "Nelamangala", modes={"bus", "metro"})
    assert r["itineraries"]
    ksrtc_legs = [l for it in r["itineraries"] for l in it["legs"]
                  if l.get("operator") == "KSRTC"]
    if not ksrtc_legs:
        pytest.skip("no KSRTC service is on the fastest path for this pair today")
    for leg in ksrtc_legs:
        assert leg["fare_inr"] is None
        assert leg["fare_basis"] == "ksrtc_fare_not_published"


def test_an_unpriced_itinerary_is_never_cheapest():
    from src.services.planner import plan
    r = plan("Majestic", "Nelamangala", modes={"bus", "metro"})
    its = r["itineraries"]
    unpriced = [i for i in its if i["total_price_inr"] is None]
    if not unpriced:
        pytest.skip("nothing unpriced on this pair today")
    for it in unpriced:
        # the amount is absent, the reason and the priced remainder are not
        assert it["priced_subtotal_inr"] is not None
        assert it["unpriced_legs"]
        assert "cheapest" not in it["ranked_best_for"]
        assert "not published" in it["explanation"]
    # Balanced may still surface a fast unpriced option, but a fare-based preference
    # must never rank an unknown amount ahead of a known one.
    from src.services.planner import plan as plan_with_pref
    cheap = plan_with_pref("Majestic", "Nelamangala", preference="cheapest",
                           modes={"bus", "metro"})
    for it in cheap["itineraries"]:
        if it["total_price_inr"] is None:
            others = [o for o in cheap["itineraries"] if o["total_price_inr"] is not None]
            assert all(o["rank"] < it["rank"] for o in others)
    # And on any itinerary, an unpriced card must never claim to be the cheapest.
    for it in unpriced:
        assert "cheapest" not in it["ranked_best_for"]


def test_intercity_block_quotes_a_corridor_and_names_the_bookends():
    data = client.post("/api/routes/plan", json={
        "origin": "Majestic", "destination": "Kanakapura",
        "transport_modes": ["bus", "metro"], "preference": "balanced"}).json()
    block = data["intercity"]
    assert block["available"] is True
    assert len(block["corridors"]) == 2
    classes = " ".join(c["service_class"] for c in block["corridors"]).lower()
    assert "express" in classes and "ordinary" in classes
    ordinary = [c for c in block["corridors"] if "ordinary" in c["service_class"].lower()][0]
    assert ordinary["fare"]["amount"] == 124.0
    assert ordinary["usable_as_fare_quote"] is True
    express = [c for c in block["corridors"] if "express" in c["service_class"].lower()][0]
    assert express["fare"]["amount"] is None, "no invented express fare"
    # quoted alongside the routed options, never merged into them
    assert all(r["transport_modes"] != ["ksrtc"] for r in data["routes"])


def test_intercity_survives_an_unresolvable_destination():
    """Mysuru is outside the place index — quote it, do not fabricate a route."""
    data = client.post("/api/routes/plan", json={
        "origin": "Majestic", "destination": "Mysuru",
        "transport_modes": ["bus"]}).json()
    assert data["routes"] == []
    assert data["intercity"]["available"] is True
    corridor = data["intercity"]["corridors"][0]
    assert corridor["to_place"] == "Mysuru"
    assert corridor["fare"]["amount"] == 162.0
    assert any("Mysuru" in w for w in data["warnings"])
    assert len(data["warnings"]) == len(set(data["warnings"]))
