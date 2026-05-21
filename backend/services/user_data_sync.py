"""Sync user-data (watch history, playlist videos) from ytvideo into the local cache tables.

Called before PPR feed computations so the ppr_engine has up-to-date data
without needing a direct DB connection to ytvideo.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from backend.db import get_db

logger = logging.getLogger("user_data_sync")

YTVIDEO_URL = os.environ.get("YTVIDEO_URL", "http://127.0.0.1:9002")
RECOMMENDERR_TOKEN = os.environ.get("RECOMMENDERR_TOKEN", "")
_SYNC_TTL = 30.0  # seconds between syncs
_last_sync: float = 0.0


def _headers() -> dict[str, str]:
    if RECOMMENDERR_TOKEN:
        return {"Authorization": f"Bearer {RECOMMENDERR_TOKEN}"}
    return {}


async def sync_user_data_cache() -> None:
    global _last_sync
    now = time.monotonic()
    if now - _last_sync < _SYNC_TTL:
        return
    _last_sync = now

    try:
        async with httpx.AsyncClient(base_url=YTVIDEO_URL, timeout=httpx.Timeout(5.0, connect=1.0)) as client:
            history_resp = await client.get("/internal/history", headers=_headers())
            history_resp.raise_for_status()
            history_rows: list[dict] = history_resp.json()

            pv_resp = await client.get("/internal/playlists/videos", headers=_headers())
            pv_resp.raise_for_status()
            pv_rows: list[dict] = pv_resp.json()
    except Exception as exc:
        logger.debug("user_data_sync: could not reach ytvideo (%s) — using local cache", exc)
        return

    conn = get_db()
    try:
        conn.executemany(
            """
            INSERT INTO watch_history (video_id, title, author_id, watched_at)
            VALUES (:video_id, :title, :author_id, :watched_at)
            ON CONFLICT(video_id) DO UPDATE SET
                title=excluded.title,
                author_id=excluded.author_id,
                watched_at=excluded.watched_at
            """,
            history_rows,
        )
        conn.executemany(
            """
            INSERT INTO playlist_videos (playlist_id, video_id, title, author_id)
            VALUES (:playlist_id, :video_id, :title, :author_id)
            ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                title=excluded.title,
                author_id=excluded.author_id
            """,
            pv_rows,
        )
        conn.commit()
    except Exception as exc:
        logger.warning("user_data_sync: DB upsert failed: %s", exc)
    finally:
        conn.close()
