"""Integration tests for GET /v1/items/* endpoints."""
from __future__ import annotations


def test_schemes_returns_four(client):
    resp = client.get("/v1/items/schemes")
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()}
    assert {"yt_video", "music_track", "music_album", "music_artist"} == names


def test_schemes_have_fields(client):
    resp = client.get("/v1/items/schemes")
    by_name = {s["name"]: s for s in resp.json()}
    assert len(by_name["yt_video"]["fields"]) > 0
    assert any(f["name"] == "title" for f in by_name["yt_video"]["fields"])


def test_items_list_empty_by_default(client):
    resp = client.get("/v1/items/")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_items_list_filter_by_scheme(client):
    resp = client.get("/v1/items/?scheme=yt_video&limit=5")
    assert resp.status_code == 200


def test_item_not_found(client):
    resp = client.get("/v1/items/999999")
    assert resp.status_code == 404


def test_create_scheme_and_retrieve(client):
    resp = client.post("/v1/items/schemes", json={
        "name": "test_movie",
        "display_name": "Test Movie",
        "description": "for testing",
        "fields": [{"name": "title", "type": "text", "label": "Title", "required": True}],
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "test_movie"

    schemes = {s["name"] for s in client.get("/v1/items/schemes").json()}
    assert "test_movie" in schemes
