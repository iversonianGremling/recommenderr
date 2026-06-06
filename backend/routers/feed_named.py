"""Named per-graph feed endpoints: POST /v1/feed/{slug}

Callers use a human-readable graph name instead of a numeric graph_id:
  POST /v1/feed/videos  {"seeds": [...], "limit": 50}
  POST /v1/feed/songs   {"seeds": [...], "limit": 50}
  POST /v1/feed/albums
  POST /v1/feed/artists
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

router = APIRouter()


class FeedRequest(BaseModel):
    seeds: list[str] = []
    limit: int = 50
    offset: int = 0


@router.get("/generations")
async def feed_generations() -> dict:
    """Cheap poll target for downstream consumers: current feed generation per
    graph (id and name). A bumped generation means the feed changed (a weight/
    rule/filter/config edit or a fresh recompute) and cached feeds should be
    dropped and re-warmed. Always instant — no recompute is triggered here."""
    from backend.db import get_db
    from backend.services import feed_cache

    def _names():
        conn = get_db()
        rows = conn.execute("SELECT id, name FROM graphs").fetchall()
        conn.close()
        return {r["id"]: r["name"] for r in rows}

    names = await run_in_threadpool(_names)
    gens = feed_cache.get_all_generations()
    return {
        "generations": {str(gid): gens.get(gid, 0) for gid in names},
        "by_name": {names[gid].lower(): gens.get(gid, 0) for gid in names},
    }


@router.post("/{slug}")
async def named_feed(slug: str, req: FeedRequest) -> dict:
    from backend.db import get_db
    from backend.services import feed_cache

    def _resolve():
        conn = get_db()
        row = conn.execute(
            "SELECT id FROM graphs WHERE LOWER(name) = ?", (slug.lower(),)
        ).fetchone()
        conn.close()
        return row

    row = await run_in_threadpool(_resolve)
    if not row:
        raise HTTPException(status_code=404, detail=f"No graph named '{slug}'")

    graph_id = row["id"]
    await feed_cache.ensure_fresh(graph_id)
    items, total = feed_cache.get_page(req.offset, req.limit, graph_id)
    return {"items": items, "total": total, "graph_id": graph_id, "graph_slug": slug,
            "generation": feed_cache.get_generation(graph_id)}
