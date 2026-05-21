"""One-shot idempotent migration: backfill existing domain tables into items/schemes."""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger("migration")

# ---------------------------------------------------------------------------
# Built-in scheme declarations
# ---------------------------------------------------------------------------

_SCHEMES: list[dict] = [
    {
        "name": "yt_video",
        "display_name": "YouTube Video",
        "description": "Video fetched via Invidious or yt-dlp",
        "fields": [
            {"name": "title",        "type": "text",      "label": "Title",    "required": True},
            {"name": "author",       "type": "text",      "label": "Channel"},
            {"name": "author_id",    "type": "text",      "label": "Channel ID"},
            {"name": "thumbnail",    "type": "url-image", "label": "Thumbnail"},
            {"name": "duration",     "type": "duration",  "label": "Duration"},
            {"name": "published_at", "type": "date",      "label": "Published"},
            {"name": "genre",        "type": "text",      "label": "Genre"},
        ],
    },
    {
        "name": "music_track",
        "display_name": "Music Track",
        "description": "Track from music_library, recognised via multi-source lookup",
        "fields": [
            {"name": "title",    "type": "text",      "label": "Title",   "required": True},
            {"name": "artist",   "type": "text",      "label": "Artist"},
            {"name": "album",    "type": "text",      "label": "Album"},
            {"name": "genre",    "type": "text",      "label": "Genre"},
            {"name": "isrc",     "type": "text",      "label": "ISRC"},
            {"name": "duration", "type": "duration",  "label": "Duration"},
            {"name": "cover_art","type": "url-image", "label": "Cover"},
        ],
    },
    {
        "name": "music_album",
        "display_name": "Music Album",
        "description": "Album from album_ratings",
        "fields": [
            {"name": "title",        "type": "text",      "label": "Title",  "required": True},
            {"name": "artist",       "type": "text",      "label": "Artist"},
            {"name": "cover_art",    "type": "url-image", "label": "Cover"},
            {"name": "source",       "type": "text",      "label": "Source"},
            {"name": "rating",       "type": "number",    "label": "Rating"},
            {"name": "release_date", "type": "date",      "label": "Released"},
        ],
    },
    {
        "name": "music_artist",
        "display_name": "Music Artist",
        "description": "Artist from artist_follows",
        "fields": [
            {"name": "name",               "type": "text",      "label": "Name",    "required": True},
            {"name": "image",              "type": "url-image", "label": "Image"},
            {"name": "source",             "type": "text",      "label": "Source"},
            {"name": "spotify_artist_id",  "type": "text",      "label": "Spotify ID"},
            {"name": "deezer_artist_id",   "type": "text",      "label": "Deezer ID"},
            {"name": "itunes_artist_id",   "type": "text",      "label": "iTunes ID"},
        ],
    },
]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_to_items_v1(conn) -> None:
    """
    Idempotent backfill: seed schemes + populate items from existing domain tables.
    conn must already have foreign_keys=ON and be committed after.
    Only runs if schema_version < 2.
    """
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] else 0
    if current >= 2:
        logger.debug("migrate_to_items_v1: already at version %d, skipping", current)
        return

    logger.info("migrate_to_items_v1: starting (current version=%d)", current)
    t0 = time.monotonic()

    _seed_schemes(conn)
    n_yt      = _backfill_yt_video(conn)
    n_music   = _backfill_music_track(conn)
    n_albums  = _backfill_music_album(conn)
    n_artists = _backfill_music_artist(conn)

    conn.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (2, ?)",
        (time.time(),),
    )
    conn.commit()

    elapsed = time.monotonic() - t0
    logger.info(
        "migrate_to_items_v1: done in %.1fs — yt_video=%d music_track=%d music_album=%d music_artist=%d",
        elapsed, n_yt, n_music, n_albums, n_artists,
    )


def _seed_schemes(conn) -> None:
    for s in _SCHEMES:
        conn.execute(
            """
            INSERT INTO schemes (name, display_name, description, fields_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                display_name = excluded.display_name,
                description  = excluded.description,
                fields_json  = excluded.fields_json
            """,
            (s["name"], s["display_name"], s.get("description", ""),
             json.dumps(s["fields"]), time.time()),
        )


