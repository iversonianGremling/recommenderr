"""Per-category music recommendation pipeline.

A "music category" is either an individual ``music_tag`` (kind=``tag``) or a
``music_tag_group`` (kind=``group``). For each, we derive seed tracks from the
user's library (tracks assigned that tag / any tag in that group) and build two
pools:

  * **external discovery** (favoured) — Last.fm/Deezer/Bandcamp similar-artist
    recs via ``music_recommendations.get_recommendations``, resolved to YouTube.
    Inherently coherent (similar-artist based) and seeded only from the
    category's own tracks, so a rap category never bleeds into pagan folk.
  * **library** — coherent in-library neighbours via ``radio_library.build_radio``
    (which now drops anything outside the seed's stylistic shape).

External is placed first, library backfills the remainder, with a per-artist cap.

Unlike the video category worker, we do NOT pre-seed every category: external
discovery is egress-heavy and CT134 egress is shared/saturated, so a category is
only computed once it has actually been requested (the endpoint marks it dirty).
"""
from __future__ import annotations

import asyncio
import logging
import time

from backend.db import get_db
from backend.services import radio_library
from backend.services import music_recommendations as mrec

logger = logging.getLogger("music_category_recs")

# ── Tunables ──────────────────────────────────────────────
RECS_PER_CATEGORY = 40
EXTERNAL_SEED_SAMPLE = 4          # distinct seed artists fed to external discovery
EXTERNAL_PER_SEED = 10            # recs requested per seed
MAX_PER_ARTIST = 3                # cap any one artist across the merged result
REFRESH_HOURS = 24                # external is costly; refresh slowly
WORKER_BATCH_SLEEP = 12.0
VALID_KINDS = ("tag", "group")


def init_music_category_recs_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS music_category_recommendations (
            kind        TEXT    NOT NULL,
            ref_id      INTEGER NOT NULL,
            video_id    TEXT    NOT NULL,
            score       REAL    NOT NULL,
            title       TEXT,
            artist      TEXT,
            album       TEXT,
            author      TEXT,
            author_id   TEXT,
            thumbnail   TEXT,
            duration    INTEGER,
            external    INTEGER NOT NULL DEFAULT 0,
            computed_at REAL    NOT NULL,
            PRIMARY KEY (kind, ref_id, video_id)
        );
        CREATE INDEX IF NOT EXISTS idx_mcatrecs_ref_score
            ON music_category_recommendations(kind, ref_id, score DESC);

        CREATE TABLE IF NOT EXISTS music_category_rec_jobs (
            kind        TEXT    NOT NULL,
            ref_id      INTEGER NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'pending',
            last_run_at REAL,
            next_run_at REAL,
            last_error  TEXT,
            PRIMARY KEY (kind, ref_id)
        );

        -- music_tag_assignments only ships a video_id index; our catalog counts
        -- and per-category seed selection query by tag_id, so add that index.
        CREATE INDEX IF NOT EXISTS idx_music_tag_assignments_tag
            ON music_tag_assignments(tag_id);
    """)
    conn.commit()
    conn.close()


# ── Catalog ────────────────────────────────────────────────

def list_categories() -> dict:
    """The music category catalog: tag groups + tags (with track counts)."""
    conn = get_db()
    try:
        groups = [dict(r) for r in conn.execute(
            "SELECT id, name, system_key, position FROM music_tag_groups ORDER BY position, name"
        ).fetchall()]
        tags = [dict(r) for r in conn.execute("""
            SELECT t.id, t.name, t.group_id, t.parent_id, t.kind,
                   (SELECT COUNT(*) FROM music_tag_assignments a WHERE a.tag_id = t.id) AS track_count
            FROM music_tags t
            ORDER BY t.group_id, t.position, t.name
        """).fetchall()]
        return {"groups": groups, "tags": tags}
    finally:
        conn.close()


# ── Seed selection ────────────────────────────────────────

def _tag_ids_for(conn, kind: str, ref_id: int) -> list[int]:
    if kind == "tag":
        return [ref_id]
    rows = conn.execute(
        "SELECT id FROM music_tags WHERE group_id = ?", (ref_id,)
    ).fetchall()
    return [r["id"] for r in rows]


def _seed_tracks(conn, kind: str, ref_id: int) -> list[dict]:
    """Library tracks belonging to this category, richest (most-rated/listened) first."""
    tag_ids = _tag_ids_for(conn, kind, ref_id)
    if not tag_ids:
        return []
    ph = ",".join("?" * len(tag_ids))
    rows = conn.execute(f"""
        SELECT DISTINCT ml.video_id, ml.title, ml.track, ml.artist, ml.author,
               ml.album, ml.genre,
               COALESCE((SELECT h.listen_count FROM watch_history h
                          WHERE h.video_id = ml.video_id), 0) AS listen_count,
               COALESCE(vr.rating, 5) AS rating
        FROM music_tag_assignments a
        JOIN music_library ml ON ml.video_id = a.video_id
        LEFT JOIN video_ratings vr ON vr.video_id = ml.video_id
        WHERE a.tag_id IN ({ph})
        ORDER BY rating DESC, listen_count DESC
    """, tag_ids).fetchall()
    return [dict(r) for r in rows]


def _external_seed_pairs(seeds: list[dict]) -> list[tuple[str, str]]:
    """Pick up to EXTERNAL_SEED_SAMPLE distinct (track, artist) pairs, one per
    artist, so discovery spans the category instead of one artist's catalogue."""
    pairs: list[tuple[str, str]] = []
    seen_artists: set[str] = set()
    for s in seeds:
        artist = (s.get("artist") or s.get("author") or "").strip()
        track = (s.get("track") or s.get("title") or "").strip()
        if not artist and not track:
            continue
        akey = artist.lower()
        if akey in seen_artists:
            continue
        seen_artists.add(akey)
        pairs.append((track, artist))
        if len(pairs) >= EXTERNAL_SEED_SAMPLE:
            break
    return pairs


