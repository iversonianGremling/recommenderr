import asyncio
import random
import time
import logging
from backend.db import get_db, save_recommendations
from backend.services.invidious_client import api_get

logger = logging.getLogger("crawler")

MAX_RETRIES = 10


def _store_metadata(video_id: str, data: dict):
    """Persist genre, description, view/like counts and keywords from a crawled video."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO video_metadata (video_id, genre, description, view_count, like_count, fetched_at) VALUES (?,?,?,?,?,?)",
        (
            video_id,
            data.get("genre"),
            (data.get("description") or "")[:2000],
            data.get("viewCount"),
            data.get("likeCount"),
            time.time(),
        )
    )
    keywords = [k for k in (data.get("keywords") or []) if k]
    if keywords:
        conn.executemany(
            "INSERT OR IGNORE INTO video_keywords (video_id, keyword) VALUES (?,?)",
            [(video_id, kw.lower().strip()) for kw in keywords]
        )
    conn.commit()
    conn.close()

# Base delay between normal requests (seconds). Actual delay is fuzzed around this.
BASE_DELAY = 2.5


def _retry_backoff(retry_count: int) -> float:
    """Exponential backoff with heavy jitter so retries don't look like a pattern.

    Returns seconds to wait before this video is eligible for retry.
    Backoff: 2^n minutes, capped at 4 hours, with ±50% jitter.
    """
    base_minutes = min(2 ** retry_count, 240)          # 1, 2, 4, 8, 16, 32, 64, 128, 240, 240…
    jitter = random.uniform(0.5, 1.5)                   # ±50%
    return base_minutes * 60 * jitter


def _request_delay() -> float:
    """Delay between requests to make traffic look organic.

    Uses a mix of short delays (most common) and occasional longer pauses
    to mimic human browsing patterns rather than a steady drumbeat.
    """
    roll = random.random()
    if roll < 0.60:
        # 60% of the time: short-ish pause (2–4s)
        return random.uniform(2.0, 4.0)
    elif roll < 0.85:
        # 25% of the time: medium pause (4–9s)
        return random.uniform(4.0, 9.0)
    elif roll < 0.97:
        # 12% of the time: longer think (9–20s)
        return random.uniform(9.0, 20.0)
    else:
        # 3% of the time: big gap like a human got distracted (20–60s)
        return random.uniform(20.0, 60.0)


def populate_queue():
    """Add uncrawled history and playlist videos to the crawl queue.
    Immediately marks as done any video that already has outgoing edges —
    no need to re-fetch recommendations we already have.
    """
    conn = get_db()
    now = time.time()
    conn.execute("""
        INSERT OR IGNORE INTO crawl_queue (video_id, title, status, added_at)
        SELECT video_id, title, 'pending', ?
        FROM watch_history
        WHERE video_id NOT IN (SELECT video_id FROM crawl_queue)
    """, (now,))
    conn.execute("""
        INSERT OR IGNORE INTO crawl_queue (video_id, title, status, added_at)
        SELECT video_id, title, 'pending', ?
        FROM playlist_videos
        WHERE video_id NOT IN (SELECT video_id FROM crawl_queue)
    """, (now,))
    # Skip anything we already have edges for (handles restarts + container rebuilds)
    conn.execute("""
        UPDATE crawl_queue SET status = 'done', crawled_at = ?
        WHERE status = 'pending'
        AND video_id IN (SELECT DISTINCT source_video_id FROM recommendation_edges)
    """, (now,))
    conn.commit()
    conn.close()


def get_next_pending():
    """Fetch the next actionable video: pending ones first, then retryable failed ones."""
    conn = get_db()
    now = time.time()
    # Prefer never-tried pending over retries
    row = conn.execute("""
        SELECT video_id, title, retry_count FROM crawl_queue
        WHERE (status = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= ?))
           OR (status = 'failed' AND retry_count < ? AND next_retry_at <= ?)
        ORDER BY
            CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
            next_retry_at ASC
        LIMIT 1
    """, (now, MAX_RETRIES, now)).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_done(video_id: str):
    conn = get_db()
    conn.execute(
        "UPDATE crawl_queue SET status = 'done', crawled_at = ? WHERE video_id = ?",
        (time.time(), video_id)
    )
    conn.commit()
    conn.close()


def mark_failed(video_id: str, retry_count: int):
    """Mark as failed and schedule next retry with backoff + jitter."""
    new_count = retry_count + 1
    if new_count >= MAX_RETRIES:
        # Give up — mark permanently failed
        conn = get_db()
        conn.execute(
            "UPDATE crawl_queue SET status = 'failed', crawled_at = ?, retry_count = ?, next_retry_at = NULL WHERE video_id = ?",
            (time.time(), new_count, video_id)
        )
        conn.commit()
        conn.close()
        logger.debug("Gave up on %s after %d attempts", video_id, new_count)
    else:
        wait = _retry_backoff(new_count)
        conn = get_db()
        conn.execute(
            "UPDATE crawl_queue SET status = 'failed', crawled_at = ?, retry_count = ?, next_retry_at = ? WHERE video_id = ?",
            (time.time(), new_count, time.time() + wait, video_id)
        )
        conn.commit()
        conn.close()
        logger.debug("Scheduled retry %d for %s in %.0fs", new_count, video_id, wait)


def reset_failed():
    """Reset all failed entries back to pending for retry (resets retry count too)."""
    conn = get_db()
    conn.execute("""
        UPDATE crawl_queue
        SET status = 'pending', retry_count = 0, next_retry_at = NULL
        WHERE status = 'failed'
    """)
    conn.commit()
    conn.close()


def get_crawler_stats():
    """Return counts by status plus number of retryable-failed entries."""
    conn = get_db()
    rows = conn.execute(
        "SELECT status, COUNT(*) as count FROM crawl_queue GROUP BY status"
    ).fetchall()
    retryable = conn.execute(
        "SELECT COUNT(*) as n FROM crawl_queue WHERE status = 'failed' AND retry_count < ? AND next_retry_at <= ?",
        (MAX_RETRIES, time.time())
    ).fetchone()
    conn.close()

    stats = {"pending": 0, "done": 0, "failed": 0, "total": 0, "retryable": retryable["n"] if retryable else 0}
    for r in rows:
        stats[r["status"]] = r["count"]
        stats["total"] += r["count"]
    return stats


def _adaptive_delay(consecutive_errors: int) -> float:
    """Scale up the inter-request delay when errors are piling up.

    Each consecutive error adds one extra multiplier tier (capped at 8×).
    Resets to normal as soon as a request succeeds.
    """
    base = _request_delay()
    if consecutive_errors == 0:
        return base
    multiplier = min(2 ** consecutive_errors, 8) * random.uniform(0.75, 1.25)
    delay = base * multiplier
    logger.info("Consecutive errors=%d, delay multiplier=%.1fx (%.0fs)", consecutive_errors, multiplier, delay)
    return delay


async def crawl_worker():
    """Background worker that fetches recommendations for all history/playlist videos.

    Uses organic-looking request timing to avoid fingerprinting:
    - Normal requests: 2–4s (60%), 4–9s (25%), 9–20s (12%), 20–60s (3%)
    - Failed retries: exponential backoff (1–240 min) with ±50% jitter
    - Up to 10 retries per video before permanent failure
    - Adaptive backpressure: multiplies delay when consecutive errors accumulate
    """
    logger.info("Crawler worker started")
    await asyncio.sleep(5)  # let app finish initializing

    consecutive_errors = 0

    while True:
        try:
            populate_queue()
            video = get_next_pending()

            if not video:
                # Nothing ready right now — sleep briefly then check again
                await asyncio.sleep(30)
                continue

            retry_count = video.get("retry_count", 0)
            try:
                data = await api_get(f"/videos/{video['video_id']}", timeout=60.0)
                recs = data.get("recommendedVideos", [])
                title = video.get("title") or data.get("title", "")
                save_recommendations(video["video_id"], title, recs, max_save=10)
                _store_metadata(video["video_id"], data)
                mark_done(video["video_id"])
                logger.debug("Crawled %s (%d recs)", video["video_id"], len(recs))
                consecutive_errors = 0  # reset on success
            except Exception as e:
                consecutive_errors += 1
                mark_failed(video["video_id"], retry_count)
                logger.warning("Failed to crawl %s (attempt %d): %s", video["video_id"], retry_count + 1, e)

        except asyncio.CancelledError:
            logger.info("Crawler worker stopped")
            raise
        except Exception as e:
            logger.error("Crawler loop error: %s", e)

        await asyncio.sleep(_adaptive_delay(consecutive_errors))
