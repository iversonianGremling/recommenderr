"""Tests for the per-category music recommender (music_category_recs)."""
from __future__ import annotations

import sqlite3
import time

import pytest


def _seed_music(path: str) -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    now = time.time()
    # Live recommenderr adds watch_history.listen_count via migration (used by
    # both radio_library and our seed ordering); schema.sql doesn't have it, so
    # add it here to mirror the live schema the code targets.
    cols = {r[1] for r in con.execute("PRAGMA table_info(watch_history)").fetchall()}
    if "listen_count" not in cols:
        con.execute("ALTER TABLE watch_history ADD COLUMN listen_count INTEGER DEFAULT 0")
    con.execute(
        "INSERT INTO music_tag_groups (id,name,system_key,position,created_at,updated_at)"
        " VALUES (1,'Genres','g',0,?,?)", (now, now),
    )
    for tid, name in ((1, "Metal"), (2, "Jazz")):
        con.execute(
            "INSERT INTO music_tags (id,name,parent_id,position,created_at,updated_at,kind,group_id)"
            " VALUES (?,?,NULL,0,?,?,'existing',1)", (tid, name, now, now),
        )
    tracks = [
        ("m1", "Metal Song 1", "BandA", "metal"),
        ("m2", "Metal Song 2", "BandA", "metal"),
        ("m3", "Metal Song 3", "BandB", "metal"),
        ("m4", "Metal Song 4", "BandD", "metal"),   # in library, NOT tagged → a candidate
        ("j1", "Jazz Song 1", "BandC", "jazz"),
        ("j2", "Jazz Song 2", "BandC", "jazz"),
    ]
    for vid, title, artist, genre in tracks:
        con.execute(
            "INSERT INTO music_library (video_id,title,track,artist,author,album,genre,added_at)"
            " VALUES (?,?,?,?,?,?,?,?)", (vid, title, title, artist, artist, "Alb", genre, now),
        )
    # Metal tag(1) → m1,m2,m3 ; Jazz tag(2) → j1,j2  (m4 deliberately untagged)
    for tid, vid in [(1, "m1"), (1, "m2"), (1, "m3"), (2, "j1"), (2, "j2")]:
        con.execute(
            "INSERT INTO music_tag_assignments (tag_id,video_id,created_at) VALUES (?,?,?)",
            (tid, vid, now),
        )
    con.commit()
    con.close()


def test_catalog_lists_groups_and_tag_counts(tmp_db):
    _seed_music(tmp_db)
    from backend.services import music_category_recs as mcr
    mcr.init_music_category_recs_db()
    cat = mcr.list_categories()
    assert any(g["name"] == "Genres" for g in cat["groups"])
    metal = next(t for t in cat["tags"] if t["name"] == "Metal")
    assert metal["track_count"] == 3


def test_external_seed_pairs_are_distinct_artists():
    from backend.services import music_category_recs as mcr
    seeds = [
        {"artist": "BandA", "track": "s1"},
        {"artist": "BandA", "track": "s2"},   # dup artist → skipped
        {"artist": "", "track": ""},            # empty → skipped
        {"artist": "BandB", "track": "s3"},
    ]
    pairs = mcr._external_seed_pairs(seeds)
    artists = [a for _, a in pairs]
    assert artists == ["BandA", "BandB"]


@pytest.mark.asyncio
async def test_library_recs_are_genre_coherent(tmp_db, monkeypatch):
    # Disable external APIs so only the (coherence-guarded) library pool runs.
    monkeypatch.setenv("DISABLE_EXTERNAL_APIS", "1")
    _seed_music(tmp_db)
    from backend.services import music_category_recs as mcr
    mcr.init_music_category_recs_db()

    n = await mcr.compute_for_category("tag", 1)  # Metal
    recs = mcr.get_recommendations("tag", 1)
    vids = {r["video_id"] for r in recs}

    # m4 (metal, untagged) is a valid coherent neighbour and should surface.
    assert "m4" in vids
    # Jazz tracks must NOT bleed into a metal category.
    assert "j1" not in vids and "j2" not in vids
    # Seeds themselves aren't recommended back.
    assert vids.isdisjoint({"m1", "m2", "m3"})
    assert n == len(recs)


def test_merge_caps_per_artist_and_favours_external():
    from backend.services import music_category_recs as mcr
    external = [
        {"video_id": "e1", "artist": "X", "score": 1.0, "external": True},
        {"video_id": "e2", "artist": "X", "score": 0.9, "external": True},
        {"video_id": "e3", "artist": "X", "score": 0.8, "external": True},
        {"video_id": "e4", "artist": "X", "score": 0.7, "external": True},  # 4th X → capped out
    ]
    library = [{"video_id": "L1", "artist": "Y", "score": 5.0, "external": False}]
    merged = mcr._merge(external, library, limit=10)
    ids = [m["video_id"] for m in merged]
    # external first, per-artist cap (MAX_PER_ARTIST=3) drops e4, library backfills
    assert ids[:3] == ["e1", "e2", "e3"]
    assert "e4" not in ids
    assert "L1" in ids
