"""Offline tests for latest-only crawler contract (no provider or webhook network)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.scraper import ev_charging_live as live


def _snapshot():
    return {
        "schemaVersion": 1, "source": "emapna",
        "fetchedAt": datetime.now(UTC).isoformat(),
        "counts": {"stations": 1, "devices": 1, "connectors": 2},
        "stations": [{
            "stationId": "st1", "name": "Station 1",
            "devices": [{
                "deviceId": "dv1", "stationId": "st1",
                "connectors": [
                    {"connectorId": "a", "compositeKey": "dv1::a", "statusCode": 1, "status": "Available"},
                    {"connectorId": "b", "compositeKey": "dv1::b", "statusCode": 3, "status": "Unavailable"},
                ],
            }],
        }],
    }


def test_atomic_latest_only_and_out_of_order_rejection(tmp_path, monkeypatch, stub_ev_charging_db):
    destination = tmp_path / "latest.json"
    monkeypatch.setenv("EV_CHARGING_SNAPSHOT_PATH", str(destination))
    first = _snapshot()
    digest, raw = live.publish_snapshot(first)
    assert json.loads(destination.read_bytes()) == first
    assert len(digest) == 64 and raw == destination.read_bytes()

    older = json.loads(json.dumps(first))
    older["fetchedAt"] = (datetime.fromisoformat(first["fetchedAt"]) - timedelta(minutes=5)).isoformat()
    with pytest.raises(live.CrawlRejected, match="out-of-order"):
        live.publish_snapshot(older)
    second = _snapshot()
    second["fetchedAt"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    second["stations"][0]["devices"][0]["connectors"][0]["statusCode"] = 2
    second["stations"][0]["devices"][0]["connectors"][0]["status"] = "Charging"
    live.publish_snapshot(second)
    assert json.loads(destination.read_bytes()) == second
    assert len(list(tmp_path.iterdir())) == 1


def test_incomplete_or_duplicate_snapshot_never_replaces_latest(tmp_path, monkeypatch, stub_ev_charging_db):
    destination = tmp_path / "latest.json"
    monkeypatch.setenv("EV_CHARGING_SNAPSHOT_PATH", str(destination))
    live.publish_snapshot(_snapshot())
    old = destination.read_bytes()
    broken = _snapshot()
    broken["counts"]["connectors"] = 999
    with pytest.raises(live.CrawlRejected):
        live.publish_snapshot(broken)
    assert destination.read_bytes() == old


class FakeResponse:
    status_code = 200
    def __init__(self, data):
        self.data = data
    def json(self):
        return {"result": {"data": self.data}}


class FakeSession:
    def __init__(self):
        self.ids = []
    def post(self, url, json, timeout, allow_redirects):
        if url.endswith("/search"):
            return FakeResponse([{
                "stationId": "st1", "stationName": "ایستگاه",
                "stationStatus": "Available",
            }])
        if url.endswith("/v2"):
            return FakeResponse({"chargers": [{"id": "dv1", "stationName": "ایستگاه"}]})
        self.ids.append(json["id"])
        return FakeResponse({"stationInfo": {
            "name": "ایستگاه",
            "connectors": [
                {"id": "a", "connectorName": "Car-DC-CCS2", "statusCode": 1},
                {"id": "b", "connectorName": "Car-AC-Type2", "statusCode": 3},
            ],
        }})


def test_real_shape_mapping_and_authoritative_detail_codes(monkeypatch):
    for name in ("STATIONS", "DEVICES", "CONNECTORS"):
        monkeypatch.setenv("EV_CHARGING_MIN_" + name, "1")
    monkeypatch.setenv("EV_CHARGING_DETAIL_DELAY_SECONDS", "0")
    session = FakeSession()
    payload = live.extract_snapshot(session)
    assert payload["counts"] == {"stations": 1, "devices": 1, "connectors": 2}
    connectors = payload["stations"][0]["devices"][0]["connectors"]
    assert [c["status"] for c in connectors] == ["Available", "Unavailable"]
    assert session.ids == ["dv1"]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_source_denial_is_not_retried_or_published(status):
    class Denied:
        status_code = status

    class DeniedSession:
        def __init__(self):
            self.calls = 0

        def post(self, url, json, timeout, allow_redirects):
            self.calls += 1
            return Denied()

    session = DeniedSession()
    with pytest.raises(live.CrawlRejected, match=str(status)):
        live.extract_snapshot(session)
    assert session.calls == 1


def test_missing_detail_never_yields_a_partial_snapshot(monkeypatch):
    for name in ("STATIONS", "DEVICES", "CONNECTORS"):
        monkeypatch.setenv("EV_CHARGING_MIN_" + name, "1")

    class BrokenSession(FakeSession):
        def post(self, url, json, timeout, allow_redirects):
            if url.endswith("/getStation"):
                return FakeResponse({"stationInfo": {"connectors": []}})
            return super().post(url, json, timeout, allow_redirects)

    with pytest.raises(live.CrawlRejected, match="missing detailed connectors"):
        live.extract_snapshot(BrokenSession())
