"""Radio service: iterative music discovery via Bandcamp, Last.fm/Deezer, and YouTube music search.

Seeds are (track, artist) pairs. The service expands outward via:
  1. Bandcamp: find the seed's album → scrape sidebar "you may also like" → resolve to YouTube
  2. Music APIs: Last.fm similar tracks + Deezer related artists (known-music sources)
  3. YouTube/Invidious: search filtered to music-confirmed videos (Topic channels & canonical patterns)

All discovered videos are cached in radio_graph_cache (48h TTL). Bandcamp album URL lookups
are cached separately with a longer TTL (168h / 1 week).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque

from backend.db import get_db
from backend.services.bandcamp_recommendations import (
    bandcamp_sidebar_to_music_recommendation_rows,
    get_shared_bandcamp_recommender,
)
from backend.services.invidious_client import api_get
from backend.services.music_client import (
    bandcamp_search_albums,
    deezer_get_related_artists,
    deezer_search,
    lastfm_get_similar_tracks,
)
from backend.services.music_recommendations import (
    CANONICAL_MUSIC_VIDEO_RE,
    NON_MUSIC_VIDEO_RE,
    track_identity_key,
    try_resolve_youtube_match,
)

logger = logging.getLogger("radio_service")

CACHE_TTL_HOURS = float(os.getenv("RADIO_CACHE_TTL_HOURS", "48"))
BANDCAMP_TTL_HOURS = float(os.getenv("RADIO_BANDCAMP_TTL_HOURS", "168"))
MAX_SEEDS = int(os.getenv("RADIO_MAX_SEEDS", "5"))
MAX_HOPS = 2

# Circuit-breakers: mark yt-dlp/Invidious as unavailable after failures to avoid long hangs.
_ytdlp_failed_until: float = 0.0
_YTDLP_BACKOFF_SECS = 120.0

_invidious_failed_until: float = 0.0
_INVIDIOUS_BACKOFF_SECS = 60.0
_invidious_fail_streak: int = 0
_INVIDIOUS_FAIL_THRESHOLD = 2  # trip after this many consecutive failures


def _ytdlp_available() -> bool:
    return time.time() > _ytdlp_failed_until


def _mark_ytdlp_failed() -> None:
    global _ytdlp_failed_until
    _ytdlp_failed_until = time.time() + _YTDLP_BACKOFF_SECS
    logger.info("yt-dlp circuit-breaker: marked unavailable for %.0fs", _YTDLP_BACKOFF_SECS)


def _invidious_available() -> bool:
    return time.time() > _invidious_failed_until


def _mark_invidious_failure() -> None:
    global _invidious_failed_until, _invidious_fail_streak
    _invidious_fail_streak += 1
    if _invidious_fail_streak >= _INVIDIOUS_FAIL_THRESHOLD:
        _invidious_failed_until = time.time() + _INVIDIOUS_BACKOFF_SECS
        logger.info(
            "Invidious circuit-breaker: tripped after %d failures, unavailable for %.0fs",
            _invidious_fail_streak, _INVIDIOUS_BACKOFF_SECS,
        )


def _mark_invidious_ok() -> None:
    global _invidious_fail_streak
    _invidious_fail_streak = 0

_MUSIC_API_SOURCES = frozenset({"spotify", "deezer", "lastfm", "itunes", "bandcamp"})


# ── Music confirmation ──────────────────────────────────────────────────────

def _is_confirmed_music(video: dict) -> bool:
    """True if the video is almost certainly a music track, not a video about music."""
    title = video.get("title") or ""
    author = video.get("author") or ""

    if NON_MUSIC_VIDEO_RE.search(title) or NON_MUSIC_VIDEO_RE.search(author):
        return False
    if author.lower().endswith(" - topic"):
        return True
    if CANONICAL_MUSIC_VIDEO_RE.search(title):
        return True
    if video.get("is_music_confirmed"):
        return True

    sources = video.get("sources") or []
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except Exception:
            sources = [s.strip() for s in sources.split(",") if s.strip()]
    if any(s in _MUSIC_API_SOURCES for s in sources):
        return True

    return False


# ── Cache helpers ───────────────────────────────────────────────────────────

def _cache_get(seed_key: str) -> list[dict] | None:
    cutoff = time.time() - CACHE_TTL_HOURS * 3600
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT track, artist, video_id, title, thumbnail, duration,
                      author, author_id, sources, score, is_music_confirmed
               FROM radio_graph_cache
               WHERE seed_key = ? AND fetched_at > ?
               ORDER BY score DESC""",
            (seed_key, cutoff),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    out = []
    for r in rows:
        sources = []
        try:
            sources = json.loads(r["sources"]) if r["sources"] else []
        except Exception:
            pass
        out.append({
            "track": r["track"], "artist": r["artist"], "video_id": r["video_id"],
            "title": r["title"], "thumbnail": r["thumbnail"], "duration": r["duration"],
            "author": r["author"], "author_id": r["author_id"],
            "sources": sources, "score": r["score"],
            "is_music_confirmed": bool(r["is_music_confirmed"]),
        })
    return out


