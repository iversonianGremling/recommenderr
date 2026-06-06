"""PPR router — exposes /v1/ppr/* endpoints expected by ytvideo."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

router = APIRouter()


def _invalidate_graph_cache(graph_id: int) -> None:
    """Mark a specific graph's feed cache as stale and bump its feed generation
    so downstream consumers (ytfront, ytmusic) know to drop + re-warm their cache."""
    from backend.services import feed_cache
    if graph_id in feed_cache._snapshots:
        feed_cache._snapshots[graph_id].computed_at = 0.0
    feed_cache.bump_generation(graph_id)


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


def _get_ppr_config(graph_id: int = 1) -> dict[str, float]:
    from backend.db import get_db
    conn = get_db()
    rows = conn.execute(
        "SELECT key, value FROM ppr_config WHERE graph_id = ?", (graph_id,)
    ).fetchall()
    conn.close()
    cfg = dict(PPR_CONFIG_DEFAULTS)
    for r in rows:
        if r["key"] in cfg:
            try:
                cfg[r["key"]] = float(r["value"])
            except ValueError:
                pass
    return cfg


def _set_ppr_config(updates: dict[str, float], graph_id: int = 1) -> None:
    from backend.db import get_db
    conn = get_db()
    now = time.time()
    for k, v in updates.items():
        conn.execute(
            "INSERT OR REPLACE INTO ppr_config (graph_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
            (graph_id, k, str(v), now),
        )
    conn.commit()
    conn.close()


@router.get("/config")
async def get_ppr_config(graph_id: int = Query(default=1)) -> dict:
    cfg = await run_in_threadpool(_get_ppr_config, graph_id)
    return {**cfg, "_defaults": PPR_CONFIG_DEFAULTS, "graph_id": graph_id}


class PprConfigUpdate(BaseModel):
    graph_id: int = 1
    watch_base: float | None = None
    playlist_base: float | None = None
    feed_rec_base: float | None = None
    alpha: float | None = None
    min_seed_rating: float | None = None
    compute_spam_mass: float | None = None


@router.put("/config")
async def put_ppr_config(body: PprConfigUpdate) -> dict:
    updates = {
        k: v for k, v in body.model_dump().items()
        if v is not None and k != "graph_id"
    }
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    await run_in_threadpool(_set_ppr_config, updates, body.graph_id)
    # PPR engine config is per-graph now — only this graph's cache is stale.
    _invalidate_graph_cache(body.graph_id)
    return {"ok": True, "updated": list(updates.keys()), "graph_id": body.graph_id}


@router.post("/config/reset")
async def reset_ppr_config(graph_id: int = Query(default=1)) -> dict:
    from backend.db import get_db
    def _reset():
        conn = get_db()
        conn.execute("DELETE FROM ppr_config WHERE graph_id = ?", (graph_id,))
        conn.commit()
        conn.close()
    await run_in_threadpool(_reset)
    _invalidate_graph_cache(graph_id)
    return {"ok": True, "graph_id": graph_id}


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------

class PPRFeedRequest(BaseModel):
    seeds: list[str] = []
    limit: int = 100
    offset: int = 0
    category: str = ""
    sort: str = "score"
    persona_id: int | None = None
    graph_id: int = 1


@router.post("/feed")
async def ppr_feed(req: PPRFeedRequest) -> dict:
    """Return pre-computed PPR feed instantly; triggers background refresh when stale."""
    from backend.services import feed_cache

    await feed_cache.ensure_fresh(req.graph_id)

    snap = feed_cache._get_snapshot(req.graph_id)
    if not snap.items:
        try:
            await feed_cache.wait_for_initial()
        except TimeoutError:
            return {"items": [], "total": 0}

    # Bypass cache for context-specific queries (persona/category/sort)
    use_cache = (
        not req.category and req.sort == "score" and req.persona_id is None
    )
    if not use_cache:
        from backend.db import get_ppr_feed
        items = await run_in_threadpool(
            get_ppr_feed,
            req.limit, req.offset,
            req.category or None,
            req.sort,
            1.0, False,
            req.persona_id, req.graph_id,
        )
        return {"items": items, "total": len(items)}

    items, total = feed_cache.get_page(req.offset, req.limit, req.graph_id)
    return {"items": items, "total": total}


@router.get("/feed/status")
async def ppr_feed_status(graph_id: int = Query(default=1)) -> dict:
    from backend.services import feed_cache
    snap = feed_cache._get_snapshot(graph_id)
    age = time.monotonic() - snap.computed_at if snap.computed_at else None
    return {
        "graph_id": graph_id,
        "items": len(snap.items),
        "age_seconds": round(age, 1) if age is not None else None,
        "is_refreshing": feed_cache._is_refreshing.get(graph_id, False),
    }


# ---------------------------------------------------------------------------
# Why / For-source / Explore
# ---------------------------------------------------------------------------

@router.get("/why/{video_id}")
async def ppr_why(video_id: str, graph_id: int = Query(default=1)) -> dict:
    from backend.services.ppr_engine import explain_recommendation
    try:
        return await run_in_threadpool(explain_recommendation, video_id, graph_id)
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
async def ppr_scores(limit: int = 50, graph_id: int = 1) -> list:
    def _query():
        from backend.db import get_db
        conn = get_db()
        rows = conn.execute(
            """
            SELECT ps.video_id, ps.graph_id, ps.score, ps.spam_mass, ps.computed_at,
                   COALESCE(fr.title, wh.title, pv.title) as title,
                   COALESCE(fr.author, wh.author, pv.author) as author
            FROM ppr_scores ps
            LEFT JOIN feed_recommendations fr ON fr.video_id = ps.video_id
            LEFT JOIN watch_history wh ON wh.video_id = ps.video_id
            LEFT JOIN playlist_videos pv ON pv.video_id = ps.video_id
            WHERE ps.graph_id = ?
            GROUP BY ps.video_id
            ORDER BY ps.score DESC
            LIMIT ?
            """,
            (graph_id, max(1, min(limit, 500))),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await run_in_threadpool(_query)


# ---------------------------------------------------------------------------
# Recompute
# ---------------------------------------------------------------------------

class RecomputeRequest(BaseModel):
    graph_id: int = 1
    # When omitted, fall back to this graph's saved PPR engine config so the
    # canvas "Recompute" buttons honour each graph's independent settings.
    min_seed_rating: int | None = None
    compute_spam_mass: bool | None = None


@router.post("/recompute")
async def ppr_recompute(req: RecomputeRequest) -> dict:
    """Trigger a full synchronous PPR recompute for the given graph and refresh the feed cache."""
    import asyncio
    from backend.services.ppr_engine import update_ppr_scores
    from backend.services import feed_cache
    from backend.db import get_ppr_feed, get_db

    # Resolve unset params from the graph's own PPR config.
    ppr_cfg = await run_in_threadpool(_get_ppr_config, req.graph_id)
    min_seed_rating = (
        req.min_seed_rating if req.min_seed_rating is not None
        else int(ppr_cfg.get("min_seed_rating", 0))
    )
    compute_spam_mass = (
        req.compute_spam_mass if req.compute_spam_mass is not None
        else bool(ppr_cfg.get("compute_spam_mass", 1.0))
    )

    started = time.monotonic()
    try:
        loop = asyncio.get_running_loop()
        # Look up content_type from graphs table
        conn = get_db()
        graph_row = conn.execute("SELECT content_type FROM graphs WHERE id=?", (req.graph_id,)).fetchone()
        conn.close()
        content_type = graph_row["content_type"] if graph_row else "mixed"
        await loop.run_in_executor(
            None,
            lambda: update_ppr_scores(
                graph_id=req.graph_id,
                content_type=content_type,
                min_seed_rating=min_seed_rating,
                compute_spam_mass=compute_spam_mass,
            ),
        )
        gid = req.graph_id
        items = await loop.run_in_executor(
            None,
            lambda: get_ppr_feed(limit=500, offset=0, sort="score", _skip_recompute=True, graph_id=gid),
        )
        from backend.services.feed_cache import _Snapshot
        feed_cache._snapshots[gid] = _Snapshot(items=items, computed_at=time.monotonic())
        feed_cache.bump_generation(gid)
        elapsed = round(time.monotonic() - started, 2)
        return {"ok": True, "elapsed_seconds": elapsed, "items": len(items),
                "generation": feed_cache.get_generation(gid)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Invalidate
# ---------------------------------------------------------------------------

class InvalidateRequest(BaseModel):
    graph_id: int | None = None


@router.post("/invalidate")
async def ppr_invalidate(body: InvalidateRequest = InvalidateRequest()) -> dict:
    from backend.services import feed_cache
    if body.graph_id is not None:
        _invalidate_graph_cache(body.graph_id)
    else:
        for gid, snap in feed_cache._snapshots.items():
            snap.computed_at = 0.0
            feed_cache.bump_generation(gid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Weight rules
# ---------------------------------------------------------------------------

class WeightRuleRequest(BaseModel):
    rule_type: str
    match_value: str
    multiplier: float
    graph_id: int = 1


@router.get("/weight-rules")
async def list_weight_rules(graph_id: int = Query(default=1)) -> list:
    from backend.db import get_weight_rules
    return await run_in_threadpool(get_weight_rules, graph_id)


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
    await run_in_threadpool(add_weight_rule, body.rule_type, body.match_value.strip(), body.multiplier, body.graph_id)
    from backend.services import feed_cache
    _invalidate_graph_cache(body.graph_id)
    return {"ok": True}


@router.delete("/weight-rules/{rule_id}")
async def delete_weight_rule(rule_id: int, graph_id: int = Query(default=1)) -> dict:
    from backend.db import delete_weight_rule
    await run_in_threadpool(delete_weight_rule, rule_id, graph_id)
    _invalidate_graph_cache(graph_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Seeds (current personalization vector)
# ---------------------------------------------------------------------------

@router.get("/seeds")
async def ppr_seeds(limit: int = 200, graph_id: int = Query(default=1)) -> list[dict]:
    """Return the current seed weights with metadata (title, author, reason breakdown)."""
    def _query():
        from backend.services.ppr_engine import get_seed_weights, WATCH_BASE, PLAYLIST_BASE, FEED_REC_BASE
        from backend.db import get_db

        cfg = _get_ppr_config(graph_id)
        min_seed_rating = int(cfg.get("min_seed_rating", 0))
        seeds = get_seed_weights(min_seed_rating=min_seed_rating, graph_id=graph_id)

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


@router.get("/user-signals")
async def ppr_user_signals() -> dict:
    """Counts of each user feedback signal that feeds PPR seed weights."""
    def _q():
        from backend.db import get_db
        conn = get_db()
        watched = conn.execute("SELECT COUNT(*) as c FROM watch_history").fetchone()["c"]
        rated_videos = conn.execute("SELECT COUNT(*) as c FROM video_ratings WHERE rating > 1").fetchone()["c"]
        blocked_videos = conn.execute("SELECT COUNT(*) as c FROM video_ratings WHERE rating <= 1").fetchone()["c"]
        rated_channels = conn.execute("SELECT COUNT(*) as c FROM channel_ratings WHERE rating > 1").fetchone()["c"]
        blocked_channels = conn.execute("SELECT COUNT(*) as c FROM channel_ratings WHERE rating <= 1").fetchone()["c"]
        playlists = conn.execute("SELECT COUNT(DISTINCT playlist_id) as c FROM playlist_videos").fetchone()["c"]
        playlist_items = conn.execute("SELECT COUNT(*) as c FROM playlist_videos").fetchone()["c"]
        rated_albums = conn.execute("SELECT COUNT(*) as c FROM album_ratings WHERE rating > 1").fetchone()["c"]
        conn.close()
        return {
            "watch_history": watched,
            "rated_videos": rated_videos,
            "blocked_videos": blocked_videos,
            "rated_channels": rated_channels,
            "blocked_channels": blocked_channels,
            "playlists": playlists,
            "playlist_items": playlist_items,
            "rated_albums": rated_albums,
        }
    return await run_in_threadpool(_q)


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
        seeds = conn.execute("SELECT COUNT(DISTINCT video_id) as c FROM ppr_scores WHERE graph_id=1").fetchone()["c"]
        conn.close()
        density = round(edges / (unique_nodes * (unique_nodes - 1)), 6) if unique_nodes > 1 else 0.0
        return {"nodes": unique_nodes, "edges": edges, "density": density, "scored_nodes": seeds}

    return await run_in_threadpool(_query)


@router.get("/graph/subgraph")
async def ppr_graph_subgraph(
    mode: str = "top",
    limit: int = 150,
    center: str | None = None,
    direction: str = "in",
    min_weight: float = 0.0,
) -> dict:
    """
    Return a renderable subgraph.

    mode=top   — top-N PPR-scored videos (targets) + their heaviest source videos.
    mode=ego   — BFS neighbourhood of `center`. direction=in|out|both.
    mode=channel — aggregate all edges at channel level; nodes are channels.
    """
    limit = max(10, min(limit, 500))

    def _meta(conn, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        meta: dict[str, dict] = {}
        for r in conn.execute(
            f"SELECT video_id, title, author, thumbnail FROM watch_history WHERE video_id IN ({ph})", ids
        ).fetchall():
            meta[r["video_id"]] = dict(r)
        for r in conn.execute(
            f"SELECT video_id, title, author, thumbnail FROM feed_recommendations WHERE video_id IN ({ph}) GROUP BY video_id", ids
        ).fetchall():
            meta[r["video_id"]] = dict(r)
        return meta

    def _query_top():
        from backend.db import get_db
        conn = get_db()
        n_targets = min(limit // 2, 100)
        n_sources = limit - n_targets

        target_rows = conn.execute(
            "SELECT video_id, score FROM ppr_scores WHERE graph_id=1 ORDER BY score DESC LIMIT ?", (n_targets,)
        ).fetchall()
        if not target_rows:
            conn.close()
            return {"nodes": [], "edges": [], "meta": {"mode": "top", "node_count": 0, "edge_count": 0}}

        targets = {r["video_id"]: r["score"] for r in target_rows}
        ph = ",".join("?" * len(targets))

        source_rows = conn.execute(
            f"""SELECT source_video_id, SUM(weight) as w FROM recommendation_edges
                WHERE target_video_id IN ({ph}) AND weight >= ?
                GROUP BY source_video_id ORDER BY w DESC LIMIT ?""",
            list(targets) + [min_weight, n_sources],
        ).fetchall()
        sources = {r["source_video_id"]: r["w"] for r in source_rows if r["source_video_id"] not in targets}

        all_ids = list(targets) + list(sources)
        ph2 = ",".join("?" * len(all_ids))
        edge_rows = conn.execute(
            f"""SELECT source_video_id as s, target_video_id as t, weight as w
                FROM recommendation_edges
                WHERE source_video_id IN ({ph2}) AND target_video_id IN ({ph2}) AND weight >= ?
                ORDER BY weight DESC LIMIT 5000""",
            all_ids + all_ids + [min_weight],
        ).fetchall()

        m = _meta(conn, all_ids)
        conn.close()

        nodes = [
            {"id": vid, "label": m.get(vid, {}).get("title") or vid,
             "author": m.get(vid, {}).get("author"), "thumbnail": m.get(vid, {}).get("thumbnail"),
             "score": score, "type": "target"}
            for vid, score in targets.items()
        ] + [
            {"id": vid, "label": m.get(vid, {}).get("title") or vid,
             "author": m.get(vid, {}).get("author"), "thumbnail": m.get(vid, {}).get("thumbnail"),
             "score": None, "edge_weight": w, "type": "source"}
            for vid, w in sources.items()
        ]
        edges = [{"source": r["s"], "target": r["t"], "weight": r["w"]} for r in edge_rows]
        return {"nodes": nodes, "edges": edges,
                "meta": {"mode": "top", "node_count": len(nodes), "edge_count": len(edges)}}

    def _query_ego():
        if not center:
            raise HTTPException(400, "center required for ego mode")
        from backend.db import get_db
        conn = get_db()

        visited: set[str] = {center}
        all_edges: list[dict] = []
        frontier: set[str] = {center}

        for _ in range(2):
            if len(visited) >= limit or not frontier:
                break
            ph = ",".join("?" * len(frontier))
            new_nodes: set[str] = set()

            if direction in ("out", "both"):
                for r in conn.execute(
                    f"SELECT source_video_id as s, target_video_id as t, weight as w "
                    f"FROM recommendation_edges WHERE source_video_id IN ({ph}) AND weight >= ? "
                    f"ORDER BY weight DESC LIMIT ?",
                    list(frontier) + [min_weight, limit * 4],
                ).fetchall():
                    if len(visited) < limit and r["t"] not in visited:
                        new_nodes.add(r["t"]); visited.add(r["t"])
                    all_edges.append({"source": r["s"], "target": r["t"], "weight": r["w"]})

            if direction in ("in", "both"):
                for r in conn.execute(
                    f"SELECT source_video_id as s, target_video_id as t, weight as w "
                    f"FROM recommendation_edges WHERE target_video_id IN ({ph}) AND weight >= ? "
                    f"ORDER BY weight DESC LIMIT ?",
                    list(frontier) + [min_weight, limit * 4],
                ).fetchall():
                    if len(visited) < limit and r["s"] not in visited:
                        new_nodes.add(r["s"]); visited.add(r["s"])
                    all_edges.append({"source": r["s"], "target": r["t"], "weight": r["w"]})

            frontier = new_nodes

        scores = {r["video_id"]: r["score"] for r in conn.execute(
            f"SELECT video_id, score FROM ppr_scores WHERE graph_id=1 AND video_id IN ({','.join('?' * len(visited))})",
            list(visited),
        ).fetchall()}
        m = _meta(conn, list(visited))
        conn.close()

        def ntype(vid: str) -> str:
            if vid == center: return "center"
            if vid in scores: return "scored"
            return "neighbor"

        nodes = [
            {"id": vid, "label": m.get(vid, {}).get("title") or vid,
             "author": m.get(vid, {}).get("author"), "thumbnail": m.get(vid, {}).get("thumbnail"),
             "score": scores.get(vid), "type": ntype(vid)}
            for vid in visited
        ]
        seen: set[tuple] = set()
        edges = []
        for e in all_edges:
            k = (e["source"], e["target"])
            if k not in seen and e["source"] in visited and e["target"] in visited:
                seen.add(k); edges.append(e)
        return {"nodes": nodes, "edges": edges,
                "meta": {"mode": "ego", "center": center, "node_count": len(nodes), "edge_count": len(edges)}}

    def _query_channel():
        from backend.db import get_db
        conn = get_db()
        rows = conn.execute(
            """
            WITH top_edges AS (
                SELECT source_video_id, target_video_id, weight
                FROM recommendation_edges WHERE weight >= ?
                ORDER BY weight DESC LIMIT 200000
            )
            SELECT fs.author AS src, ft.author AS tgt,
                   SUM(te.weight) AS w, COUNT(*) AS cnt
            FROM top_edges te
            JOIN (SELECT video_id, author FROM feed_recommendations GROUP BY video_id) fs
                ON fs.video_id = te.source_video_id
            JOIN (SELECT video_id, author FROM feed_recommendations GROUP BY video_id) ft
                ON ft.video_id = te.target_video_id
            WHERE fs.author IS NOT NULL AND ft.author IS NOT NULL AND fs.author != ft.author
            GROUP BY src, tgt ORDER BY w DESC LIMIT ?
            """,
            (min_weight, limit * 3),
        ).fetchall()
        conn.close()

        channels: set[str] = set()
        edges = []
        for r in rows:
            channels.add(r["src"]); channels.add(r["tgt"])
            edges.append({"source": r["src"], "target": r["tgt"], "weight": r["w"], "edge_count": r["cnt"]})
            if len(channels) >= limit:
                break

        edges = [e for e in edges if e["source"] in channels and e["target"] in channels]
        nodes = [{"id": ch, "label": ch, "type": "channel"} for ch in channels]
        return {"nodes": nodes, "edges": edges,
                "meta": {"mode": "channel", "node_count": len(nodes), "edge_count": len(edges)}}

    fn = {"top": _query_top, "ego": _query_ego, "channel": _query_channel}.get(mode)
    if fn is None:
        raise HTTPException(400, "mode must be top | ego | channel")
    return await run_in_threadpool(fn)


# ---------------------------------------------------------------------------
# Feed filters
# ---------------------------------------------------------------------------

class FeedFilterRequest(BaseModel):
    filter_type: str
    match_value: str
    graph_id: int = 1

_VALID_FILTER_TYPES = {"channel_id", "channel_name", "keyword", "video_id"}


@router.get("/feed-filters")
async def list_feed_filters(graph_id: int = Query(default=1)) -> list:
    from backend.db import get_feed_filters
    return await run_in_threadpool(get_feed_filters, graph_id)


@router.get("/video-keywords/{video_id}")
async def video_keywords(video_id: str, graph_id: int = Query(default=1)) -> dict:
    """A video's YouTube tags, for the dislike UI's 'stop showing me <tag>' chips.
    `filtered` marks tags already covered by an active keyword feed-filter."""
    from backend.db import get_db

    def _q():
        conn = get_db()
        kws = [r["keyword"] for r in conn.execute(
            "SELECT keyword FROM video_keywords WHERE video_id=? ORDER BY LENGTH(keyword), keyword",
            (video_id,),
        )]
        active = [r["match_value"].lower() for r in conn.execute(
            "SELECT match_value FROM feed_filters WHERE graph_id=? AND filter_type='keyword'",
            (graph_id,),
        )]
        conn.close()
        filtered = sorted({k for k in kws if any(a in k.lower() for a in active)})
        return {"video_id": video_id, "keywords": kws, "filtered": filtered}

    return await run_in_threadpool(_q)


@router.post("/feed-filters")
async def add_feed_filter_ep(body: FeedFilterRequest) -> dict:
    from backend.db import add_feed_filter
    if body.filter_type not in _VALID_FILTER_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid filter_type; allowed: {sorted(_VALID_FILTER_TYPES)}")
    if not body.match_value.strip():
        raise HTTPException(status_code=400, detail="match_value required")
    await run_in_threadpool(add_feed_filter, body.filter_type, body.match_value.strip(), body.graph_id)
    _invalidate_graph_cache(body.graph_id)
    return {"ok": True}


@router.delete("/feed-filters/{filter_id}")
async def delete_feed_filter_ep(filter_id: int, graph_id: int = Query(default=1)) -> dict:
    from backend.db import delete_feed_filter
    await run_in_threadpool(delete_feed_filter, filter_id, graph_id)
    _invalidate_graph_cache(graph_id)
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


# ---------------------------------------------------------------------------
# Cosine similarity scorer
# ---------------------------------------------------------------------------

class CosineRecomputeRequest(BaseModel):
    min_seed_rating: int = 0
    graph_id: int = 1


@router.post("/cosine/recompute")
async def cosine_recompute(req: CosineRecomputeRequest) -> dict:
    """Recompute cosine-similarity scores and store in cosine_scores table."""
    started = time.time()
    from backend.services.cosine_engine import update_cosine_scores
    from backend.db import get_db
    try:
        conn = get_db()
        row = conn.execute("SELECT content_type FROM graphs WHERE id=?", (req.graph_id,)).fetchone()
        conn.close()
        content_type = row["content_type"] if row else "mixed"
        n = await run_in_threadpool(update_cosine_scores, req.graph_id, content_type, req.min_seed_rating)
        return {"ok": True, "scored": n, "elapsed_seconds": round(time.time() - started, 2)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/cosine/scores")
async def cosine_scores(limit: int = 100, graph_id: int = 1) -> list:
    """Return top cosine-scored videos with metadata."""
    def _query():
        from backend.db import get_db
        conn = get_db()
        rows = conn.execute(
            """
            SELECT cs.video_id, cs.graph_id, cs.score, cs.computed_at,
                   COALESCE(fr.title, wh.title, pv.title) as title,
                   COALESCE(fr.author, wh.author, pv.author) as author,
                   COALESCE(fr.thumbnail, pv.thumbnail) as thumbnail,
                   COALESCE(fr.duration, pv.duration) as duration
            FROM cosine_scores cs
            LEFT JOIN feed_recommendations fr ON fr.video_id = cs.video_id
            LEFT JOIN watch_history wh ON wh.video_id = cs.video_id
            LEFT JOIN playlist_videos pv ON pv.video_id = cs.video_id
            WHERE cs.graph_id = ?
            GROUP BY cs.video_id
            ORDER BY cs.score DESC
            LIMIT ?
            """,
            (graph_id, max(1, min(limit, 500))),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    return await run_in_threadpool(_query)


# ---------------------------------------------------------------------------
# Pipeline status — aggregate stats for the pipeline dashboard
# ---------------------------------------------------------------------------

@router.get("/pipeline/status")
async def pipeline_status(graph_id: int = 1) -> dict:
    def _query():
        from backend.db import get_db, get_pipeline_config
        from backend.services.source_registry import list_sources
        from backend.services.ppr_engine import _GRAPH_EDGE_QUERIES

        conn = get_db()

        # Graph-scoped edge/node count using content_type filter
        graph_row = conn.execute("SELECT content_type FROM graphs WHERE id=?", (graph_id,)).fetchone()
        content_type = graph_row["content_type"] if graph_row else "mixed"

        edge_query = _GRAPH_EDGE_QUERIES.get(content_type, _GRAPH_EDGE_QUERIES["mixed"])
        # Wrap to count only
        edge_count_sql = f"SELECT COUNT(*) as c FROM ({edge_query})"
        graph_edges = conn.execute(edge_count_sql).fetchone()["c"]
        node_count_sql = f"""
            SELECT COUNT(*) as c FROM (
                SELECT source_video_id as v FROM ({edge_query})
                UNION SELECT target_video_id FROM ({edge_query})
            )
        """
        graph_nodes = conn.execute(node_count_sql).fetchone()["c"]

        ppr_row = conn.execute(
            "SELECT COUNT(*) as c, MAX(computed_at) as ts FROM ppr_scores WHERE graph_id=?",
            (graph_id,)
        ).fetchone()
        cosine_row = conn.execute(
            "SELECT COUNT(*) as c, MAX(computed_at) as ts FROM cosine_scores WHERE graph_id=?",
            (graph_id,)
        ).fetchone()
        serendipity_row = conn.execute(
            "SELECT COUNT(*) as c, MAX(computed_at) as ts FROM serendipity_scores WHERE graph_id=?",
            (graph_id,)
        ).fetchone()
        try:
            embedding_row = conn.execute(
                "SELECT COUNT(*) as c, MAX(computed_at) as ts FROM embedding_scores WHERE graph_id=?",
                (graph_id,)
            ).fetchone()
        except Exception:
            embedding_row = {"c": 0, "ts": None}

        filter_count = conn.execute(
            "SELECT COUNT(*) as c FROM feed_filters WHERE graph_id=?", (graph_id,)
        ).fetchone()["c"]
        weight_rule_count = conn.execute(
            "SELECT COUNT(*) as c FROM weight_rules WHERE graph_id=?", (graph_id,)
        ).fetchone()["c"]
        feed_count = conn.execute(
            "SELECT COUNT(*) as c FROM graph_feed_items WHERE graph_id=?", (graph_id,)
        ).fetchone()["c"]

        watched = conn.execute("SELECT COUNT(*) as c FROM watch_history").fetchone()["c"]
        rated_v = conn.execute("SELECT COUNT(*) as c FROM video_ratings WHERE rating > 1").fetchone()["c"]
        rated_ch = conn.execute("SELECT COUNT(*) as c FROM channel_ratings WHERE rating > 1").fetchone()["c"]
        playlist_items = conn.execute("SELECT COUNT(*) as c FROM playlist_videos").fetchone()["c"]

        # Graph-scoped content sources (only those assigned to this graph)
        graph_source_names = {
            r[0] for r in conn.execute(
                "SELECT source_name FROM graph_sources WHERE graph_id=?", (graph_id,)
            ).fetchall()
        }
        sources_raw = list_sources()
        graph_sources = [s for s in sources_raw if s["name"] in graph_source_names]
        enabled = [s for s in graph_sources if s.get("enabled") and not s.get("circuit_open")]
        open_circuits = [s for s in graph_sources if s.get("circuit_open")]

        # Signal sources
        try:
            signal_rows = conn.execute("SELECT * FROM signal_sources ORDER BY id").fetchall()
            signal_sources = [
                {
                    "id": r["id"], "name": r["name"], "kind": r["kind"],
                    "endpoint_url": r["endpoint_url"], "converter": r["converter"],
                    "auth_header": r["auth_header"], "enabled": bool(r["enabled"]),
                    "is_system": bool(r["is_system"]),
                    "last_synced_at": r["last_synced_at"],
                    "last_count": r["last_count"], "last_error": r["last_error"],
                }
                for r in signal_rows
            ]
        except Exception:
            signal_sources = []

        conn.close()

        cfg = get_pipeline_config(graph_id=graph_id)

        return {
            "config": cfg,
            "user_signals": {
                "watch_history": watched,
                "rated_videos": rated_v,
                "rated_channels": rated_ch,
                "playlist_items": playlist_items,
            },
            "signal_sources": signal_sources,
            "sources": {
                "total": len(graph_sources),
                "enabled": len(enabled),
                "circuit_open": len(open_circuits),
                "names": [s["name"] for s in enabled],
            },
            "graph": {"nodes": graph_nodes, "edges": graph_edges},
            "scorers": [
                {"id": "ppr", "name": "PPR", "description": "Personalized PageRank",
                 "scored": ppr_row["c"], "computed_at": ppr_row["ts"],
                 "enabled": bool(cfg.get("scorer.ppr.enabled", 1.0)), "weight": cfg.get("scorer.ppr.weight", 1.0)},
                {"id": "cosine", "name": "Cosine", "description": "Neighborhood overlap",
                 "scored": cosine_row["c"], "computed_at": cosine_row["ts"],
                 "enabled": bool(cfg.get("scorer.cosine.enabled", 0.0)), "weight": cfg.get("scorer.cosine.weight", 0.5)},
                {"id": "serendipity", "name": "Serendipity", "description": "Multi-hop surprise",
                 "scored": serendipity_row["c"], "computed_at": serendipity_row["ts"],
                 "enabled": bool(cfg.get("scorer.serendipity.enabled", 0.0)), "weight": cfg.get("scorer.serendipity.weight", 0.5)},
                {"id": "embedding", "name": "Embedding", "description": "Semantic similarity (Rocchio)",
                 "scored": embedding_row["c"], "computed_at": embedding_row["ts"],
                 "enabled": bool(cfg.get("scorer.embedding.enabled", 0.0)), "weight": cfg.get("scorer.embedding.weight", 0.5)},
            ],
            "filters": {"feed_filter_count": filter_count, "weight_rule_count": weight_rule_count},
            "feed": {"items": feed_count},
        }

    return await run_in_threadpool(_query)


# ---------------------------------------------------------------------------
# Pipeline config endpoints
# ---------------------------------------------------------------------------

PIPELINE_CONFIG_DEFAULTS = {
    "temporal.recency_halflife_days": 0.0,
    "scorer.ppr.enabled": 1.0,
    "scorer.ppr.weight": 1.0,
    "scorer.cosine.enabled": 0.0,
    "scorer.cosine.weight": 0.5,
    "scorer.serendipity.enabled": 0.0,
    "scorer.serendipity.weight": 0.5,
    "scorer.embedding.enabled": 0.0,
    "scorer.embedding.weight": 0.5,
    "diversity.enabled": 0.0,
    "diversity.lambda": 0.7,
    "diversity.max_per_channel": 3.0,
}


@router.get("/pipeline/config")
async def get_pipeline_config_ep(graph_id: int = Query(default=1)) -> dict:
    from backend.db import get_pipeline_config
    cfg = await run_in_threadpool(get_pipeline_config, graph_id)
    return {**cfg, "_defaults": PIPELINE_CONFIG_DEFAULTS}


class PipelineConfigUpdate(BaseModel):
    updates: dict[str, float]
    graph_id: int = 1


@router.put("/pipeline/config")
async def put_pipeline_config_ep(body: PipelineConfigUpdate) -> dict:
    from backend.db import set_pipeline_config
    valid_keys = set(PIPELINE_CONFIG_DEFAULTS.keys())
    filtered = {k: v for k, v in body.updates.items() if k in valid_keys}
    if not filtered:
        raise HTTPException(400, "No valid keys to update")
    await run_in_threadpool(set_pipeline_config, filtered, body.graph_id)
    _invalidate_graph_cache(body.graph_id)
    return {"ok": True, "updated": list(filtered.keys())}


class SerendipityRecomputeRequest(BaseModel):
    graph_id: int = 1


@router.post("/pipeline/serendipity/recompute")
async def pipeline_serendipity_recompute(body: SerendipityRecomputeRequest = SerendipityRecomputeRequest()) -> dict:
    started = time.time()
    from backend.services.serendipity_engine import update_serendipity_scores
    n = await run_in_threadpool(update_serendipity_scores, body.graph_id)
    return {"ok": True, "scored": n, "elapsed_seconds": round(time.time() - started, 2)}


# ---------------------------------------------------------------------------
# Embedding (semantic) scorer
# ---------------------------------------------------------------------------

class EmbeddingEmbedRequest(BaseModel):
    limit: int = 500


class EmbeddingRecomputeRequest(BaseModel):
    graph_id: int = 1
    limit: int = 500   # how many videos to (re-)embed before scoring


@router.get("/pipeline/embedding/status")
async def pipeline_embedding_status(graph_id: int = Query(default=1)) -> dict:
    from backend.services.embedding_engine import ollama_health
    from backend.db import get_db

    def _counts():
        conn = get_db()
        try:
            total = conn.execute("SELECT COUNT(*) c FROM video_embeddings").fetchone()["c"]
            scored = conn.execute(
                "SELECT COUNT(*) c FROM embedding_scores WHERE graph_id=?", (graph_id,)
            ).fetchone()["c"]
        except Exception:
            total, scored = 0, 0
        conn.close()
        return total, scored

    health = await run_in_threadpool(ollama_health)
    total, scored = await run_in_threadpool(_counts)
    return {"ollama": health, "embedded_videos": total, "scored_candidates": scored}


@router.post("/pipeline/embedding/embed")
async def pipeline_embedding_embed(body: EmbeddingEmbedRequest = EmbeddingEmbedRequest()) -> dict:
    """Generate/refresh embeddings for catalog videos (talks to ollama). Safe to
    call repeatedly — only missing/stale videos are embedded; `remaining` > 0
    means call again to continue."""
    started = time.time()
    from backend.services.embedding_engine import embed_catalog
    res = await run_in_threadpool(embed_catalog, body.limit)
    return {"ok": True, **res, "elapsed_seconds": round(time.time() - started, 2)}


@router.post("/pipeline/embedding/recompute")
async def pipeline_embedding_recompute(body: EmbeddingRecomputeRequest = EmbeddingRecomputeRequest()) -> dict:
    """Embed any new/changed videos, then rebuild this graph's embedding_scores
    from the Rocchio taste vector, and invalidate the feed cache."""
    started = time.time()
    from backend.services.embedding_engine import embed_catalog, update_embedding_scores
    from backend.db import get_db

    def _content_type():
        conn = get_db()
        row = conn.execute("SELECT content_type FROM graphs WHERE id=?", (body.graph_id,)).fetchone()
        conn.close()
        return row["content_type"] if row else "mixed"

    embed_res = await run_in_threadpool(embed_catalog, body.limit)
    content_type = await run_in_threadpool(_content_type)
    scored = await run_in_threadpool(update_embedding_scores, body.graph_id, content_type)
    _invalidate_graph_cache(body.graph_id)
    return {"ok": True, "embed": embed_res, "scored": scored,
            "elapsed_seconds": round(time.time() - started, 2)}


@router.get("/pipeline/serendipity/scores")
async def pipeline_serendipity_scores(limit: int = 100, graph_id: int = Query(default=1)) -> list:
    def _q():
        from backend.db import get_db
        conn = get_db()
        rows = conn.execute("""
            SELECT ss.video_id, ss.score, ss.computed_at,
                   COALESCE(fr.title, wh.title) as title,
                   COALESCE(fr.author, wh.author) as author,
                   fr.thumbnail, fr.duration
            FROM serendipity_scores ss
            LEFT JOIN feed_recommendations fr ON fr.video_id = ss.video_id
            LEFT JOIN watch_history wh ON wh.video_id = ss.video_id
            WHERE ss.graph_id = ?
            GROUP BY ss.video_id
            ORDER BY ss.score DESC LIMIT ?
        """, (graph_id, max(1, min(limit, 500)),)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await run_in_threadpool(_q)
