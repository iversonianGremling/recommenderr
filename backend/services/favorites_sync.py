"""Bridge ratings -> tags: project the user's favorites (highly-rated albums in
ytmusic's album_ratings — sourced from yamtrack, RateYourMusic, manual, …) onto
the ytmusic music-tag system as membership of a "Favorites" tag.

This keeps ratings and tags as two complementary signals (the user wants both):
ratings stay where they are; this only *adds* tag assignments (INSERT OR IGNORE),
never removing existing curation. It is idempotent and re-runnable, so as the
user downloads their rated albums into the library, re-running picks up the new
tracks automatically.

Matching is name-based against music_library:
  - album match  : library track whose (artist, album) equals a rated album
  - artist match : library track by an artist who has any album rated >= min
Album matches are precise; artist matches capture "favorite artists" more
broadly (opt out with include_artist=False).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from collections import defaultdict

from backend.db import get_db
from backend.services.music_tags import create_music_tag, manual_assign_music_tags

logger = logging.getLogger("favorites_sync")

FAVORITES_TAG_NAME = "Favorites"
FAVORITES_GROUP_ID = 1  # "General"


def _norm(v: str | None) -> str:
    if not v:
        return ""
    v = v.lower()
    v = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", v)
    v = re.sub(r"[^a-z0-9]+", " ", v)
    return " ".join(v.split())


def _find_or_create_tag(name: str, group_id: int) -> int:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM music_tags WHERE name=? AND group_id=? ORDER BY id LIMIT 1",
            (name, group_id),
        ).fetchone()
        if row:
            return int(row["id"])
    finally:
        conn.close()
    return create_music_tag(name, parent_id=None, group_id=group_id, kind="new")


def _load_favorites(min_rating: int) -> list[tuple[str, str]]:
    ytdb = os.getenv("YTMUSIC_DB_PATH", "/opt/ytmusic/data/ytmusic.db")
    yc = sqlite3.connect(f"file:{ytdb}?mode=ro", uri=True, timeout=10)
    yc.row_factory = sqlite3.Row
    try:
        rows = yc.execute(
            "SELECT album_title, album_artist FROM album_ratings WHERE rating >= ?",
            (min_rating,),
        ).fetchall()
    finally:
        yc.close()
    return [((r["album_artist"] or ""), (r["album_title"] or "")) for r in rows]


def sync_favorites_tag(
    min_rating: int = 8,
    include_artist: bool = True,
    tag_name: str = FAVORITES_TAG_NAME,
    group_id: int = FAVORITES_GROUP_ID,
) -> dict:
    """Tag library tracks that belong to the user's favorite albums/artists.

    Returns counts: favorites, matched-by-album, matched-by-artist, newly
    assigned, total in tag.
    """
    favorites = _load_favorites(min_rating)
    fav_album_keys = {(_norm(a), _norm(t)) for a, t in favorites if _norm(t)}
    fav_artists = {_norm(a) for a, _ in favorites if _norm(a)}

    tag_id = _find_or_create_tag(tag_name, group_id)

    conn = get_db()
    try:
        lib = conn.execute(
            "SELECT video_id, artist, album, author FROM music_library"
        ).fetchall()

        by_album_match: set[str] = set()
        by_artist_match: set[str] = set()
        for r in lib:
            a = _norm(r["artist"] or r["author"] or "")
            alb = _norm(r["album"] or "")
            if a and alb and (a, alb) in fav_album_keys:
                by_album_match.add(r["video_id"])
            elif include_artist and a and a in fav_artists:
                by_artist_match.add(r["video_id"])

        targets = by_album_match | by_artist_match

        already = {
            row["video_id"]
            for row in conn.execute(
                "SELECT video_id FROM music_tag_assignments WHERE tag_id=?", (tag_id,)
            ).fetchall()
        }
        new_targets = targets - already
        for vid in new_targets:
            manual_assign_music_tags(conn, vid, [tag_id])
        conn.commit()

        total_in_tag = conn.execute(
            "SELECT COUNT(*) FROM music_tag_assignments WHERE tag_id=?", (tag_id,)
        ).fetchone()[0]
    finally:
        conn.close()

    result = {
        "ok": True,
        "tag_id": tag_id,
        "tag_name": tag_name,
        "min_rating": min_rating,
        "favorites": len(favorites),
        "matched_by_album": len(by_album_match),
        "matched_by_artist": len(by_artist_match),
        "newly_assigned": len(new_targets),
        "total_in_tag": total_in_tag,
    }
    logger.info("sync_favorites_tag: %s", result)
    return result
