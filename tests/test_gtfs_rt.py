"""GTFS-Realtime reader and the live-vehicle endpoints.

Everything here runs offline. The protobuf fixtures are built with the real
``gtfs-realtime-bindings`` encoder and then fed through the real decoder, so the test
exercises the same parse path a live feed would -- without pretending a live feed exists.

What is deliberately covered:

* bytes -> FeedMessage -> normalised vehicle, including a missing ``position`` entity
  (allowed by the spec) and a missing ``bearing``/``speed``/``timestamp``;
* a vehicle whose timestamp is old is returned as delayed, not deleted and not refreshed;
* a configured-but-broken feed answers ``502``; an unconfigured one answers ``200`` with
  ``available: false`` -- "no source" is not an upstream error;
* one upstream call per polling interval, even under concurrent readers;
* a token in ``GTFS_RT_AUTH_HEADER`` never appears in any response.

    pytest tests/test_gtfs_rt.py -v
"""
from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from src.services import gtfs_rt
from web.server import app

client = TestClient(app)

pb = pytest.importorskip("google.transit.gtfs_realtime_pb2")


# ------------------------------------------------------------------------- fixtures
def build_feed(*vehicles, header_ts: int = 0) -> bytes:
    """A real FeedMessage serialised exactly as a transit agency would publish it."""
    msg = pb.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.incrementality = pb.FeedHeader.FULL_DATASET
    if header_ts:
        msg.header.timestamp = header_ts
    for i, v in enumerate(vehicles, start=1):
        e = msg.entity.add()
        e.id = v.get("entity_id", f"e{i}")
        vp = e.vehicle
        vp.vehicle.id = v.get("vehicle_id", f"KA01-{i}")
        vp.vehicle.label = v.get("label", f"500-{i}")
        if v.get("route_id"):
            vp.trip.route_id = v["route_id"]
        if v.get("trip_id"):
            vp.trip.trip_id = v["trip_id"]
        if v.get("lat") is not None:
            vp.position.latitude = v["lat"]
            vp.position.longitude = v["lon"]
            if v.get("bearing") is not None:
                vp.position.bearing = v["bearing"]
            if v.get("speed") is not None:
                vp.position.speed = v["speed"]
        if v.get("timestamp") is not None:
            vp.timestamp = v["timestamp"]
        if v.get("occupancy") is not None:
            vp.occupancy_status = v["occupancy"]
    return msg.SerializeToString()


class FakeResponse:
    def __init__(self, body: bytes, status_code: int = 200, text: str = ""):
        self.content = body
        self.status_code = status_code
        self.text = text or (body[:200].decode("utf-8", "replace") if body else "")


def fake_http(monkeypatch, response, calls: list | None = None):
    """Patch httpx.Client so no socket is opened; record the URLs that were requested."""
    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None, **kw):
            if calls is not None:
                calls.append({"url": url, "headers": headers or {}})
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(gtfs_rt.httpx, "Client", FakeClient)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gtfs_rt.clear_cache()
    for name in ("GTFS_RT_VEHICLE_URL", "GTFS_RT_TRIPUPDATES_URL", "GTFS_RT_AUTH_HEADER",
                 "GTFS_RT_POLL_INTERVAL_S", "GTFS_RT_STALE_AFTER_S"):
        monkeypatch.delenv(name, raising=False)


# ------------------------------------------------------------------------ decoding
def test_protobuf_feed_decodes_to_normalised_vehicles(monkeypatch):
    now = int(time.time())
    body = build_feed(
        {"vehicle_id": "KA01F1234", "label": "500-D", "lat": 12.9716, "lon": 77.5946,
         "bearing": 95.5, "speed": 11.1, "route_id": "500D", "timestamp": now},
        {"vehicle_id": "KA01F5678", "label": "KBS-1", "lat": 12.95, "lon": 77.58,
         "timestamp": now - 5},
        header_ts=now,
    )
    fake_http(monkeypatch, FakeResponse(body))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")

    out = gtfs_rt.positions()
    assert out["available"] is True
    assert out["feed_format"] == "protobuf"
    assert out["count"] == 2
    first = out["vehicles"][0]
    assert first["vehicle_id"] == "KA01F1234"
    assert first["label"] == "500-D"
    assert first["route_id"] == "500D"
    assert first["lat"] == pytest.approx(12.9716)
    assert first["bearing"] == pytest.approx(95.5)
    assert first["speed_kmh"] == pytest.approx(40.0, abs=0.05)   # m/s -> km/h, once
    assert first["is_live"] is True
    assert out["live_count"] == 2
    assert out["stale_count"] == 0


