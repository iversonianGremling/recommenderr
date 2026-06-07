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
# Signature of the last category snapshot we mirrored, so we skip the (lock-heavy)
# full-table rewrite on the common case where nothing changed between syncs.
_last_category_sig: str | None = None


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


def _category_membership(conn) -> dict[int, set]:
    """category_id -> set of member keys (videos / channels / tags), for change
    detection so we only recompute recs for categories that actually changed."""
    membership: dict[int, set] = {}
    for tbl, col in (
        ("video_category_assignments", "video_id"),
        ("channel_category_assignments", "channel_id"),
        ("category_tags", "tag_id"),
    ):
        for r in conn.execute(f"SELECT category_id, {col} AS k FROM {tbl}").fetchall():
            membership.setdefault(r["category_id"], set()).add((tbl, r["k"]))
    return membership


def _sync_categories_data(snapshot: dict) -> int:
    """Mirror ytvideo's category snapshot into recommenderr (IDs preserved) so the
    category_recs worker has data to compute against. Marks changed categories dirty."""
    global _last_category_sig
    import hashlib
    import json
    from backend.services import category_recs

    # Skip the lock-heavy rewrite when the snapshot is byte-identical to last time.
    sig = hashlib.md5(
        json.dumps(snapshot, sort_keys=True, default=str).encode()
    ).hexdigest()
    if sig == _last_category_sig:
        return len(snapshot.get("categories", []) or [])

    cats = snapshot.get("categories", []) or []
    tags = snapshot.get("tags", []) or []
    va = snapshot.get("video_assignments", []) or []
    ca = snapshot.get("channel_assignments", []) or []
    ct = snapshot.get("category_tags", []) or []
    vt = snapshot.get("video_tags", []) or []
    now = time.time()

    conn = get_db()
    try:
        before = _category_membership(conn)

        # Replace categories + tags, preserving ytvideo's IDs so assignment FKs and
        # cached recommendations (keyed by category_id) stay valid.
        conn.execute("DELETE FROM categories")
        conn.executemany(
            "INSERT INTO categories (id, name, parent_id, description, created_at) VALUES (?,?,?,?,?)",
            [(c["id"], c["name"], c.get("parent_id"), c.get("description") or "", now) for c in cats],
        )
        conn.execute("DELETE FROM tags")
        conn.executemany(
            "INSERT INTO tags (id, name, description, created_at) VALUES (?,?,?,?)",
            [(t["id"], t["name"], "", now) for t in tags],
        )

        # Replace assignment tables wholesale (snapshot is authoritative).
        for tbl, rows, cols in (
            ("video_category_assignments", va, ("video_id", "category_id")),
            ("channel_category_assignments", ca, ("channel_id", "category_id")),
            ("category_tags", ct, ("category_id", "tag_id")),
            ("video_tags", vt, ("video_id", "tag_id")),
        ):
            conn.execute(f"DELETE FROM {tbl}")
            conn.executemany(
                f"INSERT OR IGNORE INTO {tbl} ({cols[0]}, {cols[1]}) VALUES (?,?)",
                [(r[cols[0]], r[cols[1]]) for r in rows],
            )
        conn.commit()

        after = _category_membership(conn)
    finally:
        conn.close()

    changed = {cid for cid in (set(before) | set(after)) if before.get(cid) != after.get(cid)}
    for cid in changed:
        category_recs.mark_dirty(cid)
    if changed:
        logger.debug("category sync: %d categories changed → marked dirty", len(changed))
    _last_category_sig = sig
    return len(cats)


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
                # Category definitions/assignments ride along the same ytvideo
                # source so the category_recs worker has fresh data to rank.
                try:
                    cresp = await client.get("/internal/categories", headers=headers)
                    cresp.raise_for_status()
                    _sync_categories_data(cresp.json())
                except Exception as exc:
                    logger.warning("category sync failed for %s: %s", source.get("name"), exc)

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
