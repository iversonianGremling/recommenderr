"""Unit tests for migrate_to_items_v1."""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from backend.services.migration import migrate_to_items_v1


@pytest.fixture
def conn(tmp_db):
    c = sqlite3.connect(tmp_db)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    yield c
    c.close()


def _seed(conn, *, videos=0, tracks=0, albums=0, artists=0):
    now = time.time()
    for i in range(videos):
        conn.execute(
            "INSERT OR IGNORE INTO video_metadata (video_id, fetched_at) VALUES (?, ?)",
            (f"vid{i}", now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO feed_recommendations "
            "(video_id, title, author, author_id, source_video_id, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"vid{i}", f"Video {i}", f"Channel {i}", f"ch{i}", "src", now),
        )
    for i in range(tracks):
        conn.execute(
            "INSERT OR IGNORE INTO music_library "
            "(video_id, track, artist, album, added_at) VALUES (?, ?, ?, ?, ?)",
            (f"vid{i}", f"Track {i}", f"Artist {i}", f"Album {i}", now),
        )
    for i in range(albums):
        conn.execute(
            "INSERT OR IGNORE INTO album_ratings "
            "(album_key, album_title, rating, rated_at) VALUES (?, ?, ?, ?)",
            (f"album{i}", f"Album Title {i}", 8, now),
        )
    for i in range(artists):
        conn.execute(
            "INSERT OR IGNORE INTO artist_follows "
            "(artist_key, artist_name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (f"artist{i}", f"Artist {i}", now, now),
        )
    conn.commit()


def test_schemes_seeded(conn):
    migrate_to_items_v1(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM schemes").fetchall()}
    assert {"yt_video", "music_track", "music_album", "music_artist"} == names


def test_yt_video_backfill(conn):
    _seed(conn, videos=5)
    migrate_to_items_v1(conn)
    count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE scheme='yt_video'"
    ).fetchone()[0]
    assert count == 5


def test_music_track_backfill_and_alias(conn):
    _seed(conn, videos=3, tracks=3)
    migrate_to_items_v1(conn)

    count = conn.execute(
        "SELECT COUNT(*) FROM items WHERE scheme='music_track'"
    ).fetchone()[0]
    assert count == 3

    # Each track should have a yt_video alias
    alias_count = conn.execute(
        "SELECT COUNT(*) FROM item_aliases WHERE alias_scheme='yt_video'"
    ).fetchone()[0]
    assert alias_count == 3


def test_album_artist_backfill(conn):
    _seed(conn, albums=2, artists=4)
    migrate_to_items_v1(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE scheme='music_album'"
    ).fetchone()[0] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE scheme='music_artist'"
    ).fetchone()[0] == 4


def test_idempotent(conn):
    _seed(conn, videos=3, tracks=2)
    migrate_to_items_v1(conn)
    migrate_to_items_v1(conn)  # second call is a no-op (schema_version >= 2)

    assert conn.execute(
        "SELECT COUNT(*) FROM items WHERE scheme='yt_video'"
    ).fetchone()[0] == 3


def test_schema_version_bumped(conn):
    migrate_to_items_v1(conn)
    ver = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert ver >= 2


def test_metadata_json_valid(conn):
    _seed(conn, videos=1)
    migrate_to_items_v1(conn)
    row = conn.execute(
        "SELECT metadata_json FROM items WHERE scheme='yt_video'"
    ).fetchone()
    meta = json.loads(row["metadata_json"])
    assert "title" in meta
