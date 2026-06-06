"""Per-graph isolation tests.

Each test verifies that writing to graph A does not bleed into graph B:
  - weight rules
  - feed filters
  - pipeline config
  - feed items (graph_feed_items + get_ppr_feed)
  - pipeline_status scoped counts
"""
import time
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _fresh_cache():
    from backend.services import feed_cache
    feed_cache._snapshots.clear()
    yield


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed_graphs(client: TestClient):
    """Ensure graphs 2 and 3 exist (they are seeded from schema.sql on fresh DBs)."""
    r = client.get("/v1/graphs")
    assert r.status_code == 200
    ids = {g["id"] for g in r.json()}
    return ids


def _insert_feed_item(video_id: str, graph_id: int):
    """Directly insert a feed_recommendation + graph_feed_item via the DB layer."""
    import os
    import sqlite3
    path = os.environ["DB_PATH"]
    con = sqlite3.connect(path, isolation_level=None)
    now = time.time()
    con.execute(
        "INSERT OR IGNORE INTO feed_recommendations "
        "(video_id, title, thumbnail, duration, author, author_id, source_video_id, source_video_title, added_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (video_id, f"Title {video_id}", "", 60, "Author", "ch1", "src1", "Src Title", now),
    )
    con.execute(
        "INSERT OR IGNORE INTO graph_feed_items (graph_id, video_id, source_video_id, added_at) VALUES (?,?,?,?)",
        (graph_id, video_id, "src1", now),
    )
    con.execute(
        "INSERT OR IGNORE INTO ppr_scores (video_id, graph_id, score, computed_at) VALUES (?,?,?,?)",
        (video_id, graph_id, 0.9, now),
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Weight rule isolation
# ---------------------------------------------------------------------------

def test_weight_rule_added_to_graph2_invisible_in_graph3(client: TestClient):
    _seed_graphs(client)

    r = client.post("/v1/ppr/weight-rules", json={
        "rule_type": "keyword", "match_value": "jazz", "multiplier": 1.5, "graph_id": 2
    })
    assert r.status_code == 200

    r2 = client.get("/v1/ppr/weight-rules?graph_id=2")
    assert r2.status_code == 200
    assert any(w["match_value"] == "jazz" for w in r2.json()), "rule should appear in graph 2"

    r3 = client.get("/v1/ppr/weight-rules?graph_id=3")
    assert r3.status_code == 200
    assert not any(w["match_value"] == "jazz" for w in r3.json()), "rule must NOT appear in graph 3"


def test_weight_rule_added_to_graph3_invisible_in_graph2(client: TestClient):
    _seed_graphs(client)

    client.post("/v1/ppr/weight-rules", json={
        "rule_type": "keyword", "match_value": "metal", "multiplier": 0.5, "graph_id": 3
    })

    r2 = client.get("/v1/ppr/weight-rules?graph_id=2")
    assert not any(w["match_value"] == "metal" for w in r2.json())

    r3 = client.get("/v1/ppr/weight-rules?graph_id=3")
    assert any(w["match_value"] == "metal" for w in r3.json())


def test_weight_rule_delete_scoped(client: TestClient):
    """Deleting a rule in graph 2 should not remove the same-valued rule in graph 3."""
    _seed_graphs(client)

    client.post("/v1/ppr/weight-rules", json={"rule_type": "keyword", "match_value": "pop", "multiplier": 2.0, "graph_id": 2})
    client.post("/v1/ppr/weight-rules", json={"rule_type": "keyword", "match_value": "pop", "multiplier": 2.0, "graph_id": 3})

    rules_g2 = client.get("/v1/ppr/weight-rules?graph_id=2").json()
    rule_id = next(w["id"] for w in rules_g2 if w["match_value"] == "pop")

    del_r = client.delete(f"/v1/ppr/weight-rules/{rule_id}?graph_id=2")
    assert del_r.status_code == 200

    assert not any(w["match_value"] == "pop" for w in client.get("/v1/ppr/weight-rules?graph_id=2").json())
    assert any(w["match_value"] == "pop" for w in client.get("/v1/ppr/weight-rules?graph_id=3").json())


# ---------------------------------------------------------------------------
# Feed filter isolation
# ---------------------------------------------------------------------------

def test_feed_filter_added_to_graph2_invisible_in_graph3(client: TestClient):
    _seed_graphs(client)

    r = client.post("/v1/ppr/feed-filters", json={
        "filter_type": "keyword", "match_value": "clickbait", "graph_id": 2
    })
    assert r.status_code == 200

    filters_g2 = client.get("/v1/ppr/feed-filters?graph_id=2").json()
    filters_g3 = client.get("/v1/ppr/feed-filters?graph_id=3").json()

    assert any(f["match_value"] == "clickbait" for f in filters_g2)
    assert not any(f["match_value"] == "clickbait" for f in filters_g3)


def test_feed_filter_blocks_only_its_graph(client: TestClient):
    """A keyword filter on graph 2 should hide matching items from get_ppr_feed(graph_id=2)
    but leave items in graph 3 untouched."""
    _seed_graphs(client)

    # Insert a video into BOTH graphs
    _insert_feed_item("vid_blocked_test", graph_id=2)
    _insert_feed_item("vid_blocked_test", graph_id=3)

    # Add filter on graph 2 only (title is "Title vid_blocked_test")
    client.post("/v1/ppr/feed-filters", json={
        "filter_type": "keyword", "match_value": "vid_blocked", "graph_id": 2
    })

    from backend.db import get_ppr_feed
    feed_g2 = get_ppr_feed(graph_id=2, _skip_recompute=True)
    feed_g3 = get_ppr_feed(graph_id=3, _skip_recompute=True)

    vids_g2 = {item["video_id"] for item in feed_g2}
    vids_g3 = {item["video_id"] for item in feed_g3}

    assert "vid_blocked_test" not in vids_g2, "filtered video should be gone from graph 2"
    assert "vid_blocked_test" in vids_g3, "video in graph 3 should be unaffected by graph 2 filter"


# ---------------------------------------------------------------------------
# Pipeline config isolation
# ---------------------------------------------------------------------------

def test_pipeline_config_save_scoped(client: TestClient):
    _seed_graphs(client)

    # Read defaults
    cfg_g2_before = client.get("/v1/ppr/pipeline/config?graph_id=2").json()
    original_weight = cfg_g2_before.get("scorer.ppr.weight", 1.0)

    new_weight = 0.42
    r = client.put("/v1/ppr/pipeline/config", json={"updates": {"scorer.ppr.weight": new_weight}, "graph_id": 2})
    assert r.status_code == 200

    cfg_g2_after = client.get("/v1/ppr/pipeline/config?graph_id=2").json()
    cfg_g3_after = client.get("/v1/ppr/pipeline/config?graph_id=3").json()

    assert abs(cfg_g2_after["scorer.ppr.weight"] - new_weight) < 0.001, "config update should persist for graph 2"
    assert abs(cfg_g3_after.get("scorer.ppr.weight", 1.0) - new_weight) > 0.001, "graph 3 config must not be affected"


def test_pipeline_config_separate_keys_per_graph(client: TestClient):
    _seed_graphs(client)

    client.put("/v1/ppr/pipeline/config", json={"updates": {"scorer.cosine.weight": 0.11}, "graph_id": 2})
    client.put("/v1/ppr/pipeline/config", json={"updates": {"scorer.cosine.weight": 0.99}, "graph_id": 3})

    cfg2 = client.get("/v1/ppr/pipeline/config?graph_id=2").json()
    cfg3 = client.get("/v1/ppr/pipeline/config?graph_id=3").json()

    assert abs(cfg2["scorer.cosine.weight"] - 0.11) < 0.001
    assert abs(cfg3["scorer.cosine.weight"] - 0.99) < 0.001


# ---------------------------------------------------------------------------
# Feed / graph_feed_items isolation
# ---------------------------------------------------------------------------

def test_save_recommendations_multi_graph(client: TestClient, tmp_db):
    """save_recommendations with graph_ids=[2,3] creates one metadata row but two feed memberships."""
    import os, sqlite3
    from backend import db as dbmod

    recs = [{"videoId": f"v{i}", "title": f"T{i}", "lengthSeconds": 60, "author": "A", "authorId": "c1"} for i in range(3)]
    dbmod.save_recommendations("src_multi", "Src Title", recs, graph_ids=[2, 3])

    con = sqlite3.connect(os.environ["DB_PATH"])
    con.row_factory = sqlite3.Row

    meta_count = con.execute("SELECT COUNT(*) as n FROM feed_recommendations WHERE source_video_id='src_multi'").fetchone()["n"]
    assert meta_count == 3, "should have exactly 3 metadata rows"

    gfi_g2 = con.execute("SELECT COUNT(*) as n FROM graph_feed_items WHERE graph_id=2 AND source_video_id='src_multi'").fetchone()["n"]
    gfi_g3 = con.execute("SELECT COUNT(*) as n FROM graph_feed_items WHERE graph_id=3 AND source_video_id='src_multi'").fetchone()["n"]
    assert gfi_g2 == 3, "all 3 videos should appear in graph 2"
    assert gfi_g3 == 3, "all 3 videos should appear in graph 3"

    gfi_g1 = con.execute("SELECT COUNT(*) as n FROM graph_feed_items WHERE graph_id=1 AND source_video_id='src_multi'").fetchone()["n"]
    assert gfi_g1 == 0, "should NOT be in graph 1 (Mixed) since it wasn't in graph_ids"
    con.close()


def test_get_ppr_feed_only_returns_own_graph(client: TestClient):
    """Videos in graph 2 must not appear in graph 3's feed and vice versa."""
    _seed_graphs(client)

    _insert_feed_item("vid_g2_only", graph_id=2)
    _insert_feed_item("vid_g3_only", graph_id=3)

    from backend.db import get_ppr_feed
    feed_g2 = get_ppr_feed(graph_id=2, _skip_recompute=True)
    feed_g3 = get_ppr_feed(graph_id=3, _skip_recompute=True)

    vids_g2 = {item["video_id"] for item in feed_g2}
    vids_g3 = {item["video_id"] for item in feed_g3}

    assert "vid_g2_only" in vids_g2
    assert "vid_g2_only" not in vids_g3

    assert "vid_g3_only" in vids_g3
    assert "vid_g3_only" not in vids_g2


# ---------------------------------------------------------------------------
# Pipeline status scoped counts
# ---------------------------------------------------------------------------

def test_pipeline_status_returns_per_graph_filter_count(client: TestClient):
    _seed_graphs(client)

    client.post("/v1/ppr/feed-filters", json={"filter_type": "keyword", "match_value": "a", "graph_id": 2})
    client.post("/v1/ppr/feed-filters", json={"filter_type": "keyword", "match_value": "b", "graph_id": 2})
    client.post("/v1/ppr/feed-filters", json={"filter_type": "keyword", "match_value": "c", "graph_id": 3})

    st2 = client.get("/v1/ppr/pipeline/status?graph_id=2").json()
    st3 = client.get("/v1/ppr/pipeline/status?graph_id=3").json()

    # status returns {"filters": {"feed_filter_count": N, ...}}
    count2 = st2["filters"]["feed_filter_count"]
    count3 = st3["filters"]["feed_filter_count"]

    assert count2 == 2, f"expected 2 filters for graph 2, got {count2}"
    assert count3 == 1, f"expected 1 filter for graph 3, got {count3}"


def test_pipeline_status_returns_per_graph_weight_rule_count(client: TestClient):
    _seed_graphs(client)

    client.post("/v1/ppr/weight-rules", json={"rule_type": "keyword", "match_value": "x", "multiplier": 1.2, "graph_id": 2})
    client.post("/v1/ppr/weight-rules", json={"rule_type": "keyword", "match_value": "y", "multiplier": 0.8, "graph_id": 3})
    client.post("/v1/ppr/weight-rules", json={"rule_type": "keyword", "match_value": "z", "multiplier": 1.5, "graph_id": 3})

    st2 = client.get("/v1/ppr/pipeline/status?graph_id=2").json()
    st3 = client.get("/v1/ppr/pipeline/status?graph_id=3").json()

    count2 = st2["filters"]["weight_rule_count"]
    count3 = st3["filters"]["weight_rule_count"]

    assert count2 == 1, f"expected 1 weight rule for graph 2, got {count2}"
    assert count3 == 2, f"expected 2 weight rules for graph 3, got {count3}"


# ---------------------------------------------------------------------------
# Graph sources isolation
# ---------------------------------------------------------------------------

def _seed_sources(tmp_db_path: str):
    """Insert minimal sources and graph_source rows for testing."""
    import sqlite3
    con = sqlite3.connect(tmp_db_path, isolation_level=None)
    now = time.time()
    for name, kind in [("spotify", "music"), ("invidious", "video"), ("lastfm", "music")]:
        con.execute(
            "INSERT OR IGNORE INTO sources (name, display_name, kind, enabled) VALUES (?,?,?,1)",
            (name, name.capitalize(), kind),
        )
    # graph 2 (Songs): spotify, lastfm
    con.execute("INSERT OR IGNORE INTO graph_sources (graph_id, source_name) VALUES (2,'spotify')")
    con.execute("INSERT OR IGNORE INTO graph_sources (graph_id, source_name) VALUES (2,'lastfm')")
    # graph 3 (Videos): invidious
    con.execute("INSERT OR IGNORE INTO graph_sources (graph_id, source_name) VALUES (3,'invidious')")
    con.commit()
    con.close()


def test_graph_sources_scope(client: TestClient, tmp_db):
    """graph_sources endpoint lists only sources configured for that graph."""
    _seed_graphs(client)
    _seed_sources(tmp_db)

    r2 = client.get("/v1/graphs/2/sources")
    assert r2.status_code == 200
    r3 = client.get("/v1/graphs/3/sources")
    assert r3.status_code == 200

    names_g2_in = {s["name"] for s in r2.json() if s["in_graph"]}
    names_g3_in = {s["name"] for s in r3.json() if s["in_graph"]}

    assert "spotify" in names_g2_in, "spotify should be in graph 2 (Songs)"
    assert "lastfm" in names_g2_in, "lastfm should be in graph 2"
    assert "invidious" not in names_g2_in, "invidious should NOT be in graph 2"
    assert "invidious" in names_g3_in, "invidious should be in graph 3 (Videos)"
    assert "spotify" not in names_g3_in, "spotify should NOT be in graph 3"


def test_graph_source_toggle_scoped(client: TestClient, tmp_db):
    """Disabling a source for graph 2 should not affect graph 3's in_graph state."""
    _seed_graphs(client)
    _seed_sources(tmp_db)

    # Disable spotify in graph 2 (it's currently in_graph=True)
    r = client.put("/v1/graphs/2/sources/spotify", json={"in_graph": False})
    assert r.status_code == 200

    src_g2 = {s["name"]: s for s in client.get("/v1/graphs/2/sources").json()}
    src_g3 = {s["name"]: s for s in client.get("/v1/graphs/3/sources").json()}

    assert src_g2["spotify"]["in_graph"] is False, "spotify should be disabled for graph 2"
    # graph 3 sources unchanged
    assert src_g3["invidious"]["in_graph"] is True, "graph 3's invidious should be unaffected"
