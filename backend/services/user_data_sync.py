"""Sync user-data (watch history, playlist videos) from configured signal sources.

Called before PPR feed computations so the ppr_engine has up-to-date data
without needing a direct DB connection to ytvideo.
"""
from __future__ import annotations

import logging
import time

import httpx

from backend.db import get_db

logger = logging.getLogger("user_data_sync")

_SYNC_TTL = 30.0
_last_sync: float = 0.0


def _upsert_watch_history(rows: list[dict]) -> int:
    conn = get_db()
    try:
        conn.executemany(
            """INSERT INTO watch_history (video_id, title, author_id, watched_at)
               VALUES (:video_id, :title, :author_id, :watched_at)
               ON CONFLICT(video_id) DO UPDATE SET
                   title=excluded.title,
                   author_id=excluded.author_id,
                   watched_at=excluded.watched_at""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _upsert_playlist_videos(rows: list[dict]) -> int:
    conn = get_db()
    try:
        conn.executemany(
            """INSERT INTO playlist_videos (playlist_id, video_id, title, author_id)
               VALUES (:playlist_id, :video_id, :title, :author_id)
               ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                   title=excluded.title,
                   author_id=excluded.author_id""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _update_source_result(source_id: int, count: int | None, error: str | None) -> None:
    conn = get_db()
    try:
        conn.execute(
            "UPDATE signal_sources SET last_synced_at=?, last_count=?, last_error=? WHERE id=?",
            (time.time(), count, error, source_id),
        )
        conn.commit()
    finally:
        conn.close()


async def sync_source(source: dict) -> dict:
    """Fetch + upsert data for one signal source. Returns {ok, count} or {ok, error}."""
    headers: dict[str, str] = {}
    if source.get("auth_header"):
        headers["Authorization"] = source["auth_header"]

    try:
        async with httpx.AsyncClient(
            base_url=source["endpoint_url"],
            timeout=httpx.Timeout(10.0, connect=2.0),
        ) as client:
            converter = source.get("converter", "ytfront_v1")

            if converter == "ytfront_v1":
                resp = await client.get("/internal/history", headers=headers)
                resp.raise_for_status()
                count = _upsert_watch_history(resp.json())

            elif converter == "ytfront_likes_v1":
                resp = await client.get("/internal/playlists/videos", headers=headers)
                resp.raise_for_status()
                count = _upsert_playlist_videos(resp.json())

            else:  # native: expects {watch_history: [...], playlist_videos: [...]}
                resp = await client.get("/api/signals", headers=headers)
                resp.raise_for_status()
                data = resp.json()
                count = 0
                if "watch_history" in data:
                    count += _upsert_watch_history(data["watch_history"])
                if "playlist_videos" in data:
                    count += _upsert_playlist_videos(data["playlist_videos"])

        _update_source_result(source["id"], count, None)
        logger.debug("sync_source %s: %d rows", source["name"], count)
        return {"ok": True, "count": count}

    except Exception as exc:
        msg = str(exc)
        _update_source_result(source["id"], None, msg)
        logger.debug("sync_source %s: error — %s", source["name"], msg)
        return {"ok": False, "error": msg}


async def sync_user_data_cache() -> None:
    global _last_sync
    now = time.monotonic()
    if now - _last_sync < _SYNC_TTL:
        return
    _last_sync = now

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM signal_sources WHERE enabled=1"
        ).fetchall()
        sources = [dict(r) for r in rows]
    except Exception:
        # Table may not exist yet on first-boot before migration runs
        sources = []
    finally:
        conn.close()

    for source in sources:
        try:
            await sync_source(source)
        except Exception as exc:
            logger.warning("sync_user_data_cache: source %s failed: %s", source.get("name"), exc)
