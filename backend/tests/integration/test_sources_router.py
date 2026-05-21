"""Integration tests for GET/PATCH /v1/sources/* endpoints."""
from __future__ import annotations

import json


def test_list_sources_returns_all_declared(client):
    resp = client.get("/v1/sources")
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()}
    expected = {"lastfm", "spotify", "deezer", "itunes", "musicbrainz",
                "bandcamp", "discogs", "invidious", "ytdlp", "youtube_rss"}
    assert expected <= names


def test_list_sources_no_credentials_leaked(client):
    resp = client.get("/v1/sources")
    assert resp.status_code == 200
    payload = json.dumps(resp.json())
    assert "credentials_json" not in payload
    for source in resp.json():
        assert "credentials_json" not in source


def test_get_source_ok(client):
    resp = client.get("/v1/sources/lastfm")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "lastfm"
    assert "enabled" in data
    assert "weight" in data
    assert "circuit_open" in data


def test_get_source_not_found(client):
    resp = client.get("/v1/sources/nonexistent_xyz")
    assert resp.status_code == 404


def test_patch_disable_and_reenable(client):
    resp = client.patch("/v1/sources/deezer", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] == 0

    resp2 = client.patch("/v1/sources/deezer", json={"enabled": True})
    assert resp2.status_code == 200
    assert resp2.json()["enabled"] == 1


def test_patch_weight(client):
    resp = client.patch("/v1/sources/deezer", json={"weight": 0.42})
    assert resp.status_code == 200
    assert abs(resp.json()["weight"] - 0.42) < 0.001


def test_patch_weight_out_of_range(client):
    resp = client.patch("/v1/sources/deezer", json={"weight": 99.0})
    assert resp.status_code == 422


def test_patch_credentials_write_only(client):
    resp = client.patch("/v1/sources/lastfm", json={"credentials": {"LASTFM_KEY": "test-key"}})
    assert resp.status_code == 200
    data = resp.json()
    # Credential value must never appear in the response
    assert "test-key" not in json.dumps(data)
    # But has_value indicator should be present
    assert data["credential_status"]["LASTFM_KEY"] is True


def test_reset_circuit(client):
    # Trip the circuit manually via DB
    from backend.services.source_registry import _set_fields
    import time
    _set_fields("deezer", circuit_open_until=time.time() + 3600, failure_streak=10)

    resp = client.post("/v1/sources/deezer/reset-circuit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["circuit_open"] is False
    assert data["failure_streak"] == 0


def test_reset_circuit_not_found(client):
    resp = client.post("/v1/sources/nonexistent_xyz/reset-circuit")
    assert resp.status_code == 404
