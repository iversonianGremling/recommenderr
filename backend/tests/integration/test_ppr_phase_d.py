"""Phase D integration tests: seeds, graph/stats, feed-filters, weight-rule types."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Invalidate feed cache before each test so stale snapshots don't bleed."""
    from backend.services import feed_cache
    feed_cache._snapshots.clear()
    yield


# ---------------------------------------------------------------------------
# /v1/ppr/seeds
# ---------------------------------------------------------------------------

def test_seeds_returns_list(client: TestClient):
    r = client.get("/v1/ppr/seeds?limit=10")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)


def test_seeds_schema(client: TestClient):
    """Each seed has the expected keys (empty list is also valid on a fresh DB)."""
    r = client.get("/v1/ppr/seeds?limit=50")
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list)
    if items:
        s = items[0]
        assert "video_id" in s
        assert "weight" in s
        assert "reasons" in s
        assert isinstance(s["reasons"], list)


# ---------------------------------------------------------------------------
# /v1/ppr/graph/stats
# ---------------------------------------------------------------------------

def test_graph_stats_shape(client: TestClient):
    r = client.get("/v1/ppr/graph/stats")
    assert r.status_code == 200
    d = r.json()
    assert "nodes" in d
    assert "edges" in d
    assert "density" in d
    assert "scored_nodes" in d
    assert isinstance(d["nodes"], int)
    assert isinstance(d["edges"], int)


def test_graph_stats_empty_db(client: TestClient):
    """On a fresh DB, nodes/edges should be 0."""
    r = client.get("/v1/ppr/graph/stats")
    assert r.status_code == 200
    d = r.json()
    assert d["nodes"] == 0
    assert d["edges"] == 0
    assert d["density"] == 0.0


# ---------------------------------------------------------------------------
# /v1/ppr/feed-filters
# ---------------------------------------------------------------------------

def test_feed_filters_empty(client: TestClient):
    r = client.get("/v1/ppr/feed-filters")
    assert r.status_code == 200
    assert r.json() == []


def test_feed_filter_add_and_list(client: TestClient):
    r = client.post("/v1/ppr/feed-filters", json={"filter_type": "channel_id", "match_value": "UCtest"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r2 = client.get("/v1/ppr/feed-filters")
    assert r2.status_code == 200
    filters = r2.json()
    assert any(f["match_value"] == "UCtest" for f in filters)


def test_feed_filter_delete(client: TestClient):
    client.post("/v1/ppr/feed-filters", json={"filter_type": "keyword", "match_value": "spam_keyword"})
    filters = client.get("/v1/ppr/feed-filters").json()
    fid = next(f["id"] for f in filters if f["match_value"] == "spam_keyword")

    r = client.delete(f"/v1/ppr/feed-filters/{fid}")
    assert r.status_code == 200

    filters2 = client.get("/v1/ppr/feed-filters").json()
    assert not any(f["id"] == fid for f in filters2)


def test_feed_filter_invalid_type(client: TestClient):
    r = client.post("/v1/ppr/feed-filters", json={"filter_type": "bad_type", "match_value": "x"})
    assert r.status_code == 400


def test_feed_filter_empty_match_rejected(client: TestClient):
    r = client.post("/v1/ppr/feed-filters", json={"filter_type": "channel_id", "match_value": "  "})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Weight-rule validator — extended types (Phase D fix)
# ---------------------------------------------------------------------------

def test_weight_rule_genre_accepted(client: TestClient):
    r = client.post("/v1/ppr/weight-rules", json={"rule_type": "genre", "match_value": "classical", "multiplier": 1.5})
    assert r.status_code == 200


def test_weight_rule_category_accepted(client: TestClient):
    r = client.post("/v1/ppr/weight-rules", json={"rule_type": "category", "match_value": "music", "multiplier": 0.5})
    assert r.status_code == 200


def test_weight_rule_attribute_accepted(client: TestClient):
    r = client.post("/v1/ppr/weight-rules", json={"rule_type": "attribute", "match_value": "live", "multiplier": 2.0})
    assert r.status_code == 200


def test_weight_rule_invalid_type_rejected(client: TestClient):
    r = client.post("/v1/ppr/weight-rules", json={"rule_type": "bogus", "match_value": "x", "multiplier": 1.0})
    assert r.status_code == 400