def test_entity_without_position_is_skipped_not_invented(monkeypatch):
    """The spec allows a vehicle entity with no ``position``; it must not become a 0,0 dot."""
    now = int(time.time())
    body = build_feed(
        {"vehicle_id": "NO-POS", "lat": None, "timestamp": now},        # no position block
        {"vehicle_id": "AT-NULL", "lat": 0.0, "lon": 0.0, "timestamp": now},
        {"vehicle_id": "REAL", "lat": 12.9, "lon": 77.6, "timestamp": now},
    )
    fake_http(monkeypatch, FakeResponse(body))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")

    out = gtfs_rt.positions()
    assert [v["vehicle_id"] for v in out["vehicles"]] == ["REAL"]
    assert out["total_in_feed"] == 1


def test_missing_optional_fields_stay_null(monkeypatch):
    now = int(time.time())
    body = build_feed({"vehicle_id": "SPARSE", "lat": 12.9, "lon": 77.6, "timestamp": now})
    fake_http(monkeypatch, FakeResponse(body))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    v = gtfs_rt.positions()["vehicles"][0]
    # Absent in the feed -> absent in the output. Never defaulted to 0, which reads as a
    # real bearing/speed to every consumer downstream.
    assert v["bearing"] is None
    assert v["speed_kmh"] is None
    assert v["route_id"] is None
    assert v["current_stop_sequence"] is None


def test_stale_position_is_delayed_not_live(monkeypatch):
    now = int(time.time())
    body = build_feed({"vehicle_id": "OLD", "lat": 12.9, "lon": 77.6,
                       "timestamp": now - 3600})
    fake_http(monkeypatch, FakeResponse(body))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    monkeypatch.setenv("GTFS_RT_STALE_AFTER_S", "120")

    out = gtfs_rt.positions()
    v = out["vehicles"][0]
    assert v["is_live"] is False
    assert v["age_s"] >= 3500
    assert "old" in v["stale_reason"]
    assert out["live_count"] == 0 and out["stale_count"] == 1


def test_vehicle_without_timestamp_is_never_live(monkeypatch):
    body = build_feed({"vehicle_id": "NO-TS", "lat": 12.9, "lon": 77.6})
    fake_http(monkeypatch, FakeResponse(body))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    v = gtfs_rt.positions()["vehicles"][0]
    assert v["timestamp"] is None and v["age_s"] is None
    assert v["is_live"] is False           # cannot be shown as live without evidence


def test_json_feed_also_decodes(monkeypatch):
    now = int(time.time())
    payload = {"header": {"gtfs_realtime_version": "2.0", "timestamp": now},
               "entity": [{"id": "1", "vehicle": {
                   "vehicle": {"id": "J1", "label": "V-500"},
                   "trip": {"route_id": "500"},
                   "position": {"latitude": 12.94, "longitude": 77.57},
                   "timestamp": now}}]}
    fake_http(monkeypatch, FakeResponse(json.dumps(payload).encode()))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp")
    out = gtfs_rt.positions()
    assert out["feed_format"] == "json"
    assert out["vehicles"][0]["label"] == "V-500"


def test_garbage_body_is_a_decode_error(monkeypatch):
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    fake_http(monkeypatch, FakeResponse(b"<html>503 Service Unavailable</html>"))
    with pytest.raises(gtfs_rt.DecodeError):
        gtfs_rt.decode_feed(b"<html>503 Service Unavailable</html>")


