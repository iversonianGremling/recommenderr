"""Per-category recommendation pipeline.

Background workers pick stale categories, derive seed videos from the category's
contents (videos / channels / tags), call the existing PPR engine, and persist
the top results into category_recommendations. The endpoint reads from there.

All knobs live in this module so they're easy to tune.
"""
import asyncio
import logging
import time

from backend.db import get_db, get_category_descendant_ids, _published_ts_from_invidious_rec
from backend.services.invidious_client import api_get
from backend.services.ppr_engine import explore_from_seeds

logger = logging.getLogger("category_recs")

# ── Tunables ──────────────────────────────────────────────
MIN_CATEGORY_SEED_VIDEOS = 5
MAX_CATEGORY_SEED_VIDEOS = 10
MIN_VIDEOS_PER_CHANNEL = 3
RECS_PER_CATEGORY = 30
WORKER_COUNT = 3
WORKER_BATCH_SLEEP = 8.0
CATEGORY_REFRESH_HOURS = 6
INVIDIOUS_TOP_VIDEOS_PAGE_SIZE = 30


def init_category_recs_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS category_recommendations (
            category_id INTEGER NOT NULL,
            video_id    TEXT    NOT NULL,
            score       REAL    NOT NULL,
            title       TEXT,
            author      TEXT,
            author_id   TEXT,
            thumbnail   TEXT,
            duration    INTEGER,
            computed_at REAL    NOT NULL,
            PRIMARY KEY (category_id, video_id)
        );
        CREATE INDEX IF NOT EXISTS idx_catrecs_cat_score
            ON category_recommendations(category_id, score DESC);

        CREATE TABLE IF NOT EXISTS category_rec_jobs (
            category_id  INTEGER PRIMARY KEY,
            status       TEXT NOT NULL DEFAULT 'pending',
            last_run_at  REAL,
            next_run_at  REAL,
            last_error   TEXT
        );
    """)
    conn.commit()
    conn.close()


# ── Job table helpers ─────────────────────────────────────

def mark_dirty(category_id: int):
    """Mark a category (and all its ancestors) as needing recompute."""
    if not category_id:
        return
    conn = get_db()
    ids = [category_id]
    cur_id = category_id
    visited = {category_id}
    while True:
        row = conn.execute("SELECT parent_id FROM categories WHERE id=?", (cur_id,)).fetchone()
        if not row or row["parent_id"] is None or row["parent_id"] in visited:
            break
        visited.add(row["parent_id"])
        ids.append(row["parent_id"])
        cur_id = row["parent_id"]
    now = time.time()
    for cid in ids:
        conn.execute("""
            INSERT INTO category_rec_jobs (category_id, status, next_run_at)
            VALUES (?, 'pending', ?)
            ON CONFLICT(category_id) DO UPDATE SET
                status = CASE WHEN status='running' THEN 'running' ELSE 'pending' END,
                next_run_at = ?
        """, (cid, now, now))
    conn.commit()
    conn.close()


def _ensure_jobs_for_all_categories():
    """Insert pending job rows for every category that doesn't have one yet."""
    conn = get_db()
    conn.execute("""
        INSERT OR IGNORE INTO category_rec_jobs (category_id, status, next_run_at)
        SELECT id, 'pending', ? FROM categories
    """, (time.time(),))
    # Re-queue stale done jobs (older than refresh window)
    cutoff = time.time() - CATEGORY_REFRESH_HOURS * 3600
    conn.execute("""
        UPDATE category_rec_jobs
        SET status = 'pending', next_run_at = ?
        WHERE status = 'done' AND last_run_at < ?
    """, (time.time(), cutoff))
    conn.commit()
    conn.close()


def pick_next_job() -> int | None:
    """Atomically claim the next eligible category job. Returns category_id or None."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT category_id FROM category_rec_jobs
            WHERE status = 'pending' AND (next_run_at IS NULL OR next_run_at <= ?)
            ORDER BY COALESCE(last_run_at, 0) ASC, category_id ASC
            LIMIT 1
        """, (time.time(),)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        cid = row["category_id"]
        conn.execute(
            "UPDATE category_rec_jobs SET status='running' WHERE category_id=?",
            (cid,)
        )
        conn.execute("COMMIT")
        return cid
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _finish_job(category_id: int, error: str | None = None):
    conn = get_db()
    conn.execute("""
        UPDATE category_rec_jobs
        SET status = ?, last_run_at = ?, last_error = ?,
            next_run_at = ?
        WHERE category_id = ?
    """, (
        "failed" if error else "done",
        time.time(),
        error,
        time.time() + CATEGORY_REFRESH_HOURS * 3600,
        category_id,
    ))
    conn.commit()
    conn.close()


