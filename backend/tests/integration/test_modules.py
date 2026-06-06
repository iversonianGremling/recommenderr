"""Phase G integration tests: custom modules CRUD, test-run, recompute."""
from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

_SCORER_CODE = """\
def score(candidates):
    result = {}
    for c in candidates:
        result[c['video_id']] = c.get('score', 0) * 2.0
    return result
"""

_FILTER_CODE = """\
def filter_items(items):
    return [i for i in items if (i.get('duration') or 0) < 7200]
"""

_BAD_CODE = "def bad(): pass"  # missing required entry function


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_feed(tmp_db: str) -> None:
    """Insert a handful of fake feed rows so test/recompute have data."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")
    now = time.time()
    for i in range(5):
        vid = f"vid{i}"
        conn.execute(
            "INSERT OR IGNORE INTO feed_recommendations "
            "(video_id, title, author, duration, source_video_id, added_at) "
            "VALUES (?,?,?,?,?,?)",
            (vid, f"Title {i}", "Author", 300, "src", now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO ppr_scores (video_id, score, computed_at) VALUES (?,?,?)",
            (vid, 0.01 * (i + 1), now),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_list_modules_empty(client: TestClient):
    r = client.get("/v1/modules")
    assert r.status_code == 200
    assert r.json() == []


def test_create_scorer(client: TestClient):
    r = client.post("/v1/modules", json={"name": "My Scorer", "type": "scorer"})
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "My Scorer"
    assert d["type"] == "scorer"
    assert d["enabled"] == 1
    assert "score(candidates)" in d["code"]


def test_create_filter(client: TestClient):
    r = client.post("/v1/modules", json={"name": "My Filter", "type": "filter"})
    assert r.status_code == 200
    d = r.json()
    assert d["type"] == "filter"
    assert "filter_items(items)" in d["code"]


def test_create_invalid_type(client: TestClient):
    r = client.post("/v1/modules", json={"name": "X", "type": "ranker"})
    assert r.status_code == 400


def test_create_bad_code(client: TestClient):
    r = client.post("/v1/modules", json={"name": "Bad", "type": "scorer", "code": _BAD_CODE})
    assert r.status_code == 400
    assert "errors" in r.json()["detail"]


def test_get_module(client: TestClient):
    created = client.post("/v1/modules", json={"name": "S", "type": "scorer"}).json()
    r = client.get(f"/v1/modules/{created['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]


def test_get_module_not_found(client: TestClient):
    r = client.get("/v1/modules/999")
    assert r.status_code == 404


def test_update_module_name(client: TestClient):
    m = client.post("/v1/modules", json={"name": "Old", "type": "scorer"}).json()
    r = client.put(f"/v1/modules/{m['id']}", json={"name": "New"})
    assert r.status_code == 200
    assert r.json()["name"] == "New"


def test_update_module_toggle_enabled(client: TestClient):
    m = client.post("/v1/modules", json={"name": "M", "type": "scorer"}).json()
    r = client.put(f"/v1/modules/{m['id']}", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] == 0


def test_update_module_bad_code_rejected(client: TestClient):
    m = client.post("/v1/modules", json={"name": "M", "type": "scorer"}).json()
    r = client.put(f"/v1/modules/{m['id']}", json={"code": _BAD_CODE})
    assert r.status_code == 400


def test_delete_module(client: TestClient):
    m = client.post("/v1/modules", json={"name": "D", "type": "scorer"}).json()
    r = client.delete(f"/v1/modules/{m['id']}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert client.get(f"/v1/modules/{m['id']}").status_code == 404


def test_list_modules_returns_created(client: TestClient):
    client.post("/v1/modules", json={"name": "A", "type": "scorer"})
    client.post("/v1/modules", json={"name": "B", "type": "filter"})
    modules = client.get("/v1/modules").json()
    assert len(modules) == 2
    types = {m["type"] for m in modules}
    assert types == {"scorer", "filter"}


# ---------------------------------------------------------------------------
# Test-run
# ---------------------------------------------------------------------------

def test_test_scorer(client: TestClient, tmp_db: str):
    _seed_feed(tmp_db)
    m = client.post("/v1/modules", json={"name": "S", "type": "scorer", "code": _SCORER_CODE}).json()
    r = client.post(f"/v1/modules/{m['id']}/test", json={"limit": 5})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert len(d["results"]) > 0
    for row in d["results"]:
        assert "module_score" in row


def test_test_filter(client: TestClient, tmp_db: str):
    _seed_feed(tmp_db)
    m = client.post("/v1/modules", json={"name": "F", "type": "filter", "code": _FILTER_CODE}).json()
    r = client.post(f"/v1/modules/{m['id']}/test", json={"limit": 5})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True


def test_test_module_not_found(client: TestClient):
    r = client.post("/v1/modules/999/test", json={"limit": 5})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Recompute + scores
# ---------------------------------------------------------------------------

def test_recompute_scorer(client: TestClient, tmp_db: str):
    _seed_feed(tmp_db)
    m = client.post("/v1/modules", json={"name": "S", "type": "scorer", "code": _SCORER_CODE}).json()
    r = client.post(f"/v1/modules/{m['id']}/recompute")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["scored"] > 0


def test_scores_after_recompute(client: TestClient, tmp_db: str):
    _seed_feed(tmp_db)
    m = client.post("/v1/modules", json={"name": "S", "type": "scorer", "code": _SCORER_CODE}).json()
    client.post(f"/v1/modules/{m['id']}/recompute")
    r = client.get(f"/v1/modules/{m['id']}/scores?limit=10")
    assert r.status_code == 200
    scores = r.json()
    assert len(scores) > 0
    for row in scores:
        assert "video_id" in row
        assert "score" in row


def test_scores_empty_before_recompute(client: TestClient, tmp_db: str):
    _seed_feed(tmp_db)
    m = client.post("/v1/modules", json={"name": "S", "type": "scorer"}).json()
    r = client.get(f"/v1/modules/{m['id']}/scores")
    assert r.status_code == 200
    assert r.json() == []


def test_delete_cascades_scores(client: TestClient, tmp_db: str):
    _seed_feed(tmp_db)
    m = client.post("/v1/modules", json={"name": "S", "type": "scorer", "code": _SCORER_CODE}).json()
    client.post(f"/v1/modules/{m['id']}/recompute")
    # Verify scores exist
    scores_before = client.get(f"/v1/modules/{m['id']}/scores").json()
    assert len(scores_before) > 0
    # Delete module → scores should cascade-delete
    client.delete(f"/v1/modules/{m['id']}")
    conn = sqlite3.connect(tmp_db)
    count = conn.execute(
        "SELECT COUNT(*) FROM custom_module_scores WHERE module_id = ?", (m["id"],)
    ).fetchone()[0]
    conn.close()
    assert count == 0
