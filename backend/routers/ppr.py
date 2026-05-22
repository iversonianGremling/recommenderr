"""PPR router — exposes /v1/ppr/* endpoints expected by ytvideo."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

router = APIRouter()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PPR_CONFIG_DEFAULTS: dict[str, float] = {
    "watch_base": 0.01,
    "playlist_base": 3.0,
    "feed_rec_base": 1.5,
    "alpha": 0.15,
    "min_seed_rating": 0.0,
    "compute_spam_mass": 1.0,  # 1=true, 0=false
}


def _get_ppr_config() -> dict[str, float]:
    from backend.db import get_db
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM ppr_config").fetchall()
    conn.close()
    cfg = dict(PPR_CONFIG_DEFAULTS)
    for r in rows:
        if r["key"] in cfg:
            try:
                cfg[r["key"]] = float(r["value"])
            except ValueError:
                pass
    return cfg


def _set_ppr_config(updates: dict[str, float]) -> None:
    from backend.db import get_db
    conn = get_db()
    now = time.time()
    for k, v in updates.items():
        conn.execute(
            "INSERT OR REPLACE INTO ppr_config (key, value, updated_at) VALUES (?, ?, ?)",
            (k, str(v), now),
        )
    conn.commit()
    conn.close()


@router.get("/config")
async def get_ppr_config() -> dict:
    cfg = await run_in_threadpool(_get_ppr_config)
    return {**cfg, "_defaults": PPR_CONFIG_DEFAULTS}


class PprConfigUpdate(BaseModel):
    watch_base: float | None = None
    playlist_base: float | None = None
    feed_rec_base: float | None = None
    alpha: float | None = None
    min_seed_rating: float | None = None
    compute_spam_mass: float | None = None


@router.put("/config")
async def put_ppr_config(body: PprConfigUpdate) -> dict:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    await run_in_threadpool(_set_ppr_config, updates)
    from backend.services import feed_cache
    feed_cache._snapshot.computed_at = 0.0
    return {"ok": True, "updated": list(updates.keys())}


@router.post("/config/reset")
async def reset_ppr_config() -> dict:
    from backend.db import get_db
    def _reset():
        conn = get_db()
        conn.execute("DELETE FROM ppr_config")
        conn.commit()
        conn.close()
    await run_in_threadpool(_reset)
    from backend.services import feed_cache
    feed_cache._snapshot.computed_at = 0.0
    return {"ok": True}


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------

class PPRFeedRequest(BaseModel):
    seeds: list[str] = []
    limit: int = 100
    offset: int = 0
    category: str = ""
    sort: str = "score"


@router.post("/feed")
async def ppr_feed(req: PPRFeedRequest) -> dict:
    """Return pre-computed PPR feed instantly; triggers background refresh when stale."""
    from backend.services import feed_cache

    await feed_cache.ensure_fresh()

    if not feed_cache._snapshot.items:
        try:
            await feed_cache.wait_for_initial()
        except TimeoutError:
            return {"items": [], "total": 0}

    if req.category or req.sort != "score":
        from backend.db import get_ppr_feed
        items = await run_in_threadpool(
            get_ppr_feed,
            req.limit, req.offset,
            req.category or None,
            req.sort,
        )
        return {"items": items, "total": len(items)}

    items, total = feed_cache.get_page(req.offset, req.limit)
    return {"items": items, "total": total}


@router.get("/feed/status")
async def ppr_feed_status() -> dict:
    from backend.services import feed_cache
    snap = feed_cache._snapshot
    age = time.monotonic() - snap.computed_at if snap.computed_at else None
    return {
        "items": len(snap.items),
        "age_seconds": round(age, 1) if age is not None else None,
        "is_refreshing": feed_cache._is_refreshing,
    }


# ---------------------------------------------------------------------------
# Why / For-source / Explore
# ---------------------------------------------------------------------------

@router.get("/why/{video_id}")
async def ppr_why(video_id: str) -> dict:
    from backend.services.ppr_engine import explain_recommendation
    try:
        return await run_in_threadpool(explain_recommendation, video_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/for-source/{video_id}")
async def ppr_for_source(video_id: str, limit: int = 24) -> dict:
    from backend.db import get_db
    lim = max(1, min(int(limit), 48))

    def _query():
        conn = get_db()
        rows = conn.execute(
            """
            SELECT re.target_video_id as video_id, re.weight,
                   COALESCE(fr.title, pv.title, wh.title) as title,
                   COALESCE(fr.author, pv.author, wh.author) as author,
                   COALESCE(fr.author_id, pv.author_id, wh.author_id) as author_id,
                   COALESCE(fr.thumbnail, pv.thumbnail, wh.thumbnail) as thumbnail,
                   COALESCE(fr.duration, pv.duration) as duration
            FROM recommendation_edges re
            LEFT JOIN feed_recommendations fr ON fr.video_id = re.target_video_id
            LEFT JOIN playlist_videos pv ON pv.video_id = re.target_video_id
            LEFT JOIN watch_history wh ON wh.video_id = re.target_video_id
            WHERE re.source_video_id = ?
            GROUP BY re.target_video_id
            ORDER BY re.weight DESC
            LIMIT ?
            """,
            (video_id, lim),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    try:
        videos = await run_in_threadpool(_query)
        return {"videos": videos, "total": len(videos)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class ExploreRequest(BaseModel):
    seeds: list[dict] = []
    limit: int = 50


@router.post("/explore")
async def ppr_explore(req: ExploreRequest) -> list:
    from backend.services.ppr_engine import explore_from_seeds
    try:
        return await run_in_threadpool(explore_from_seeds, req.seeds, req.limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

@router.get("/scores")
async def ppr_scores(limit: int = 50) -> list:
    def _query():
        from backend.db import get_db
        conn = get_db()
        rows = conn.execute(
            """
            SELECT ps.video_id, ps.score, ps.spam_mass, ps.computed_at,
                   COALESCE(fr.title, wh.title, pv.title) as title,
                   COALESCE(fr.author, wh.author, pv.author) as author
            FROM ppr_scores ps
            LEFT JOIN feed_recommendations fr ON fr.video_id = ps.video_id
            LEFT JOIN watch_history wh ON wh.video_id = ps.video_id
            LEFT JOIN playlist_videos pv ON pv.video_id = ps.video_id
            ORDER BY ps.score DESC
            LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await run_in_threadpool(_query)


