"""Server-side per-graph feed page cache.

Pre-computes PRECOMPUTE_LIMIT feed items per graph in background tasks and serves
slices immediately.  A request never blocks on PPR recomputation — stale data
is returned while a background refresh runs, then swapped atomically.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("feed_cache")

PRECOMPUTE_LIMIT = 500
REFRESH_INTERVAL = 270.0   # seconds before cache is considered stale (~4.5 min)


@dataclass
class _Snapshot:
    items: list[dict] = field(default_factory=list)
    computed_at: float = 0.0


_snapshots: dict[int, _Snapshot] = {}
_is_refreshing: dict[int, bool] = {}
_refresh_locks: dict[int, asyncio.Lock] = {}
_initial_ready: asyncio.Event | None = None

# Monotonic per-graph "feed generation". Bumped whenever the feed for a graph
# changes in a way consumers should react to — either invalidated (a weight /
# rule / filter / config edit) or rebuilt with fresh content. Downstream feed
# consumers (ytfront, ytmusic) poll this and, when it changes, drop their local
# feed cache and re-warm it. See routers/feed_named.py: GET /v1/feed/generations.
_generations: dict[int, int] = {}


def bump_generation(graph_id: int) -> int:
    """Advance a graph's feed generation and return the new value."""
    _generations[graph_id] = _generations.get(graph_id, 0) + 1
    return _generations[graph_id]


def get_generation(graph_id: int) -> int:
    return _generations.get(graph_id, 0)


def get_all_generations() -> dict[int, int]:
    return dict(_generations)


def _get_snapshot(graph_id: int) -> _Snapshot:
    if graph_id not in _snapshots:
        _snapshots[graph_id] = _Snapshot()
    return _snapshots[graph_id]


def _get_lock(graph_id: int) -> asyncio.Lock:
    if graph_id not in _refresh_locks:
        _refresh_locks[graph_id] = asyncio.Lock()
    return _refresh_locks[graph_id]


def _get_initial_ready() -> asyncio.Event:
    global _initial_ready
    if _initial_ready is None:
        _initial_ready = asyncio.Event()
    return _initial_ready


async def _do_refresh(graph_id: int) -> None:
    _is_refreshing[graph_id] = True
    try:
        from backend.db import get_db, get_ppr_feed
        from backend.services.ppr_engine import update_ppr_scores

        def _get_content_type():
            conn = get_db()
            row = conn.execute(
                "SELECT content_type FROM graphs WHERE id=?", (graph_id,)
            ).fetchone()
            conn.close()
            return row["content_type"] if row else "mixed"

        loop = asyncio.get_running_loop()
        content_type = await loop.run_in_executor(None, _get_content_type)

        def _compute():
            from backend.db import get_pipeline_config
            update_ppr_scores(graph_id=graph_id, content_type=content_type,
                              compute_spam_mass=True)
            # Keep optional blend scorers fresh: get_ppr_feed blends cosine/
            # serendipity when enabled, and they're only recomputed here — so
            # without this the feed would blend stale scores. Serendipity reads
            # cosine_scores, so recompute cosine whenever either is on.
            cfg = get_pipeline_config(graph_id=graph_id)
            cosine_on = bool(cfg.get("scorer.cosine.enabled", 0.0))
            seren_on = bool(cfg.get("scorer.serendipity.enabled", 0.0))
            if cosine_on or seren_on:
                from backend.services.cosine_engine import update_cosine_scores
                update_cosine_scores(graph_id=graph_id, content_type=content_type)
            if seren_on:
                from backend.services.serendipity_engine import update_serendipity_scores
                update_serendipity_scores(graph_id=graph_id)
            # Embedding scorer: re-score from existing vectors (pure math, no
            # ollama). Generating embeddings is a separate explicit step
            # (/pipeline/embedding/embed) so the hot refresh path stays cheap.
            if bool(cfg.get("scorer.embedding.enabled", 0.0)):
                from backend.services.embedding_engine import update_embedding_scores
                update_embedding_scores(graph_id=graph_id, content_type=content_type)
            return get_ppr_feed(
                limit=PRECOMPUTE_LIMIT, offset=0, sort="score",
                _skip_recompute=True, graph_id=graph_id,
                max_spam_mass=0.92,
            )

        items = await loop.run_in_executor(None, _compute)
        prev = _snapshots.get(graph_id)
        if not items and prev and prev.items:
            # A transient recompute can yield 0 items (e.g. a refresh racing the
            # graph-1 feed_recommendations prune, or a momentary seed gap). Don't
            # let that blank the feed for a whole REFRESH_INTERVAL — keep the last
            # good snapshot and try again on the next cycle.
            logger.warning("feed_cache: graph %d refresh produced 0 items; keeping previous %d (gen %d)",
                           graph_id, len(prev.items), get_generation(graph_id))
        else:
            _snapshots[graph_id] = _Snapshot(items=items, computed_at=time.monotonic())
            bump_generation(graph_id)
            logger.info("feed_cache: graph %d refreshed %d items (gen %d)",
                        graph_id, len(items), get_generation(graph_id))
    except Exception as exc:
        logger.warning("feed_cache: graph %d refresh failed: %s", graph_id, exc)
    finally:
        _is_refreshing[graph_id] = False
        _get_initial_ready().set()


async def warm() -> None:
    """Warm all active graphs at startup (foreground — awaited before serving)."""
    from backend.db import get_db
    from backend.services.user_data_sync import sync_user_data_cache

    await sync_user_data_cache()

    def _get_graph_ids():
        conn = get_db()
        rows = conn.execute("SELECT id FROM graphs ORDER BY id").fetchall()
        conn.close()
        return [r["id"] for r in rows]

    loop = asyncio.get_running_loop()
    graph_ids = await loop.run_in_executor(None, _get_graph_ids)
    # Warm graphs concurrently
    await asyncio.gather(*[_do_refresh(gid) for gid in graph_ids], return_exceptions=True)


async def ensure_fresh(graph_id: int = 1) -> None:
    """Return immediately. Fire a background refresh if the cache is stale."""
    if _is_refreshing.get(graph_id):
        return
    snap = _get_snapshot(graph_id)
    age = time.monotonic() - snap.computed_at
    if age < REFRESH_INTERVAL:
        return
    asyncio.ensure_future(_do_refresh(graph_id))


async def wait_for_initial() -> None:
    """Block until at least one graph has completed at least one computation."""
    await asyncio.wait_for(_get_initial_ready().wait(), timeout=30.0)


def get_page(offset: int = 0, limit: int = 100, graph_id: int = 1) -> tuple[list[dict], int]:
    """Return (items_slice, total) from the current snapshot. Always instant."""
    items = _get_snapshot(graph_id).items
    return items[offset: offset + limit], len(items)
