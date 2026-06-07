"""Real cover-art resolution for artists / albums / tracks.

Library lists and recommendation rows historically used the YouTube video
thumbnail (`/vi/{id}/...`) as their "cover", which (a) isn't a real artist photo
or album sleeve and (b) hits YouTube image hosts — risking the same rate-limiting
that throttles playback. This resolves covers from **Deezer → iTunes only** (no
Invidious / YouTube), caches them in `cover_cache`, and never blocks a request:

  * endpoints call `peek(kind, key, *terms)` — returns a cached URL or None and
    enqueues a miss for the background worker;
  * the worker drains those priority misses first (lazy-on-view), then slowly
    backfills the rest of the library so covers fill in over time.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from backend.db import get_db
from backend.services import music_client

logger = logging.getLogger("cover_art")

NEG_TTL = 7 * 24 * 3600        # re-attempt a known-missing cover after a week
BACKFILL_SLEEP = 4.0           # gap between external lookups (egress-friendly)
VALID_KINDS = ("artist", "album", "track")

# Misses enqueued by request handlers, drained by the worker first.
_priority: list[tuple] = []
_priority_seen: set[tuple[str, str]] = set()
_lock = threading.Lock()


def init_cover_cache_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cover_cache (
            kind       TEXT NOT NULL,   -- 'artist' | 'album' | 'track'
            key        TEXT NOT NULL,   -- normalized lookup key
            url        TEXT,            -- resolved image URL ('' = known-missing)
            source     TEXT,
            fetched_at REAL NOT NULL,
            PRIMARY KEY (kind, key)
        );
    """)
    conn.commit()
    conn.close()


def _norm(s) -> str:
    return (s or "").strip().lower()


def artist_key(name: str) -> str:
    return _norm(name)


def album_key(artist: str, album: str) -> str:
    return f"{_norm(artist)}::{_norm(album)}"


def track_key(artist: str, track: str) -> str:
    return f"{_norm(artist)}::{_norm(track)}"


def _img(results: list[dict] | None) -> str:
    if not results:
        return ""
    r = results[0]
    return (r.get("image") or r.get("cover_art") or "").strip()


# ── Cache I/O ──────────────────────────────────────────────

def _cache_row(kind: str, key: str):
    conn = get_db()
    row = conn.execute(
        "SELECT url, fetched_at FROM cover_cache WHERE kind=? AND key=?", (kind, key)
    ).fetchone()
    conn.close()
    return row


def _cache_put(kind: str, key: str, url: str, source: str):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO cover_cache (kind,key,url,source,fetched_at) VALUES (?,?,?,?,?)",
        (kind, key, url or "", source or "", time.time()),
    )
    conn.commit()
    conn.close()


def peek(kind: str, key: str, *terms: str) -> str | None:
    """Sync, non-blocking. Return a cached cover URL, or None and enqueue a miss
    (with the lookup `terms`) for the background worker to resolve."""
    if kind not in VALID_KINDS or not key:
        return None
    row = _cache_row(kind, key)
    if row is not None:
        if row["url"]:
            return row["url"]
        if (time.time() - row["fetched_at"]) < NEG_TTL:
            return None  # known-missing, still fresh — don't re-enqueue
    # Miss (or stale negative): enqueue for the worker.
    if terms:
        with _lock:
            if (kind, key) not in _priority_seen:
                _priority_seen.add((kind, key))
                _priority.append((kind, key, *terms))
    return None


# ── Resolution (async, hits Deezer → iTunes) ───────────────

async def _resolve(kind: str, key: str, terms: tuple) -> str:
    url, src = "", ""
    try:
        if kind == "artist":
            (name,) = terms
            url = _img(await music_client.deezer_search_artist(name, limit=1)); src = "deezer"
            if not url:
                url = _img(await music_client.itunes_search_artist(name, limit=1)); src = "itunes"
        elif kind == "album":
            artist, album = terms
            q = f"{artist} {album}".strip()
            url = _img(await music_client.deezer_search_album(q, limit=1)); src = "deezer"
            if not url:
                url = _img(await music_client.itunes_search_album(q, limit=1)); src = "itunes"
        elif kind == "track":
            artist, track = terms
            q = f"{artist} {track}".strip()
            url = _img(await music_client.deezer_search(q, limit=1)); src = "deezer"
            if not url:
                url = _img(await music_client.itunes_search(q, limit=1)); src = "itunes"
    except Exception as e:
        logger.debug("cover resolve failed %s/%s: %s", kind, key, e)
    _cache_put(kind, key, url, src if url else "")
    return url


async def resolve_now(kind: str, key: str, *terms: str) -> str | None:
    """Await a cover (cache-first). For background/worker use, not request paths."""
    if kind not in VALID_KINDS or not key:
        return None
    row = _cache_row(kind, key)
    if row is not None and (row["url"] or (time.time() - row["fetched_at"]) < NEG_TTL):
        return row["url"] or None
    url = await _resolve(kind, key, terms)
    return url or None


# ── Backfill worker ────────────────────────────────────────

def _next_priority() -> tuple | None:
    with _lock:
        if not _priority:
            return None
        item = _priority.pop(0)
        _priority_seen.discard((item[0], item[1]))
        return item


def _library_backfill_targets(limit: int = 40) -> list[tuple]:
    """Distinct library artists + albums lacking a cached cover."""
    conn = get_db()
    try:
        targets: list[tuple] = []
        arows = conn.execute("""
            SELECT DISTINCT COALESCE(NULLIF(artist,''), author) AS a
            FROM music_library
            WHERE COALESCE(NULLIF(artist,''), author) IS NOT NULL
            LIMIT 4000
        """).fetchall()
        for r in arows:
            name = r["a"]
            if not name:
                continue
            k = artist_key(name)
            if not _cache_row("artist", k):
                targets.append(("artist", k, name))
            if len(targets) >= limit:
                return targets
        brows = conn.execute("""
            SELECT DISTINCT COALESCE(NULLIF(artist,''), author) AS a, album AS b
            FROM music_library
            WHERE COALESCE(album,'') <> ''
            LIMIT 4000
        """).fetchall()
        for r in brows:
            if not r["a"] or not r["b"]:
                continue
            k = album_key(r["a"], r["b"])
            if not _cache_row("album", k):
                targets.append(("album", k, r["a"], r["b"]))
            if len(targets) >= limit:
                break
        return targets
    finally:
        conn.close()


async def cover_backfill_worker():
    logger.info("Cover-art backfill worker started")
    await asyncio.sleep(12)
    pending: list[tuple] = []
    while True:
        try:
            # Priority misses (lazy-on-view) first.
            item = _next_priority()
            if item is None:
                if not pending:
                    pending = await asyncio.to_thread(_library_backfill_targets, 40)
                item = pending.pop(0) if pending else None
            if item is None:
                await asyncio.sleep(30.0)
                continue
            kind, key, *terms = item
            await _resolve(kind, key, tuple(terms))
            await asyncio.sleep(BACKFILL_SLEEP)
        except asyncio.CancelledError:
            logger.info("Cover-art backfill worker stopped")
            raise
        except Exception as e:
            logger.error("cover backfill loop error: %s", e)
            await asyncio.sleep(10.0)