# ── Job table helpers ─────────────────────────────────────

def mark_dirty(kind: str, ref_id: int):
    if kind not in VALID_KINDS or not ref_id:
        return
    conn = get_db()
    now = time.time()
    conn.execute("""
        INSERT INTO music_category_rec_jobs (kind, ref_id, status, next_run_at)
        VALUES (?, ?, 'pending', ?)
        ON CONFLICT(kind, ref_id) DO UPDATE SET
            status = CASE WHEN status='running' THEN 'running' ELSE 'pending' END,
            next_run_at = ?
    """, (kind, ref_id, now, now))
    conn.commit()
    conn.close()


def pick_next_job() -> tuple[str, int] | None:
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT kind, ref_id FROM music_category_rec_jobs
            WHERE status = 'pending' AND (next_run_at IS NULL OR next_run_at <= ?)
            ORDER BY COALESCE(last_run_at, 0) ASC
            LIMIT 1
        """, (time.time(),)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "UPDATE music_category_rec_jobs SET status='running' WHERE kind=? AND ref_id=?",
            (row["kind"], row["ref_id"]),
        )
        conn.execute("COMMIT")
        return (row["kind"], row["ref_id"])
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _requeue_stale_jobs():
    cutoff = time.time() - REFRESH_HOURS * 3600
    conn = get_db()
    conn.execute("""
        UPDATE music_category_rec_jobs
        SET status = 'pending', next_run_at = ?
        WHERE status = 'done' AND last_run_at < ?
    """, (time.time(), cutoff))
    conn.commit()
    conn.close()


def _finish_job(kind: str, ref_id: int, error: str | None = None):
    conn = get_db()
    conn.execute("""
        UPDATE music_category_rec_jobs
        SET status = ?, last_run_at = ?, last_error = ?, next_run_at = ?
        WHERE kind = ? AND ref_id = ?
    """, (
        "failed" if error else "done",
        time.time(), error,
        time.time() + REFRESH_HOURS * 3600,
        kind, ref_id,
    ))
    conn.commit()
    conn.close()


def get_job_status(kind: str, ref_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT status, last_run_at, last_error FROM music_category_rec_jobs WHERE kind=? AND ref_id=?",
        (kind, ref_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ── Compute + persist ─────────────────────────────────────

def _persist(kind: str, ref_id: int, items: list[dict]):
    conn = get_db()
    now = time.time()
    conn.execute(
        "DELETE FROM music_category_recommendations WHERE kind=? AND ref_id=?",
        (kind, ref_id),
    )
    for it in items:
        conn.execute("""
            INSERT OR REPLACE INTO music_category_recommendations
                (kind, ref_id, video_id, score, title, artist, album, author,
                 author_id, thumbnail, duration, external, computed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            kind, ref_id, it["video_id"], it.get("score", 0.0),
            it.get("title"), it.get("artist"), it.get("album"), it.get("author"),
            it.get("author_id"), it.get("thumbnail"), it.get("duration"),
            1 if it.get("external") else 0, now,
        ))
    conn.commit()
    conn.close()


async def _external_pool(seed_pairs: list[tuple[str, str]], exclude_vids: set[str]) -> list[dict]:
    """Aggregate similar-artist discovery across the seed sample. Only tracks that
    resolved to a YouTube id and aren't already in the library count as discovery."""
    out: list[dict] = []
    seen: set[str] = set()
    for track, artist in seed_pairs:
        try:
            rows = await mrec.get_recommendations(track, artist, limit=EXTERNAL_PER_SEED)
        except Exception as e:
            logger.debug("external discovery failed for (%s, %s): %s", artist, track, e)
            continue
        for r in rows:
            vid = (r.get("video_id") or "").strip()
            if not vid or vid in seen or vid in exclude_vids:
                continue
            seen.add(vid)
            out.append({
                "video_id": vid,
                "title": r.get("title"),
                "track": r.get("track"),   # clean track name for cover lookup
                "artist": r.get("artist") or r.get("author"),
                "album": r.get("album"),
                "author": r.get("author"),
                "author_id": r.get("authorId") or r.get("author_id"),
                "thumbnail": r.get("thumbnail"),
                "duration": r.get("lengthSeconds") or r.get("duration"),
                "external": True,
                "score": float(r.get("recommendation_score") or r.get("graph_score") or 0.0),
            })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


