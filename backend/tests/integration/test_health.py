def test_health_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "recommenderr"
    assert body["status"] == "ok"
    assert body["schema_version"] == 1


def test_schema_applied(tmp_db):
    import sqlite3

    con = sqlite3.connect(tmp_db)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    finally:
        con.close()
    names = {r[0] for r in rows}
    expected_subset = {
        "video_metadata",
        "recommendation_edges",
        "ppr_scores",
        "music_library",
        "music_jobs",
        "recognition_cache",
        "artist_release_events",
        "video_media_overrides",
        "weight_rules",
    }
    assert expected_subset.issubset(names), names