# ── Seed selection ────────────────────────────────────────

def _videos_in_category(conn, category_id: int) -> list[str]:
    cat_ids = get_category_descendant_ids(conn, category_id)
    ph = ",".join("?" * len(cat_ids))
    rows = conn.execute(
        f"SELECT video_id FROM video_category_assignments WHERE category_id IN ({ph})",
        cat_ids,
    ).fetchall()
    return [r["video_id"] for r in rows]


def _channels_in_category(conn, category_id: int) -> list[str]:
    cat_ids = get_category_descendant_ids(conn, category_id)
    ph = ",".join("?" * len(cat_ids))
    rows = conn.execute(
        f"SELECT channel_id FROM channel_category_assignments WHERE category_id IN ({ph})",
        cat_ids,
    ).fetchall()
    return [r["channel_id"] for r in rows]


def _tag_ids_for_category(conn, category_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT tag_id FROM category_tags WHERE category_id=?",
        (category_id,),
    ).fetchall()
    return [r["tag_id"] for r in rows]


def _top_videos_for_channel_from_db(conn, channel_id: str, n: int) -> list[str]:
    """Pick the top-N videos for a channel from local DB, ranked by view_count."""
    rows = conn.execute("""
        SELECT v.video_id, COALESCE(vm.view_count, 0) AS views
        FROM (
            SELECT video_id, author_id FROM watch_history
            UNION
            SELECT video_id, author_id FROM feed_recommendations
            UNION
            SELECT video_id, author_id FROM playlist_videos
        ) v
        LEFT JOIN video_metadata vm ON vm.video_id = v.video_id
        WHERE v.author_id = ?
        GROUP BY v.video_id
        ORDER BY views DESC
        LIMIT ?
    """, (channel_id, n)).fetchall()
    return [r["video_id"] for r in rows]


async def _fetch_top_videos_for_channel_invidious(channel_id: str, n: int) -> list[dict]:
    """Fetch popular videos for a channel from Invidious. Returns list of dicts."""
    try:
        data = await api_get(
            f"/channels/{channel_id}/videos",
            params={"sort_by": "popular"},
            timeout=30.0,
        )
    except Exception as e:
        logger.warning("Invidious popular fetch failed for %s: %s", channel_id, e)
        return []
    items = data if isinstance(data, list) else data.get("videos", [])
    return items[:n]


def _persist_invidious_videos(channel_id: str, items: list[dict]):
    """Cache Invidious channel-video results in feed_recommendations + video_metadata."""
    if not items:
        return
    conn = get_db()
    now = time.time()
    for it in items:
        vid = it.get("videoId") or it.get("video_id")
        if not vid:
            continue
        title = it.get("title", "")
        author = it.get("author", "")
        thumb = None
        thumbs = it.get("videoThumbnails") or []
        if thumbs:
            thumb = thumbs[0].get("url")
        duration = it.get("lengthSeconds") or it.get("duration")
        view_count = it.get("viewCount")
        pub = _published_ts_from_invidious_rec(it)
        conn.execute("""
            INSERT INTO feed_recommendations
                (video_id, title, thumbnail, duration, author, author_id,
                 source_video_id, source_video_title, added_at, published_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (vid, title, thumb, duration, author, channel_id,
              f"channel:{channel_id}", "popular", now, pub))
        if view_count is not None:
            conn.execute("""
                INSERT OR REPLACE INTO video_metadata
                    (video_id, view_count, fetched_at)
                VALUES (?,?,?)
            """, (vid, view_count, now))
    conn.commit()
    conn.close()


async def select_seed_videos(category_id: int) -> list[str]:
    """Build the seed-video set for a category."""
    conn = get_db()
    try:
        seeds: list[str] = list(dict.fromkeys(_videos_in_category(conn, category_id)))

        if len(seeds) < MIN_CATEGORY_SEED_VIDEOS:
            channels = _channels_in_category(conn, category_id)
            for ch in channels:
                if len(seeds) >= MAX_CATEGORY_SEED_VIDEOS:
                    break
                local = _top_videos_for_channel_from_db(conn, ch, MIN_VIDEOS_PER_CHANNEL)
                if not local:
                    # Fall back to Invidious. Releases conn first.
                    conn.close()
                    items = await _fetch_top_videos_for_channel_invidious(
                        ch, INVIDIOUS_TOP_VIDEOS_PAGE_SIZE
                    )
                    _persist_invidious_videos(ch, items)
                    conn = get_db()
                    local = _top_videos_for_channel_from_db(conn, ch, MIN_VIDEOS_PER_CHANNEL)
                for v in local:
                    if v not in seeds:
                        seeds.append(v)

        if len(seeds) < MIN_CATEGORY_SEED_VIDEOS:
            tag_ids = _tag_ids_for_category(conn, category_id)
            if tag_ids:
                ph = ",".join("?" * len(tag_ids))
                rows = conn.execute(f"""
                    SELECT DISTINCT vt.video_id
                    FROM video_tags vt
                    LEFT JOIN video_ratings vr ON vr.video_id = vt.video_id
                    WHERE vt.tag_id IN ({ph})
                    ORDER BY CAST(COALESCE(vr.rating,'5') AS REAL) DESC
                    LIMIT ?
                """, (*tag_ids, MAX_CATEGORY_SEED_VIDEOS)).fetchall()
                for r in rows:
                    if r["video_id"] not in seeds:
                        seeds.append(r["video_id"])
                        if len(seeds) >= MAX_CATEGORY_SEED_VIDEOS:
                            break

        return seeds[:MAX_CATEGORY_SEED_VIDEOS]
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ── Compute + persist ─────────────────────────────────────

def _persist_recommendations(category_id: int, items: list[dict]):
    conn = get_db()
    now = time.time()
    conn.execute("DELETE FROM category_recommendations WHERE category_id=?", (category_id,))
    for it in items:
        conn.execute("""
            INSERT OR REPLACE INTO category_recommendations
                (category_id, video_id, score, title, author, author_id,
                 thumbnail, duration, computed_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            category_id, it["video_id"], it.get("score", 0.0),
            it.get("title"), it.get("author"), it.get("author_id"),
            it.get("thumbnail"), it.get("duration"), now,
        ))
    conn.commit()
    conn.close()


