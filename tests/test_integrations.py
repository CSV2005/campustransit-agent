"""Integration registry, secret redaction, connection tests and the road-routing chain.

No test here touches the network. Every provider call is either a local function or a
monkeypatched ``httpx.Client``, so the suite runs offline and cannot spend anyone's quota.
The billable-probe tests assert on the *refusal*, which is the behaviour that protects a
real key.

    pytest tests/test_integrations.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from src.services import config_check, http as http_service, integrations as reg
from src.services import integration_tests as itests
from src.services import road_routing, trip_modes
from src.services.redact import redact, redact_obj
from web.server import app

client = TestClient(app)

FAKE_GOOGLE_KEY = "AIza" + "Z" * 35
FAKE_ORS_KEY = "5b3ce" + "a" * 40


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    """Each test starts from a clean registry with no recorded checks."""
    for name in ("GOOGLE_MAPS_SERVER_API_KEY", "GOOGLE_MAPS_BROWSER_KEY",
                 "GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_SERVER_KEY",
                 "OPENROUTESERVICE_API_KEY", "TOMTOM_API_KEY",
                 "ROAD_ROUTING_CHAIN", "ROAD_ROUTING_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    reg.clear_checks()
    http_service.clear_cache()
    yield
    reg.clear_checks()
    http_service.clear_cache()


def _configure(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _record(integration_id: str, state: str, ok: bool, detail: str = "",
            when: str | None = None) -> None:
    reg.record_check(integration_id, state=state, ok=ok, detail=detail)
    if when:
        con = reg._con()                                     # noqa: SLF001 - test helper
        con.execute("UPDATE integration_checks SET checked_at=? WHERE integration_id=?",
                    (when, integration_id))
        con.commit()
        con.close()


# ------------------------------------------------------------------- redaction
def test_redact_removes_the_configured_value(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    text = f"request failed for https://maps.googleapis.com/x?key={FAKE_GOOGLE_KEY}"
    out = redact(text)
    assert FAKE_GOOGLE_KEY not in out
    assert "[redacted]" in out


def test_redact_catches_key_shapes_it_was_never_given():
    assert "AIza" not in redact("token=" + "AIza" + "Q" * 35)
    assert redact_obj({"nested": ["x", "AIza" + "Q" * 35]})["nested"][1] == "[redacted]"


def test_redact_obj_is_deep_and_idempotent(monkeypatch):
    _configure(monkeypatch, TOMTOM_API_KEY="a" * 32)
    payload = {"a": [{"b": f"key={'a' * 32}"}], "n": 1}
    once = redact_obj(payload)
    assert once["a"][0]["b"] != payload["a"][0]["b"]
    assert redact_obj(once) == once
    assert once["n"] == 1


def test_registry_never_returns_a_key_value(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY,
               OPENROUTESERVICE_API_KEY=FAKE_ORS_KEY)
    body = json.dumps(client.get("/api/v1/integrations").json())
    assert FAKE_GOOGLE_KEY not in body
    assert FAKE_ORS_KEY not in body
    # Names are expected; values are not.
    assert "GOOGLE_MAPS_SERVER_API_KEY" in body


# --------------------------------------------------------------------- states
def test_env_var_alone_is_never_ready(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    st = reg.status("google_places")
    assert st["configured"] is True
    assert st["state"] == reg.CONFIGURED_UNTESTED      # not "ready"
    assert "never tested" in st["reason"]


def test_successful_check_moves_to_ready(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    _record("google_places", reg.READY, True, "Autocomplete OK")
    st = reg.status("google_places")
    assert st["state"] == reg.READY
    assert st["healthy"] is True
    assert st["last_check"]["ok"] is True


@pytest.mark.parametrize("state", [reg.AUTH_FAILURE, reg.QUOTA_OR_BILLING,
                                   reg.PROVIDER_UNAVAILABLE])
def test_failed_checks_keep_their_own_state(monkeypatch, state):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    _record("google_places", state, False, "provider said no")
    st = reg.status("google_places")
    assert st["state"] == state
    assert st["healthy"] is False


def test_a_stale_success_is_not_ready(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    old = (datetime.now(timezone.utc) - timedelta(days=3)
           ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    _record("google_places", reg.READY, True, "ok", when=old)
    st = reg.status("google_places")
    assert st["state"] == reg.CONFIGURED_UNTESTED
    assert "older than" in st["reason"]


def test_disable_overrides_everything(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    _record("google_places", reg.READY, True, "ok")
    reg.set_enabled("google_places", False)
    try:
        st = reg.status("google_places")
        assert st["state"] == reg.DISABLED
        assert st["enabled"] is False
    finally:
        reg.set_enabled("google_places", True)


def test_missing_variable_names_the_variable():
    st = reg.status("tomtom")
    assert st["state"] == reg.NOT_CONFIGURED
    assert "TOMTOM_API_KEY" in st["reason"]
    assert st["setup_url"].startswith("https://")


def test_unsupported_is_distinct_from_missing():
    # Two different kinds of "no":
    #   nominatim        -- we could call it, but its policy forbids how we would use it
    #   bmtc_unofficial  -- it answers, but it is not an authorised feed
    # Neither may look like "just add a key", and neither may look like a fault.
    for blocked in ("nominatim", "bmtc_unofficial_api"):
        st = reg.status(blocked)
        assert st["state"] == reg.UNSUPPORTED, blocked
        assert st["state_label"] == "Unsupported here"

    # gtfs_realtime moved from unsupported to *supported but unconfigured* when the
    # reader was written: the capability exists, the credential does not.
    st = reg.status("gtfs_realtime")
    assert st["state"] == reg.NOT_CONFIGURED
    assert "GTFS_RT_VEHICLE_URL" in st["reason"]
    assert st["test"]["supported"] is True


def test_local_integrations_are_verified_live():
    st = reg.status("gtfs_static")
    assert st["state"] == reg.READY
    assert "bus stops" in st["reason"]


def test_every_integration_has_setup_and_purpose():
    for st in reg.registry()["integrations"]:
        assert st["purpose"], st["id"]
        assert st["state"] in reg.STATE_LABELS, st["id"]
        if st["state"] != reg.UNSUPPORTED:
            assert st["setup_url"] or not st["optional"]


# ---------------------------------------------------------------- the API shape
def test_registry_endpoint_shape():
    data = client.get("/api/v1/integrations").json()
    assert data["summary"]["total"] == len(data["integrations"])
    assert set(data["counts"]) == set(reg.STATE_ORDER)
    assert "road_routing" in data and "config" in data
    assert data["config"]["ok"] is True


def test_config_health_reports_names_not_values(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY="not-a-google-key")
    body = client.get("/api/v1/config/health").text
    assert "not-a-google-key" not in body
    data = json.loads(body)
    names = {c["name"]: c for c in data["checks"]}
    assert names["Google server key"]["status"] == "warn"


def test_unknown_integration_is_404():
    assert client.get("/api/v1/integrations/nope").status_code == 404
    assert client.post("/api/v1/integrations/nope/test").status_code == 404


def test_billable_test_refuses_without_confirmation(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    r = client.post("/api/v1/integrations/google_places/test")
    assert r.status_code == 409
    body = r.json()
    assert body["requires_confirmation"] is True
    assert "billed" in body["warning"]
    # and nothing was recorded, because nothing ran
    assert reg.last_check("google_places") is None


def test_testing_an_unconfigured_provider_says_so_without_a_probe():
    r = client.post("/api/v1/integrations/tomtom/test", json={"confirm_billable": True})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == reg.NOT_CONFIGURED
    assert "TOMTOM_API_KEY" in body["detail"]
    assert reg.last_check("tomtom") is None      # nothing was probed, nothing recorded


def test_enable_toggle_round_trip():
    r = client.put("/api/v1/integrations/tomtom/enabled", json={"enabled": False})
    assert r.status_code == 200 and r.json()["state"] == reg.DISABLED
    r = client.put("/api/v1/integrations/tomtom/enabled", json={"enabled": True})
    assert r.json()["state"] == reg.NOT_CONFIGURED


# ------------------------------------------------------------------ probes
class _Resp:
    def __init__(self, status: int, payload: dict | str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def fake_http(monkeypatch):
    """Swap httpx.Client for a recorder with a queued response."""
    queue: list[_Resp] = []

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **k):
            return queue.pop(0)

        def post(self, url, **k):
            return queue.pop(0)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return queue


@pytest.mark.parametrize("payload,expected", [
    ({"status": "OK", "predictions": [{"description": "Majestic"}]}, reg.READY),
    ({"status": "ZERO_RESULTS", "predictions": []}, reg.READY),
    ({"status": "REQUEST_DENIED", "error_message": "The provided API key is invalid."},
     reg.AUTH_FAILURE),
    ({"status": "OVER_QUERY_LIMIT", "error_message": "quota"},
     reg.QUOTA_OR_BILLING),
])
def test_google_places_probe_maps_provider_status(monkeypatch, fake_http, payload, expected):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    fake_http.append(_Resp(200, payload))
    out = itests.run_test("google_places", confirm_billable=True)
    assert out["state"] == expected
    assert reg.last_check("google_places")["state"] == expected


def test_google_probe_handles_a_non_json_body(monkeypatch, fake_http):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    fake_http.append(_Resp(502, "<html>bad gateway</html>"))
    out = itests.run_test("google_geocoding", confirm_billable=True)
    assert out["ok"] is False
    assert out["state"] == reg.PROVIDER_UNAVAILABLE


def test_transport_error_is_provider_unavailable(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise httpx.ConnectError("dns died")

        def post(self, *a, **k):
            raise httpx.ConnectError("dns died")

    monkeypatch.setattr(httpx, "Client", _Boom)
    out = itests.run_test("google_routes", confirm_billable=True)
    assert out["state"] == reg.PROVIDER_UNAVAILABLE
    assert "transport error" in out["detail"]


def test_routes_probe_reports_ready_with_a_route(monkeypatch, fake_http):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    fake_http.append(_Resp(200, {"routes": [{"distanceMeters": 3120,
                                             "duration": "600s"}]}))
    out = itests.run_test("google_routes", confirm_billable=True)
    assert out["state"] == reg.READY
    assert "3.12 km" in out["detail"]


def test_tomtom_auth_failure_is_classified(monkeypatch, fake_http):
    _configure(monkeypatch, TOMTOM_API_KEY="b" * 32)
    fake_http.append(_Resp(403, {"detailedError": {"message": "Invalid API key"}}))
    out = itests.run_test("tomtom", confirm_billable=True)
    assert out["state"] == reg.AUTH_FAILURE


def test_ors_health_down_short_circuits_before_using_quota(monkeypatch, fake_http):
    _configure(monkeypatch, OPENROUTESERVICE_API_KEY=FAKE_ORS_KEY)
    fake_http.append(_Resp(503, {"status": "not ready"}))
    out = itests.run_test("openrouteservice", confirm_billable=True)
    assert out["state"] == reg.PROVIDER_UNAVAILABLE
    assert fake_http == []                       # the quota-consuming call never happened


# ------------------------------------------------------------- geocoding
def test_forward_geocode_surfaces_ambiguity(monkeypatch, fake_http):
    from src.services import places
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    fake_http.append(_Resp(200, {"status": "OK", "results": [
        {"formatted_address": "Dayananda Sagar University, Harohalli, Karnataka",
         "geometry": {"location": {"lat": 12.663, "lng": 77.450}},
         "place_id": "A", "types": ["university"]},
        {"formatted_address": "Dayananda Sagar College of Engineering, Bengaluru",
         "geometry": {"location": {"lat": 12.917, "lng": 77.564}},
         "place_id": "B", "types": ["university"]},
    ]}))
    out = places.geocode("Dayananda Sagar University")
    assert out["ok"] is True
    assert out["ambiguous"] is True
    assert len(out["candidates"]) == 2
    assert out["lat"] == pytest.approx(12.663)
    assert "distinct places" in out["ambiguity_note"]


def test_reverse_geocode_uses_google_when_configured(monkeypatch, fake_http):
    from src.services import places
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    fake_http.append(_Resp(200, {"status": "OK", "results": [
        {"formatted_address": "12, MG Road, Bengaluru, Karnataka 560001",
         "geometry": {"location": {"lat": 12.9757, "lng": 77.6064}}}]}))
    out = places.reverse_geocode(12.9757, 77.6064)
    assert out["ok"] is True
    assert out["source"] == "google_geocoding"
    assert "MG Road" in out["name"]


def test_reverse_geocode_is_labelled_when_it_is_only_the_local_index():
    from src.services import places
    out = places.reverse_geocode(12.9767, 77.5713)
    assert out["ok"] is True
    assert out["source"] == "local index"
    assert "not a street address" in out["precision"]


# ------------------------------------------------------------- road routing chain
def _google_ok(monkeypatch):
    """Make the Google branch return a route without any network call."""
    monkeypatch.setattr(road_routing, "_google",
                        lambda *a, **k: [{"distance_km": 12.4, "duration_min": 31.0,
                                          "duration_free_flow_min": 24.0, "delay_min": 7.0,
                                          "geometry": [[12.9, 77.5], [12.8, 77.6]],
                                          "geometry_kind": "polyline", "summary": "",
                                          "traffic_aware": True}])


def _ors_ok(monkeypatch):
    monkeypatch.setattr(road_routing, "_ors",
                        lambda *a, **k: [{"distance_km": 13.0, "duration_min": 29.0,
                                          "duration_free_flow_min": None, "delay_min": None,
                                          "geometry": [[12.9, 77.5], [12.8, 77.6]],
                                          "geometry_kind": "polyline", "summary": "",
                                          "traffic_aware": False}])


def test_chain_prefers_the_first_configured_provider(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY,
               OPENROUTESERVICE_API_KEY=FAKE_ORS_KEY)
    _google_ok(monkeypatch)
    _ors_ok(monkeypatch)
    out = road_routing.plan((12.9, 77.5), (12.8, 77.6))
    assert out["provider"] == "google_routes"
    assert out["traffic_aware"] is True


def test_chain_falls_through_to_the_next_provider(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY,
               OPENROUTESERVICE_API_KEY=FAKE_ORS_KEY)
    monkeypatch.setattr(road_routing, "_google",
                        lambda *a, **k: (_ for _ in ()).throw(
                            road_routing.RoadRoutingError("HTTP 500")))
    _ors_ok(monkeypatch)
    out = road_routing.plan((12.9, 77.5), (12.8, 77.6))
    assert out["provider"] == "openrouteservice"
    # No live traffic from ORS, and the result says so rather than inheriting Google's flag.
    assert out["traffic_aware"] is False
    assert "not live traffic" in out["note"]
    assert [a["provider"] for a in out["attempts"]] == ["google_routes", "openrouteservice"]


def test_auth_failed_provider_is_skipped_not_retried(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY,
               OPENROUTESERVICE_API_KEY=FAKE_ORS_KEY)
    _record("google_routes", reg.AUTH_FAILURE, False, "API key not valid")
    _ors_ok(monkeypatch)
    out = road_routing.plan((12.9, 77.5), (12.8, 77.6))
    assert out["provider"] == "openrouteservice"
    assert any(s["provider"] == "google_routes" for s in out["skipped"])


def test_pinning_disables_fallback(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY,
               OPENROUTESERVICE_API_KEY=FAKE_ORS_KEY,
               ROAD_ROUTING_PROVIDER="openrouteservice")
    monkeypatch.setattr(road_routing, "_google",
                        lambda *a, **k: pytest.fail("Google must not be called when pinned"))
    _ors_ok(monkeypatch)
    out = road_routing.plan((12.9, 77.5), (12.8, 77.6))
    assert out["provider"] == "openrouteservice"


def test_no_provider_is_unavailable_not_a_straight_line():
    out = road_routing.plan((12.9, 77.5), (12.8, 77.6))
    assert out["ok"] is False
    assert out["available"] is False
    assert out["routes"] == []
    assert "does not estimate" in out["reason"]


def test_straight_line_only_when_explicitly_allowed():
    out = road_routing.plan((12.9, 77.5), (12.8, 77.6), allow_estimate=True)
    assert out["provider"] == "straight_line"
    assert out["is_estimate"] is True


def test_drive_trip_names_its_provider(monkeypatch):
    _configure(monkeypatch, GOOGLE_MAPS_SERVER_API_KEY=FAKE_GOOGLE_KEY)
    _google_ok(monkeypatch)
    out = trip_modes.drive_trip({"lat": 12.9, "lon": 77.5, "name": "A"},
                                {"lat": 12.8, "lon": 77.6, "name": "B"})
    assert out["ok"] is True
    assert out["provider"] == "google_routes"
    assert out["itineraries"][0]["provider_label"] == "Google Routes"
    assert out["itineraries"][0]["durations"]["with_traffic_min"] == 31.0
    # still no invented fare
    assert out["itineraries"][0]["total_price_inr"] is None


def test_drive_trip_unavailable_without_providers():
    out = trip_modes.drive_trip({"lat": 12.9, "lon": 77.5, "name": "A"},
                                {"lat": 12.8, "lon": 77.6, "name": "B"})
    assert out["ok"] is False
    assert "does not estimate" in out["reason"]


def test_polyline_decoder_round_trip():
    # The canonical example from Google's own documentation.
    pts = road_routing.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert len(pts) == 3
    # Google's documented example: (38.5,-120.2), (40.7,-120.95), (43.252,-126.453)
    assert pts[0][0] == pytest.approx(38.5, abs=0.01)
    assert pts[1][1] == pytest.approx(-120.95, abs=0.05)
    assert pts[-1][0] == pytest.approx(43.252, abs=0.01)


# --------------------------------------------------------------- chat budget
def test_deadline_produces_a_rules_answer_and_a_reason(monkeypatch):
    from src.agent import CampusTransitAgent

    agent = CampusTransitAgent(force_engine="llm")
    monkeypatch.setattr(agent, "_chat_llm", lambda *a, **k: (_ for _ in ()).throw(
        __import__("src.agent", fromlist=["DeadlineExceeded"]).DeadlineExceeded("150s used")))
    out = agent.chat("Best route from Majestic to Whitefield?")
    assert out["engine"] == "rules"
    assert out["fallback_reason"].startswith("deadline:")
    assert out["answer"], "the traveller must still get an answer"


def test_each_model_round_gets_only_the_remaining_budget(monkeypatch):
    """A 6-round loop must not multiply the per-call timeout by 6."""
    from src import agent as agent_mod

    calls: list[float] = []

    class _Reply:
        content = ""
        tool_calls: list = []

        def as_message(self):
            return {"role": "assistant", "content": ""}

    def fake_chat(messages, tools=None, timeout=None, **k):
        calls.append(timeout)
        raise agent_mod.ollama_client.OllamaTimeout("stop here")

    monkeypatch.setattr(agent_mod.ollama_client, "chat", fake_chat)
    a = agent_mod.CampusTransitAgent(force_engine="llm")
    a.chat("anything", budget_s=30)
    assert calls and calls[0] <= 30


def test_chat_endpoint_advertises_a_client_timeout_longer_than_its_budget():
    data = client.get("/api/chat/health").json()
    assert data["timeout_hint_ms"] > data["budget_s"] * 1000