# ------------------------------------------------------------------- HTTP contract
def test_unconfigured_returns_unavailable_and_names_the_variable():
    r = client.get("/api/transit/vehicle-positions")
    assert r.status_code == 200                     # not an error: there is no source
    body = r.json()
    assert body["available"] is False
    assert body["kind"] == "not_configured"
    assert "GTFS_RT_VEHICLE_URL" in body["needs"]
    assert body["vehicles"] == []
    # The wording must not imply buses are being tracked.
    assert "no authorised" in body["reason"].lower()


def test_broken_feed_is_502_not_an_empty_success(monkeypatch):
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    fake_http(monkeypatch, FakeResponse(b"not protobuf", status_code=200))
    r = client.get("/api/transit/vehicle-positions")
    assert r.status_code == 502
    body = r.json()
    assert body["available"] is False
    assert body["kind"] == "provider_unavailable"


def test_provider_500_is_502(monkeypatch):
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    fake_http(monkeypatch, FakeResponse(b"boom", status_code=503, text="upstream down"))
    r = client.get("/api/transit/vehicle-positions")
    assert r.status_code == 502
    assert r.json()["http_status"] == 503


def test_401_is_reported_as_an_auth_failure(monkeypatch):
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    fake_http(monkeypatch, FakeResponse(b"unauthorized", status_code=401))
    body = client.get("/api/transit/vehicle-positions").json()
    assert body["kind"] == "auth_failure"


def test_transport_error_does_not_raise(monkeypatch):
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    fake_http(monkeypatch, httpx.ConnectError("dns failure"))
    r = client.get("/api/transit/vehicle-positions")
    assert r.status_code == 502
    assert "ConnectError" in r.json()["reason"]


def test_successful_feed_is_served_with_counts(monkeypatch):
    now = int(time.time())
    fake_http(monkeypatch, FakeResponse(build_feed(
        {"vehicle_id": "A", "lat": 12.9, "lon": 77.6, "timestamp": now, "route_id": "500D"},
        {"vehicle_id": "B", "lat": 12.91, "lon": 77.61, "timestamp": now},
        header_ts=now)))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    body = client.get("/api/transit/vehicle-positions").json()
    assert body["available"] is True and body["count"] == 2
    assert body["live_count"] == 2
    assert body["stale_after_s"] >= 30
    assert body["refresh_after_s"] >= 15


def test_route_filter_matches_route_id_or_label(monkeypatch):
    now = int(time.time())
    fake_http(monkeypatch, FakeResponse(build_feed(
        {"vehicle_id": "A", "lat": 12.9, "lon": 77.6, "timestamp": now,
         "route_id": "500D", "label": "500-D"},
        {"vehicle_id": "B", "lat": 12.91, "lon": 77.61, "timestamp": now,
         "route_id": "335", "label": "335-E"})))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    body = client.get("/api/transit/vehicle-positions?route=500D").json()
    assert [v["vehicle_id"] for v in body["vehicles"]] == ["A"]


def test_bbox_filters_and_validates(monkeypatch):
    now = int(time.time())
    fake_http(monkeypatch, FakeResponse(build_feed(
        {"vehicle_id": "IN", "lat": 12.97, "lon": 77.59, "timestamp": now},
        {"vehicle_id": "OUT", "lat": 13.34, "lon": 74.74, "timestamp": now})))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    body = client.get("/api/transit/vehicle-positions?bbox=12.8,77.4,13.1,77.8").json()
    assert [v["vehicle_id"] for v in body["vehicles"]] == ["IN"]
    assert client.get("/api/transit/vehicle-positions?bbox=1,2,3").status_code == 422
    assert client.get("/api/transit/vehicle-positions?bbox=13,77,12,78").status_code == 422
    assert client.get("/api/transit/vehicle-positions?bbox=a,b,c,d").status_code == 422