def _cache_store(seed_key: str, recs: list[dict]) -> None:
    if not recs:
        return
    now = time.time()
    conn = get_db()
    try:
        for rec in recs:
            vid = (rec.get("video_id") or "").strip()
            if not vid:
                continue
            sources = rec.get("sources") or []
            if isinstance(sources, str):
                try:
                    sources = json.loads(sources)
                except Exception:
                    sources = [s.strip() for s in sources.split(",") if s.strip()]
            conn.execute(
                """INSERT INTO radio_graph_cache
                       (seed_key, track, artist, video_id, title, thumbnail, duration,
                        author, author_id, sources, score, is_music_confirmed, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(seed_key, video_id) DO UPDATE SET
                       title=excluded.title, thumbnail=excluded.thumbnail,
                       duration=excluded.duration, author=excluded.author,
                       author_id=excluded.author_id, sources=excluded.sources,
                       score=excluded.score,
                       is_music_confirmed=excluded.is_music_confirmed,
                       fetched_at=excluded.fetched_at""",
                (
                    seed_key, rec.get("track", ""), rec.get("artist", ""), vid,
                    rec.get("title"), rec.get("thumbnail"),
                    rec.get("duration") or rec.get("lengthSeconds"),
                    rec.get("author"), rec.get("author_id"),
                    json.dumps(sources), rec.get("score", 0.5),
                    1 if _is_confirmed_music(rec) else 0, now,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _bc_lookup_get(seed_key: str) -> dict | None:
    cutoff = time.time() - BANDCAMP_TTL_HOURS * 3600
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT bandcamp_url FROM radio_bandcamp_lookup WHERE seed_key = ? AND fetched_at > ?",
            (seed_key, cutoff),
        ).fetchone()
    finally:
        conn.close()
    return {"bandcamp_url": row["bandcamp_url"]} if row else None


def _bc_lookup_store(seed_key: str, bandcamp_url: str | None) -> None:
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO radio_bandcamp_lookup (seed_key, bandcamp_url, fetched_at)
               VALUES (?,?,?)
               ON CONFLICT(seed_key) DO UPDATE SET
                   bandcamp_url=excluded.bandcamp_url, fetched_at=excluded.fetched_at""",
            (seed_key, bandcamp_url, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


# ── Source fetchers ─────────────────────────────────────────────────────────

def _ytdlp_search_sync(q: str, n: int = 20) -> list[dict]:
    """Blocking yt-dlp ytsearch (IPv6-forced) — run in executor."""
    if not _ytdlp_available():
        return []
    import yt_dlp  # lazy import; only used here
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "socket_timeout": 8,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.extract_info(f"ytsearch{n}:{q}", download=False)
            entries = result.get("entries", []) if result else []
            if not entries:
                _mark_ytdlp_failed()
            return entries
    except Exception:
        _mark_ytdlp_failed()
        return []


def _shape_ytdlp_entry(entry: dict, track: str, artist: str) -> dict:
    return {
        "track": track, "artist": artist,
        "video_id": entry.get("id", ""),
        "title": entry.get("title", ""),
        "author": entry.get("uploader", "") or entry.get("channel", ""),
        "author_id": entry.get("uploader_id") or entry.get("channel_id"),
        "thumbnail": entry.get("thumbnail"),
        "duration": entry.get("duration"),
        "sources": ["youtube"], "score": 0.45,
    }


async def _youtube_music_search(track: str, artist: str) -> list[dict]:
    """Search YouTube for music-confirmed videos. Tries Invidious first, falls back to yt-dlp."""
    q = f"{artist} {track}".strip()
    if not q:
        return []

    # Try Invidious (fast when working)
    if _invidious_available():
        try:
            results = await asyncio.wait_for(api_get("/search", {"q": q, "type": "video"}), timeout=5.0)
            _mark_invidious_ok()
            if isinstance(results, list):
                out = []
                for v in results[:20]:
                    if not isinstance(v, dict):
                        continue
                    rec = {
                        "track": track, "artist": artist,
                        "video_id": v.get("videoId", ""),
                        "title": v.get("title", ""),
                        "author": v.get("author", ""),
                        "author_id": v.get("authorId"),
                        "thumbnail": ((v.get("videoThumbnails") or [{}])[0]).get("url"),
                        "duration": v.get("lengthSeconds"),
                        "sources": ["youtube"], "score": 0.45,
                    }
                    if _is_confirmed_music(rec):
                        rec["score"] = 0.72
                        rec["is_music_confirmed"] = True
                        out.append(rec)
                if out:
                    return out
        except Exception:
            _mark_invidious_failure()

    # Fallback: yt-dlp direct search (IPv6; 10s total budget)
    if not _ytdlp_available():
        return []
    try:
        loop = asyncio.get_running_loop()
        entries = await asyncio.wait_for(
            loop.run_in_executor(None, _ytdlp_search_sync, q, 20),
            timeout=10.0,
        )
        out = []
        for entry in (entries or []):
            if not entry:
                continue
            rec = _shape_ytdlp_entry(entry, track, artist)
            if _is_confirmed_music(rec):
                rec["score"] = 0.72
                rec["is_music_confirmed"] = True
                out.append(rec)
        return out
    except asyncio.TimeoutError:
        _mark_ytdlp_failed()
        return []
    except Exception:
        return []


async def _api_music_recs(track: str, artist: str, limit: int = 8) -> list[dict]:
    """Last.fm similar tracks + Deezer related artists — both confirmed-music sources."""

    async def _lastfm() -> list[dict]:
        try:
            items = await lastfm_get_similar_tracks(track, artist, limit=limit)
            return [
                {
                    "track": i.get("track", ""), "artist": i.get("artist", ""),
                    "sources": ["lastfm"], "score": 0.82,
                    "is_music_confirmed": True,
                    "video_id": "", "title": "", "author": "",
                }
                for i in items if i.get("track") or i.get("artist")
            ]
        except Exception:
            return []

    async def _deezer() -> list[dict]:
        try:
            seed = await deezer_search(f"{artist} {track}", limit=1)
            if not seed:
                return []
            artist_id = seed[0].get("deezer_artist_id")
            if not artist_id:
                return []
            related = await deezer_get_related_artists(artist_id, limit=3)
            rows: list[dict] = []
            for rel in related[:2]:
                hits = await deezer_search(rel.get("artist", ""), limit=1)
                for h in hits:
                    if h.get("track") or h.get("artist"):
                        rows.append({
                            "track": h.get("track", ""), "artist": h.get("artist", ""),
                            "sources": ["deezer"], "score": 0.78,
                            "is_music_confirmed": True,
                            "video_id": "", "title": "", "author": "",
                        })
            return rows[:3]
        except Exception:
            return []

    lf, dz = await asyncio.gather(_lastfm(), _deezer())
    return lf + dz


async def _bandcamp_recs(track: str, artist: str, seed_key: str) -> list[dict]:
    """Find seed's album on Bandcamp, scrape sidebar 'you may also like'."""
    cached = _bc_lookup_get(seed_key)
    if cached is not None:
        bc_url = cached.get("bandcamp_url")
    else:
        bc_url = None
        try:
            results = await bandcamp_search_albums(f"{artist} {track}", limit=3)
            if results:
                bc_url = results[0].get("bandcamp_url") or ""
        except Exception:
            pass
        _bc_lookup_store(seed_key, bc_url or None)

    if not bc_url:
        return []

    try:
        loop = asyncio.get_running_loop()
        recommender = get_shared_bandcamp_recommender()
        sidebar = await loop.run_in_executor(None, recommender.get_recommendations, bc_url)
    except Exception as exc:
        logger.debug("Bandcamp sidebar failed for %s: %s", bc_url, exc)
        return []

    rows = bandcamp_sidebar_to_music_recommendation_rows(sidebar)
    for r in rows:
        r["is_music_confirmed"] = True
        r["sources"] = ["bandcamp"]
        r["score"] = float(r.get("graph_score") or 0.8)
    return rows


async def _resolve_to_youtube(recs: list[dict]) -> list[dict]:
    """Resolve recs without video_id. Tries Invidious first, falls back to yt-dlp.

    Only resolves the top-scored unresolved items (by score DESC) to cap yt-dlp calls.
    """
    need_all = [r for r in recs if not (r.get("video_id") or "").strip()]
    have = [r for r in recs if (r.get("video_id") or "").strip()]
    if not need_all:
        return recs

    # Prioritise high-confidence items; cap total resolution calls
    need = sorted(need_all, key=lambda x: -(x.get("score") or 0))[:10]
    sem = asyncio.Semaphore(3)

    async def _one(rec: dict) -> dict:
        async with sem:
            # Try Invidious (with short timeout + circuit-breaker)
            if _invidious_available():
                try:
                    hit = await asyncio.wait_for(try_resolve_youtube_match(rec), timeout=5.0)
                    if hit and hit.get("video_id"):
                        _mark_invidious_ok()
                        merged = {**rec, **hit}
                        orig_src = rec.get("sources") or []
                        hit_src = [s for s in (hit.get("source") or "").split(",") if s]
                        merged["sources"] = list(dict.fromkeys(orig_src + hit_src))
                        merged["is_music_confirmed"] = _is_confirmed_music(merged)
                        return merged
                    _mark_invidious_failure()
                except Exception:
                    _mark_invidious_failure()

            # Fallback: yt-dlp search
            t = (rec.get("track") or "").strip()
            a = (rec.get("artist") or "").strip()
            q = f"{a} {t}".strip()
            if not q:
                return rec
            if not _ytdlp_available():
                return rec
            try:
                loop = asyncio.get_running_loop()
                entries = await asyncio.wait_for(
                    loop.run_in_executor(None, _ytdlp_search_sync, q, 5),
                    timeout=8.0,
                )
                for entry in (entries or []):
                    if not entry:
                        continue
                    candidate = {**rec, **_shape_ytdlp_entry(entry, t, a)}
                    if _is_confirmed_music(candidate):
                        return candidate
            except Exception:
                pass

            return rec

    resolved = await asyncio.gather(*[_one(r) for r in need])
    return have + list(resolved)


# ── Main entry point ─────────────────────────────────────────────────────────

async def generate_radio(
    seeds: list[tuple[str, str]],
    *,
    limit: int = 30,
    hops: int = 1,
    exclude_video_ids: set[str] | None = None,
) -> list[dict]:
    """
    BFS from (track, artist) seeds. Returns up to ``limit`` music-confirmed YouTube videos.

    Each node fetches Bandcamp sidebar + Last.fm/Deezer + YouTube music search in parallel,
    then resolves unresolved items to YouTube. Everything is persisted in radio_graph_cache
    so repeat calls for the same seeds return instantly from cache.

    hops=1 (default): only process the given seeds.
    hops=2: also expand top results from hop 1 as new seeds (slow on first call, cached after).
    """
    if os.getenv("DISABLE_EXTERNAL_APIS", "0") == "1":
        return []

    exclude_video_ids = exclude_video_ids or set()

    uniq: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for t, a in seeds:
        k = track_identity_key(a or "", t or "")
        if k not in seen_keys:
            seen_keys.add(k)
            uniq.append(((t or "").strip(), (a or "").strip()))
    uniq = uniq[:MAX_SEEDS]

    queue: deque[tuple[str, str, int]] = deque((t, a, 1) for t, a in uniq)
    visited: set[str] = set()
    result: list[dict] = []
    seen_videos: set[str] = set()

    while queue and len(result) < limit:
        track, artist, hop = queue.popleft()
        seed_key = track_identity_key(artist, track)
        if seed_key in visited:
            continue
        visited.add(seed_key)

        cached = _cache_get(seed_key)
        if cached is not None:
            recs = cached
        else:
            bc, api, yt = await asyncio.gather(
                _bandcamp_recs(track, artist, seed_key),
                _api_music_recs(track, artist),
                _youtube_music_search(track, artist),
            )
            merged = bc + api + yt
            resolved = await _resolve_to_youtube(merged)

            recs_all = [
                r for r in resolved
                if _is_confirmed_music(r) and (r.get("video_id") or "").strip()
            ]
            seen_local: set[str] = set()
            recs = []
            for r in sorted(recs_all, key=lambda x: -(x.get("score") or 0)):
                vid = r["video_id"]
                if vid not in seen_local:
                    seen_local.add(vid)
                    recs.append(r)

            _cache_store(seed_key, recs)

        for rec in recs:
            vid = (rec.get("video_id") or "").strip()
            if not vid or vid in seen_videos or vid in exclude_video_ids:
                continue
            seen_videos.add(vid)
            result.append(rec)

        if hop < hops:
            for rec in recs[:4]:
                nk = track_identity_key(rec.get("artist", ""), rec.get("track", ""))
                if nk not in visited and (rec.get("track") or rec.get("artist")):
                    queue.append((rec.get("track", ""), rec.get("artist", ""), hop + 1))

    return result[:limit]