# ---------------------------------------------------------------------------
# Recompute
# ---------------------------------------------------------------------------

class RecomputeRequest(BaseModel):
    min_seed_rating: int = 0
    compute_spam_mass: bool = True


@router.post("/recompute")
async def ppr_recompute(req: RecomputeRequest) -> dict:
    """Trigger a full synchronous PPR recompute and refresh the feed cache."""
    import asyncio
    from backend.services.ppr_engine import update_ppr_scores
    from backend.services import feed_cache
    from backend.db import get_ppr_feed

    started = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: update_ppr_scores(
                min_seed_rating=req.min_seed_rating,
                compute_spam_mass=req.compute_spam_mass,
            ),
        )
        items = await loop.run_in_executor(
            None,
            lambda: get_ppr_feed(limit=500, offset=0, sort="score", _skip_recompute=True),
        )
        from backend.services.feed_cache import _Snapshot
        feed_cache._snapshot = _Snapshot(items=items, computed_at=time.monotonic())
        elapsed = round(time.monotonic() - started, 2)
        return {"ok": True, "elapsed_seconds": elapsed, "items": len(items)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Invalidate
# ---------------------------------------------------------------------------

@router.post("/invalidate")
async def ppr_invalidate() -> dict:
    from backend.services import feed_cache
    feed_cache._snapshot.computed_at = 0.0
    return {"ok": True}


# ---------------------------------------------------------------------------
# Weight rules
# ---------------------------------------------------------------------------

class WeightRuleRequest(BaseModel):
    rule_type: str
    match_value: str
    multiplier: float


@router.get("/weight-rules")
async def list_weight_rules() -> list:
    from backend.db import get_weight_rules
    return await run_in_threadpool(get_weight_rules)


@router.post("/weight-rules")
async def add_weight_rule(body: WeightRuleRequest) -> dict:
    from backend.db import add_weight_rule
    _VALID_RULE_TYPES = {"keyword", "channel_id", "channel_name", "genre", "category", "attribute"}
    if body.rule_type not in _VALID_RULE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid rule_type; allowed: {sorted(_VALID_RULE_TYPES)}")
    if not body.match_value.strip():
        raise HTTPException(status_code=400, detail="match_value required")
    if body.multiplier <= 0:
        raise HTTPException(status_code=400, detail="multiplier must be > 0")
    await run_in_threadpool(add_weight_rule, body.rule_type, body.match_value.strip(), body.multiplier)
    from backend.services import feed_cache
    feed_cache._snapshot.computed_at = 0.0
    return {"ok": True}


@router.delete("/weight-rules/{rule_id}")
async def delete_weight_rule(rule_id: int) -> dict:
    from backend.db import delete_weight_rule
    await run_in_threadpool(delete_weight_rule, rule_id)
    from backend.services import feed_cache
    feed_cache._snapshot.computed_at = 0.0
    return {"ok": True}


# ---------------------------------------------------------------------------
# Seeds (current personalization vector)
# ---------------------------------------------------------------------------

@router.get("/seeds")
async def ppr_seeds(limit: int = 200) -> list[dict]:
    """Return the current seed weights with metadata (title, author, reason breakdown)."""
    def _query():
        from backend.services.ppr_engine import get_seed_weights, WATCH_BASE, PLAYLIST_BASE, FEED_REC_BASE
        from backend.db import get_db

        cfg = _get_ppr_config()
        min_seed_rating = int(cfg.get("min_seed_rating", 0))
        seeds = get_seed_weights(min_seed_rating=min_seed_rating)

        if not seeds:
            return []

        conn = get_db()
        ph = ",".join("?" * len(seeds))
        vids = list(seeds.keys())

        meta = {
            r["video_id"]: dict(r)
            for r in conn.execute(
                f"""
                SELECT video_id,
                       COALESCE(fr.title, wh.title, pv.title) as title,
                       COALESCE(fr.author, wh.author, pv.author) as author
                FROM (SELECT video_id FROM watch_history WHERE video_id IN ({ph})
                      UNION
                      SELECT video_id FROM playlist_videos WHERE video_id IN ({ph})
                      UNION
                      SELECT video_id FROM feed_recommendations WHERE video_id IN ({ph})) v
                LEFT JOIN feed_recommendations fr USING (video_id)
                LEFT JOIN watch_history wh USING (video_id)
                LEFT JOIN playlist_videos pv USING (video_id)
                GROUP BY video_id
                """,
                vids + vids + vids,
            ).fetchall()
        }

        watched = {r["video_id"] for r in conn.execute(f"SELECT video_id FROM watch_history WHERE video_id IN ({ph})", vids).fetchall()}
        in_playlist = {r["video_id"] for r in conn.execute(f"SELECT DISTINCT video_id FROM playlist_videos WHERE video_id IN ({ph})", vids).fetchall()}
        conn.close()

        result = []
        for vid, weight in sorted(seeds.items(), key=lambda x: -x[1]):
            reasons = []
            if vid in watched:
                reasons.append(f"watched (+{WATCH_BASE})")
            if vid in in_playlist:
                reasons.append(f"playlist (+{PLAYLIST_BASE})")
            m = meta.get(vid, {})
            result.append({
                "video_id": vid,
                "weight": round(weight, 4),
                "title": m.get("title"),
                "author": m.get("author"),
                "reasons": reasons,
            })

        return result[:max(1, min(limit, 1000))]

    return await run_in_threadpool(_query)


# ---------------------------------------------------------------------------
# Graph stats
# ---------------------------------------------------------------------------

@router.get("/graph/stats")
async def ppr_graph_stats() -> dict:
    """Return basic graph statistics: node count, edge count, density."""
    def _query():
        from backend.db import get_db
        conn = get_db()
        edges = conn.execute("SELECT COUNT(*) as c FROM recommendation_edges").fetchone()["c"]
        nodes_row = conn.execute(
            "SELECT COUNT(DISTINCT source_video_id) + COUNT(DISTINCT target_video_id) as c FROM recommendation_edges"
        ).fetchone()
        unique_nodes = conn.execute(
            """
            SELECT COUNT(*) as c FROM (
                SELECT source_video_id as v FROM recommendation_edges
                UNION
                SELECT target_video_id FROM recommendation_edges
            )
            """
        ).fetchone()["c"]
        seeds = conn.execute("SELECT COUNT(DISTINCT video_id) as c FROM ppr_scores").fetchone()["c"]
        conn.close()
        density = round(edges / (unique_nodes * (unique_nodes - 1)), 6) if unique_nodes > 1 else 0.0
        return {"nodes": unique_nodes, "edges": edges, "density": density, "scored_nodes": seeds}

    return await run_in_threadpool(_query)


# ---------------------------------------------------------------------------
# Feed filters
# ---------------------------------------------------------------------------

class FeedFilterRequest(BaseModel):
    filter_type: str
    match_value: str

_VALID_FILTER_TYPES = {"channel_id", "channel_name", "keyword", "video_id"}


@router.get("/feed-filters")
async def list_feed_filters() -> list:
    from backend.db import get_feed_filters
    return await run_in_threadpool(get_feed_filters)


@router.post("/feed-filters")
async def add_feed_filter_ep(body: FeedFilterRequest) -> dict:
    from backend.db import add_feed_filter
    if body.filter_type not in _VALID_FILTER_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid filter_type; allowed: {sorted(_VALID_FILTER_TYPES)}")
    if not body.match_value.strip():
        raise HTTPException(status_code=400, detail="match_value required")
    await run_in_threadpool(add_feed_filter, body.filter_type, body.match_value.strip())
    from backend.services import feed_cache
    feed_cache._snapshot.computed_at = 0.0
    return {"ok": True}


@router.delete("/feed-filters/{filter_id}")
async def delete_feed_filter_ep(filter_id: int) -> dict:
    from backend.db import delete_feed_filter
    await run_in_threadpool(delete_feed_filter, filter_id)
    from backend.services import feed_cache
    feed_cache._snapshot.computed_at = 0.0
    return {"ok": True}


# ---------------------------------------------------------------------------
# Track search (for radio seed picker — no auth required)
# ---------------------------------------------------------------------------

@router.get("/track-search")
async def ppr_track_search(q: str) -> list[dict]:
    """Quick Last.fm track search for the radio seed picker UI."""
    from backend.services.music_client import lastfm_search_track
    try:
        results = await lastfm_search_track(q.strip(), limit=12)
        return [{"track": r.get("track", ""), "artist": r.get("artist", "")} for r in results]
    except Exception:
        return []
