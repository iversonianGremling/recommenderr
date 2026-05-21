import asyncio
import logging
import time
from backend.db import get_db
from backend.services.invidious_client import api_get
from backend.services.music_recognition import recognize
from backend.services.music_recommendations import get_recommendations
from backend.services.music_client import lastfm_get_similar_tracks, deezer_search
from backend.services.music_tags import ensure_playlist_tag

logger = logging.getLogger("music_worker")

_job_queue: asyncio.Queue = asyncio.Queue()


def _pick_genre(recognition, lastfm_tags: list[str]) -> str | None:
    """Best-effort genre: prefer Last.fm tags, fallback to video genre field."""
    # Map common Last.fm tags to cleaner genre names
    TAG_MAP = {
        "hip hop": "Hip-Hop", "hip-hop": "Hip-Hop", "rap": "Hip-Hop",
        "electronic": "Electronic", "electronica": "Electronic", "edm": "Electronic",
        "house": "Electronic", "techno": "Electronic", "dnb": "Electronic",
        "drum and bass": "Electronic", "ambient": "Electronic",
        "rock": "Rock", "alternative": "Rock", "indie rock": "Rock",
        "indie": "Indie", "indie pop": "Indie",
        "pop": "Pop",
        "r&b": "R&B", "soul": "R&B", "rnb": "R&B",
        "jazz": "Jazz",
        "classical": "Classical",
        "metal": "Metal", "heavy metal": "Metal",
        "punk": "Punk",
        "folk": "Folk", "acoustic": "Folk",
        "country": "Country",
        "reggae": "Reggae",
        "blues": "Blues",
        "latin": "Latin",
    }
    for tag in lastfm_tags:
        mapped = TAG_MAP.get(tag.lower().strip())
        if mapped:
            return mapped
    # Fallback: use first tag if reasonable length
    if lastfm_tags:
        t = lastfm_tags[0].strip()
        if 2 < len(t) < 30:
            return t.title()
    return None