async def compute_for_category(category_id: int) -> int:
    """Run the seed pipeline + PPR for a single category. Returns rec count."""
    seeds = await select_seed_videos(category_id)
    if not seeds:
        _persist_recommendations(category_id, [])
        return 0
    seed_input = [{"type": "video", "id": v} for v in seeds]
    # explore_from_seeds is sync + DB-heavy; off-thread it.
    items = await asyncio.to_thread(explore_from_seeds, seed_input, RECS_PER_CATEGORY)
    _persist_recommendations(category_id, items)
    return len(items)


def get_recommendations(category_id: int, limit: int = RECS_PER_CATEGORY) -> list[dict]:
    conn = get_db()
    rows = conn.execute("""
        SELECT video_id, score, title, author, author_id, thumbnail, duration, computed_at
        FROM category_recommendations
        WHERE category_id = ?
        ORDER BY score DESC
        LIMIT ?
    """, (category_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_job_status(category_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT status, last_run_at, last_error FROM category_rec_jobs WHERE category_id=?",
        (category_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Worker ─────────────────────────────────────────────────

async def category_recs_worker(worker_id: int):
    """One async worker; multiple of these run concurrently and rotate categories."""
    logger.info("Category-recs worker %d started", worker_id)
    await asyncio.sleep(5 + worker_id * 1.5)  # stagger startup

    last_seed = 0.0
    while True:
        try:
            now = time.time()
            if now - last_seed > 60:
                # Worker 0 pulls fresh category definitions/assignments from signal
                # sources (ytvideo) before re-seeding jobs. sync_user_data_cache has
                # its own TTL guard, so this stays cheap.
                if worker_id == 0:
                    try:
                        from backend.services.user_data_sync import sync_user_data_cache
                        await sync_user_data_cache()
                    except Exception as e:
                        logger.debug("category worker: user-data sync skipped: %s", e)
                _ensure_jobs_for_all_categories()
                last_seed = now

            cat_id = pick_next_job()
            if cat_id is None:
                await asyncio.sleep(15.0)
                continue

            t0 = time.time()
            try:
                count = await compute_for_category(cat_id)
                _finish_job(cat_id, None)
                logger.info(
                    "[w%d] category %d → %d recs in %.1fs",
                    worker_id, cat_id, count, time.time() - t0,
                )
            except Exception as e:
                _finish_job(cat_id, str(e)[:200])
                logger.warning("[w%d] category %d failed: %s", worker_id, cat_id, e)

            await asyncio.sleep(WORKER_BATCH_SLEEP)
        except asyncio.CancelledError:
            logger.info("Category-recs worker %d stopped", worker_id)
            raise
        except Exception as e:
            logger.error("[w%d] loop error: %s", worker_id, e)
            await asyncio.sleep(5.0)