def _library_pool(seed_vids: list[str], exclude_vids: set[str], limit: int) -> list[dict]:
    """Coherent in-library neighbours via the (coherence-guarded) radio builder."""
    if not seed_vids:
        return []
    seeds = [{"video_id": v} for v in seed_vids[:radio_library.SEED_PROFILE_CAP]]
    tracks = radio_library.build_radio(seeds, limit=limit, exclude_video_ids=exclude_vids)
    out = []
    for t in tracks:
        out.append({
            "video_id": t["video_id"],
            "title": t.get("title"),
            "track": t.get("track"),
            "artist": t.get("artist") or t.get("author"),
            "album": None,
            "author": t.get("author"),
            "author_id": t.get("author_id"),
            "thumbnail": t.get("thumbnail"),
            "duration": t.get("duration"),
            "external": False,
            "score": float(t.get("score") or 0.0),
        })
    return out


def _merge(external: list[dict], library: list[dict], limit: int) -> list[dict]:
    """External-first (per user preference), library backfills, capped per artist."""
    merged: list[dict] = []
    seen_vids: set[str] = set()
    per_artist: dict[str, int] = {}

    def _take(pool: list[dict]):
        for it in pool:
            if len(merged) >= limit:
                return
            vid = it["video_id"]
            if vid in seen_vids:
                continue
            akey = (it.get("artist") or "").strip().lower()
            if akey and per_artist.get(akey, 0) >= MAX_PER_ARTIST:
                continue
            seen_vids.add(vid)
            if akey:
                per_artist[akey] = per_artist.get(akey, 0) + 1
            merged.append(it)

    _take(external)
    _take(library)
    return merged[:limit]


async def _attach_covers(items: list[dict]) -> None:
    """Replace YouTube thumbnails with real Deezer/iTunes track covers (cached).
    Per user preference, cover art never comes from YouTube image hosts."""
    from backend.services import cover_art

    async def _one(it: dict):
        artist = (it.get("artist") or it.get("author") or "").strip()
        track = (it.get("track") or it.get("title") or "").strip()
        url = None
        if artist and track:
            url = await cover_art.resolve_now(
                "track", cover_art.track_key(artist, track), artist, track
            )
        it["thumbnail"] = url or ""

    if items:
        await asyncio.gather(*[_one(it) for it in items])


async def compute_for_category(kind: str, ref_id: int) -> int:
    conn = get_db()
    try:
        seeds = _seed_tracks(conn, kind, ref_id)
        lib_vids = {r["video_id"] for r in conn.execute(
            "SELECT video_id FROM music_library"
        ).fetchall()}
    finally:
        conn.close()

    if not seeds:
        _persist(kind, ref_id, [])
        return 0

    seed_vids = [s["video_id"] for s in seeds]
    seed_pairs = _external_seed_pairs(seeds)

    external = await _external_pool(seed_pairs, exclude_vids=lib_vids)
    # Library pool excludes the category's own seed tracks + anything external
    # already surfaced, so the two pools don't collide.
    exclude = set(seed_vids) | {e["video_id"] for e in external}
    library = await asyncio.to_thread(
        _library_pool, seed_vids, exclude, RECS_PER_CATEGORY
    )

    items = _merge(external, library, RECS_PER_CATEGORY)
    await _attach_covers(items)
    _persist(kind, ref_id, items)
    return len(items)


def get_recommendations(kind: str, ref_id: int, limit: int = RECS_PER_CATEGORY) -> list[dict]:
    conn = get_db()
    rows = conn.execute("""
        SELECT video_id, score, title, artist, album, author, author_id,
               thumbnail, duration, external, computed_at
        FROM music_category_recommendations
        WHERE kind = ? AND ref_id = ?
        ORDER BY external DESC, score DESC
        LIMIT ?
    """, (kind, ref_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Worker ─────────────────────────────────────────────────

async def music_category_recs_worker():
    logger.info("Music-category-recs worker started")
    await asyncio.sleep(8)  # stagger after video workers
    last_requeue = 0.0
    while True:
        try:
            now = time.time()
            if now - last_requeue > 300:
                _requeue_stale_jobs()
                last_requeue = now

            job = pick_next_job()
            if job is None:
                await asyncio.sleep(20.0)
                continue

            kind, ref_id = job
            t0 = time.time()
            try:
                count = await compute_for_category(kind, ref_id)
                _finish_job(kind, ref_id, None)
                logger.info("music category %s:%d → %d recs in %.1fs",
                            kind, ref_id, count, time.time() - t0)
            except Exception as e:
                _finish_job(kind, ref_id, str(e)[:200])
                logger.warning("music category %s:%d failed: %s", kind, ref_id, e)

            await asyncio.sleep(WORKER_BATCH_SLEEP)
        except asyncio.CancelledError:
            logger.info("Music-category-recs worker stopped")
            raise
        except Exception as e:
            logger.error("music category worker loop error: %s", e)
            await asyncio.sleep(5.0)