# ------------------------------------------------------------------- behaviour
def test_one_upstream_call_per_polling_interval(monkeypatch):
    now = int(time.time())
    calls: list = []
    fake_http(monkeypatch, FakeResponse(build_feed(
        {"vehicle_id": "A", "lat": 12.9, "lon": 77.6, "timestamp": now})), calls)
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    monkeypatch.setenv("GTFS_RT_POLL_INTERVAL_S", "60")

    for _ in range(5):
        assert client.get("/api/transit/vehicle-positions").status_code == 200
    assert len(calls) == 1
    assert client.get("/api/transit/vehicle-positions").json()["cached"] is True


def test_poll_interval_floor_is_fifteen_seconds(monkeypatch):
    monkeypatch.setenv("GTFS_RT_POLL_INTERVAL_S", "1")
    assert gtfs_rt.poll_interval_s() == 15.0
    monkeypatch.setenv("GTFS_RT_POLL_INTERVAL_S", "nonsense")
    assert gtfs_rt.poll_interval_s() == gtfs_rt.DEFAULT_POLL_S


def test_concurrent_readers_share_one_request(monkeypatch):
    now = int(time.time())
    calls: list = []

    class SlowClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None, **kw):
            calls.append(url)
            time.sleep(0.15)                 # long enough for the others to pile up
            return FakeResponse(build_feed({"vehicle_id": "A", "lat": 12.9,
                                            "lon": 77.6, "timestamp": now}))

    monkeypatch.setattr(gtfs_rt.httpx, "Client", SlowClient)
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")

    results = []
    threads = [threading.Thread(target=lambda: results.append(gtfs_rt.positions()))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1
    assert all(r["available"] for r in results)


def test_failed_fetch_is_cached_briefly_so_a_broken_feed_is_not_hammered(monkeypatch):
    calls: list = []
    fake_http(monkeypatch, FakeResponse(b"nope", status_code=500), calls)
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    for _ in range(3):
        client.get("/api/transit/vehicle-positions")
    assert len(calls) == 1


def test_auth_header_is_sent_but_never_echoed(monkeypatch):
    now = int(time.time())
    calls: list = []
    fake_http(monkeypatch, FakeResponse(build_feed(
        {"vehicle_id": "A", "lat": 12.9, "lon": 77.6, "timestamp": now})), calls)
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL", "https://feed.example.gov/vp.pb")
    monkeypatch.setenv("GTFS_RT_AUTH_HEADER", "Authorization: Bearer s3cr3t-token-abcdef")

    r = client.get("/api/transit/vehicle-positions")
    assert calls[0]["headers"]["Authorization"] == "Bearer s3cr3t-token-abcdef"
    assert "s3cr3t-token-abcdef" not in r.text
    assert "s3cr3t-token-abcdef" not in client.get("/api/transit/realtime-status").text


def test_source_is_host_only_so_query_tokens_stay_secret(monkeypatch):
    now = int(time.time())
    fake_http(monkeypatch, FakeResponse(build_feed(
        {"vehicle_id": "A", "lat": 12.9, "lon": 77.6, "timestamp": now})))
    monkeypatch.setenv("GTFS_RT_VEHICLE_URL",
                       "https://feed.example.gov/vp.pb?token=leaky-token-value")
    body = client.get("/api/transit/vehicle-positions").json()
    assert body["source"] == "feed.example.gov"
    assert "leaky-token-value" not in json.dumps(body)


def test_realtime_status_endpoint_reports_without_a_feed():
    body = client.get("/api/transit/realtime-status").json()
    assert body["configured"] is False
    assert body["ok"] is False


def test_trip_updates_unconfigured_is_200_available_false():
    body = client.get("/api/transit/trip-updates").json()
    assert body["available"] is False and body["kind"] == "not_configured"
    assert "GTFS_RT_TRIPUPDATES_URL" in body["needs"]


def test_registry_entry_is_not_ready_without_a_feed():
    from src.services import integrations as reg
    st = reg.status("gtfs_realtime")
    assert st["state"] == reg.NOT_CONFIGURED
    assert st["healthy"] is False
    # The unsupported route stays labelled, and the unofficial API is not offered.
    assert reg.status("bmtc_unofficial_api")["state"] == reg.UNSUPPORTED
