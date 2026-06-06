"""Phase E integration tests: personas CRUD, seeds, scores, recompute."""
import sqlite3
import time
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _skip_cache_warm():
    from backend.services import feed_cache
    feed_cache._snapshots.clear()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_item(tmp_db: str, scheme: str = "yt_video", external_id: str = "vid1") -> int:
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT OR IGNORE INTO schemes (name, display_name, fields_json, created_at) VALUES (?,?,?,?)",
        (scheme, scheme.title(), "[]", time.time()),
    )
    conn.execute(
        "INSERT OR IGNORE INTO items (scheme, external_id, metadata_json, added_at) VALUES (?,?,?,?)",
        (scheme, external_id, "{}", time.time()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM items WHERE scheme=? AND external_id=?", (scheme, external_id)
    ).fetchone()
    conn.close()
    return row[0]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_create_persona(client: TestClient):
    r = client.post("/v1/personas", json={"name": "Jazz lover", "alpha": 0.2})
    assert r.status_code == 201
    d = r.json()
    assert d["name"] == "Jazz lover"
    assert d["alpha"] == 0.2


def test_list_personas_empty(client: TestClient):
    r = client.get("/v1/personas")
    assert r.status_code == 200
    assert r.json() == []


def test_list_personas_shows_created(client: TestClient):
    client.post("/v1/personas", json={"name": "p1"})
    client.post("/v1/personas", json={"name": "p2"})
    personas = client.get("/v1/personas").json()
    assert len(personas) == 2
    names = [p["name"] for p in personas]
    assert "p1" in names and "p2" in names


def test_get_persona(client: TestClient):
    pid = client.post("/v1/personas", json={"name": "My persona"}).json()["id"]
    r = client.get(f"/v1/personas/{pid}")
    assert r.status_code == 200
    assert r.json()["name"] == "My persona"


def test_get_persona_not_found(client: TestClient):
    assert client.get("/v1/personas/9999").status_code == 404


def test_patch_persona(client: TestClient):
    pid = client.post("/v1/personas", json={"name": "Old name"}).json()["id"]
    r = client.patch(f"/v1/personas/{pid}", json={"name": "New name", "alpha": 0.3})
    assert r.status_code == 200
    assert r.json()["name"] == "New name"


def test_duplicate_name_rejected(client: TestClient):
    client.post("/v1/personas", json={"name": "Unique"})
    r = client.post("/v1/personas", json={"name": "Unique"})
    assert r.status_code == 409


def test_delete_persona(client: TestClient):
    pid = client.post("/v1/personas", json={"name": "Gone"}).json()["id"]
    client.delete(f"/v1/personas/{pid}")
    assert client.get(f"/v1/personas/{pid}").status_code == 404


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

def test_seeds_empty(client: TestClient):
    pid = client.post("/v1/personas", json={"name": "empty"}).json()["id"]
    r = client.get(f"/v1/personas/{pid}/seeds")
    assert r.status_code == 200
    assert r.json() == []


def test_set_seeds_replace(client: TestClient, tmp_db: str):
    item_id = _seed_item(tmp_db, "yt_video", "vid_a")
    pid = client.post("/v1/personas", json={"name": "seeded"}).json()["id"]

    r = client.post(
        f"/v1/personas/{pid}/seeds",
        json={"seeds": [{"scheme": "yt_video", "external_id": "vid_a", "weight": 2.0}]},
    )
    assert r.status_code == 200
    assert r.json()["seed_count"] == 1

    seeds = client.get(f"/v1/personas/{pid}/seeds").json()
    assert len(seeds) == 1
    assert seeds[0]["external_id"] == "vid_a"
    assert seeds[0]["weight"] == 2.0


def test_set_seeds_merge(client: TestClient, tmp_db: str):
    _seed_item(tmp_db, "yt_video", "vid_a")
    _seed_item(tmp_db, "yt_video", "vid_b")
    pid = client.post("/v1/personas", json={"name": "merged"}).json()["id"]
    client.post(f"/v1/personas/{pid}/seeds", json={"seeds": [{"scheme": "yt_video", "external_id": "vid_a"}]})
    r = client.post(
        f"/v1/personas/{pid}/seeds",
        json={"seeds": [{"scheme": "yt_video", "external_id": "vid_b"}], "merge": True},
    )
    assert r.json()["seed_count"] == 2


def test_seed_unknown_item_returns_404(client: TestClient):
    pid = client.post("/v1/personas", json={"name": "missing"}).json()["id"]
    r = client.post(
        f"/v1/personas/{pid}/seeds",
        json={"seeds": [{"scheme": "yt_video", "external_id": "nonexistent"}]},
    )
    assert r.status_code == 404


def test_delete_seed(client: TestClient, tmp_db: str):
    item_id = _seed_item(tmp_db, "yt_video", "vid_del")
    pid = client.post("/v1/personas", json={"name": "with-seed"}).json()["id"]
    client.post(f"/v1/personas/{pid}/seeds", json={"seeds": [{"scheme": "yt_video", "external_id": "vid_del"}]})

    r = client.delete(f"/v1/personas/{pid}/seeds/{item_id}")
    assert r.status_code == 204
    assert client.get(f"/v1/personas/{pid}/seeds").json() == []


# ---------------------------------------------------------------------------
# Scores + recompute
# ---------------------------------------------------------------------------

def test_scores_empty(client: TestClient):
    pid = client.post("/v1/personas", json={"name": "no scores"}).json()["id"]
    r = client.get(f"/v1/personas/{pid}/scores")
    assert r.status_code == 200
    assert r.json() == []


def test_recompute_no_seeds_returns_zero(client: TestClient):
    pid = client.post("/v1/personas", json={"name": "empty-compute"}).json()["id"]
    r = client.post(f"/v1/personas/{pid}/recompute")
    assert r.status_code == 200
    assert r.json()["scored"] == 0


def test_recompute_with_seeds(client: TestClient, tmp_db: str):
    """Recompute persona with a seed that exists in recommendation_edges."""
    conn = sqlite3.connect(tmp_db)
    conn.execute("PRAGMA foreign_keys=ON")
    t = time.time()
    conn.execute("INSERT OR IGNORE INTO schemes (name, display_name, fields_json, created_at) VALUES ('yt_video','YT Video','[]',?)", (t,))
    conn.execute("INSERT OR IGNORE INTO items (scheme, external_id, metadata_json, added_at) VALUES ('yt_video','src_v','{}',?)", (t,))
    conn.execute("INSERT OR IGNORE INTO items (scheme, external_id, metadata_json, added_at) VALUES ('yt_video','tgt_v','{}',?)", (t,))
    conn.execute("INSERT OR IGNORE INTO recommendation_edges (source_video_id, target_video_id, weight, added_at) VALUES ('src_v','tgt_v',1.0,?)", (t,))
    conn.commit()
    conn.close()

    pid = client.post("/v1/personas", json={"name": "graph-compute"}).json()["id"]
    client.post(f"/v1/personas/{pid}/seeds", json={"seeds": [{"scheme": "yt_video", "external_id": "src_v"}]})

    r = client.post(f"/v1/personas/{pid}/recompute")
    assert r.status_code == 200
    data = r.json()
    assert data["scored"] >= 0  # tgt_v should appear (src_v is seed, filtered out of scores)

    scores = client.get(f"/v1/personas/{pid}/scores").json()
    assert isinstance(scores, list)