async def _get_genre_tags(track: str, artist: str) -> list[str]:
    """Get top tags from Last.fm for a track, for genre detection."""
    from backend.services.music_client import _LASTFM_KEY
    if not _LASTFM_KEY:
        return []
    try:
        import httpx, os
        params = {
            "method": "track.getTopTags",
            "track": track,
            "artist": artist,
            "api_key": _LASTFM_KEY,
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get("https://ws.audioscrobbler.com/2.0/", params=params)
            r.raise_for_status()
            data = r.json()
        tags = data.get("toptags", {}).get("tag", [])
        return [t["name"] for t in tags[:5] if t.get("name")]
    except Exception:
        return []


async def _process_job(job_id: int):
    conn = get_db()
    job = conn.execute("SELECT * FROM music_jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        conn.close()
        return

    playlist_id = job["playlist_id"]
    videos = conn.execute(
        "SELECT video_id, title FROM playlist_videos WHERE playlist_id=? ORDER BY position",
        (playlist_id,)
    ).fetchall()
    conn.execute(
        "UPDATE music_jobs SET status='running', total=?, updated_at=? WHERE id=?",
        (len(videos), time.time(), job_id)
    )
    playlist_tag_id = ensure_playlist_tag(conn, playlist_id, job["playlist_title"]) if playlist_id is not None else None
    conn.commit()
    conn.close()

    processed = 0
    errors = 0

    for video_row in videos:
        video_id = video_row["video_id"]
        try:
            # Fetch full video info for recognition
            try:
                info = await api_get(f"/videos/{video_id}")
            except Exception:
                info = {"title": video_row["title"], "videoId": video_id}

            rec = await recognize(info)

            if not rec.is_music or not (rec.track or rec.artist):
                processed += 1
                _update_progress(job_id, processed, errors)
                await asyncio.sleep(0.3)
                continue

            track = rec.track or info.get("title", "")
            artist = rec.artist or info.get("author", "")

            # Get genre tags from Last.fm
            tags = await _get_genre_tags(track, artist)
            genre = _pick_genre(rec, tags)

            # Store the source video itself in music_library if recognized
            _upsert_library(
                video_id=video_id,
                title=info.get("title") or video_row["title"],
                thumbnail=info.get("videoThumbnails", [{}])[0].get("url") if info.get("videoThumbnails") else None,
                duration=info.get("lengthSeconds"),
                author=info.get("author"),
                author_id=info.get("authorId"),
                track=track,
                artist=artist,
                album=rec.album,
                genre=genre,
                source_job_id=job_id,
                source_video_id=video_id,
                tag_id=playlist_tag_id,
            )

            # Get cross-service recommendations and store them
            recs = await get_recommendations(track, artist, limit=8)
            for r in recs:
                if not r.get("video_id"):
                    continue
                # Add to music_library
                _upsert_library(
                    video_id=r["video_id"],
                    title=r.get("title"),
                    thumbnail=r.get("thumbnail"),
                    duration=r.get("lengthSeconds"),
                    author=r.get("author"),
                    author_id=None,
                    track=r.get("track"),
                    artist=r.get("artist"),
                    album=r.get("album"),
                    genre=genre,  # inherit source genre; could refine per-rec
                    source_job_id=job_id,
                    source_video_id=video_id,
                    tag_id=playlist_tag_id,
                )
                # Add to PPR graph as recommendation edge
                _add_edge(video_id, r["video_id"])
                # Add to feed_recommendations
                _add_feed_rec(
                    video_id=r["video_id"],
                    title=r.get("title", ""),
                    thumbnail=r.get("thumbnail"),
                    duration=r.get("lengthSeconds"),
                    author=r.get("author"),
                    author_id=None,
                    source_video_id=video_id,
                    source_title=track,
                )

            processed += 1
            _update_progress(job_id, processed, errors)
            # Polite delay between videos
            await asyncio.sleep(1.5)

        except Exception as e:
            logger.warning(f"[music_worker] error on {video_id}: {e}")
            errors += 1
            processed += 1
            _update_progress(job_id, processed, errors)
            await asyncio.sleep(0.5)

    conn = get_db()
    conn.execute(
        "UPDATE music_jobs SET status='done', processed=?, errors=?, updated_at=? WHERE id=?",
        (processed, errors, time.time(), job_id)
    )
    conn.commit()
    conn.close()
    logger.info(f"[music_worker] job {job_id} done: {processed} processed, {errors} errors")


def _upsert_library(*, video_id, title, thumbnail, duration, author, author_id,
                    track, artist, album, genre, source_job_id, source_video_id, tag_id=None):
    conn = get_db()
    now = time.time()
    conn.execute("""
        INSERT INTO music_library
            (video_id, title, thumbnail, duration, author, author_id, track, artist, album,
             genre, source_job_id, source_video_id, added_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(video_id) DO UPDATE SET
            track=COALESCE(excluded.track, track),
            artist=COALESCE(excluded.artist, artist),
            album=COALESCE(excluded.album, album),
            genre=COALESCE(excluded.genre, genre),
            added_at=excluded.added_at
    """, (video_id, title, thumbnail, duration, author, author_id,
          track, artist, album, genre, source_job_id, source_video_id, now))
    if tag_id is not None:
        conn.execute(
            """
            INSERT OR IGNORE INTO music_tag_assignments (tag_id, video_id, created_at)
            VALUES (?, ?, ?)
            """,
            (tag_id, video_id, now),
        )
    conn.commit()
    conn.close()


def _add_edge(src: str, tgt: str, weight: float = 1.0):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO recommendation_edges (source_video_id, target_video_id, weight, added_at)
        VALUES (?,?,?,?)
    """, (src, tgt, weight, time.time()))
    conn.commit()
    conn.close()


def _add_feed_rec(*, video_id, title, thumbnail, duration, author, author_id,
                  source_video_id, source_title, published_at=None):
    if not title:
        return
    conn = get_db()
    conn.execute("""
        INSERT OR IGNORE INTO feed_recommendations
            (video_id, title, thumbnail, duration, author, author_id, source_video_id, source_video_title, added_at, published_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (video_id, title, thumbnail, duration, author, author_id,
          source_video_id, source_title, time.time(), published_at))
    conn.commit()
    conn.close()


def _update_progress(job_id: int, processed: int, errors: int):
    conn = get_db()
    conn.execute(
        "UPDATE music_jobs SET processed=?, errors=?, updated_at=? WHERE id=?",
        (processed, errors, time.time(), job_id)
    )
    conn.commit()
    conn.close()


async def submit_job(playlist_id: int) -> int:
    """Create a new job and enqueue it. Returns job id."""
    conn = get_db()
    pl = conn.execute("SELECT title FROM playlists WHERE id=?", (playlist_id,)).fetchone()
    title = pl["title"] if pl else str(playlist_id)
    now = time.time()
    cur = conn.execute(
        "INSERT INTO music_jobs (playlist_id, playlist_title, status, created_at, updated_at) VALUES (?,?,'pending',?,?)",
        (playlist_id, title, now, now)
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    await _job_queue.put(job_id)
    return job_id


async def music_worker():
    """Background worker — processes music jobs from the queue."""
    logger.info("Music worker started")
    # On startup, requeue any jobs that were left in running/pending state
    conn = get_db()
    stale = conn.execute(
        "SELECT id FROM music_jobs WHERE status IN ('pending', 'running')"
    ).fetchall()
    conn.close()
    for row in stale:
        await _job_queue.put(row["id"])

    while True:
        try:
            job_id = await _job_queue.get()
            await _process_job(job_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[music_worker] unhandled error: {e}")