def _backfill_yt_video(conn) -> int:
    """Populate items from video_metadata, enriching title/author from crawled rows."""
    rows = conn.execute(
        """
        SELECT
            vm.video_id,
            vm.genre,
            COALESCE(fr.title, wh.title, pv.title, vm.video_id) AS title,
            fr.author                                             AS author,
            COALESCE(fr.author_id, wh.author_id, pv.author_id)  AS author_id,
            fr.thumbnail                                          AS thumbnail,
            fr.duration                                           AS duration,
            fr.published_at                                       AS published_at,
            vm.fetched_at
        FROM video_metadata vm
        LEFT JOIN (
            SELECT video_id, title, author, author_id, thumbnail, duration, published_at
            FROM feed_recommendations
            GROUP BY video_id
        ) fr ON fr.video_id = vm.video_id
        LEFT JOIN watch_history wh ON wh.video_id = vm.video_id
        LEFT JOIN (
            SELECT video_id, title, author_id
            FROM playlist_videos
            GROUP BY video_id
        ) pv ON pv.video_id = vm.video_id
        """,
    ).fetchall()

    now = time.time()
    count = 0
    for r in rows:
        meta = {
            "title":        r["title"],
            "author":       r["author"],
            "author_id":    r["author_id"],
            "thumbnail":    r["thumbnail"],
            "duration":     r["duration"],
            "published_at": r["published_at"],
            "genre":        r["genre"],
        }
        conn.execute(
            """
            INSERT INTO items (scheme, external_id, metadata_json, added_at)
            VALUES ('yt_video', ?, ?, ?)
            ON CONFLICT(scheme, external_id) DO NOTHING
            """,
            (r["video_id"], json.dumps(meta), r["fetched_at"] or now),
        )
        count += 1

    return count


def _backfill_music_track(conn) -> int:
    """Populate items from music_library + add yt_video alias for each."""
    rows = conn.execute(
        """
        SELECT id, video_id, track, artist, album, genre, thumbnail, duration, added_at
        FROM music_library
        """
    ).fetchall()

    now = time.time()
    count = 0
    for r in rows:
        # Use "ml:<id>" as the external_id so it's stable even if video_id is reused
        ext_id = f"ml:{r['id']}"
        meta = {
            "title":    r["track"] or r["video_id"],
            "artist":   r["artist"],
            "album":    r["album"],
            "genre":    r["genre"],
            "cover_art": r["thumbnail"],
            "duration": r["duration"],
        }
        conn.execute(
            """
            INSERT INTO items (scheme, external_id, metadata_json, added_at)
            VALUES ('music_track', ?, ?, ?)
            ON CONFLICT(scheme, external_id) DO NOTHING
            """,
            (ext_id, json.dumps(meta), r["added_at"] or now),
        )
        # Link the music_track to its yt_video item via an alias on the yt_video side
        item_row = conn.execute(
            "SELECT id FROM items WHERE scheme='music_track' AND external_id=?",
            (ext_id,),
        ).fetchone()
        if item_row and r["video_id"]:
            conn.execute(
                """
                INSERT OR IGNORE INTO item_aliases (item_id, alias_scheme, alias_external_id)
                VALUES (?, 'yt_video', ?)
                """,
                (item_row["id"], r["video_id"]),
            )
        count += 1

    return count


def _backfill_music_album(conn) -> int:
    rows = conn.execute(
        "SELECT album_key, album_title, album_artist, cover_art, source, rating, rated_at FROM album_ratings"
    ).fetchall()

    now = time.time()
    count = 0
    for r in rows:
        meta = {
            "title":   r["album_title"],
            "artist":  r["album_artist"],
            "cover_art": r["cover_art"],
            "source":  r["source"],
            "rating":  r["rating"],
        }
        conn.execute(
            """
            INSERT INTO items (scheme, external_id, metadata_json, added_at)
            VALUES ('music_album', ?, ?, ?)
            ON CONFLICT(scheme, external_id) DO NOTHING
            """,
            (r["album_key"], json.dumps(meta), r["rated_at"] or now),
        )
        count += 1

    return count


def _backfill_music_artist(conn) -> int:
    rows = conn.execute(
        """
        SELECT artist_key, artist_name, image, source,
               spotify_artist_id, deezer_artist_id, itunes_artist_id, created_at
        FROM artist_follows
        """
    ).fetchall()

    now = time.time()
    count = 0
    for r in rows:
        meta = {
            "name":              r["artist_name"],
            "image":             r["image"],
            "source":            r["source"],
            "spotify_artist_id": r["spotify_artist_id"],
            "deezer_artist_id":  r["deezer_artist_id"],
            "itunes_artist_id":  r["itunes_artist_id"],
        }
        conn.execute(
            """
            INSERT INTO items (scheme, external_id, metadata_json, added_at)
            VALUES ('music_artist', ?, ?, ?)
            ON CONFLICT(scheme, external_id) DO NOTHING
            """,
            (r["artist_key"], json.dumps(meta), r["created_at"] or now),
        )
        count += 1

    return count
