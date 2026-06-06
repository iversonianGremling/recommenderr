"""Named graphs CRUD + per-graph PPR recompute."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

router = APIRouter()


class GraphCreate(BaseModel):
    name: str
    content_type: str = "mixed"
    config_json: str | None = None


class GraphUpdate(BaseModel):
    name: str | None = None
    config_json: str | None = None


class GraphRecomputeRequest(BaseModel):
    min_seed_rating: int = 0
    compute_spam_mass: bool = True


@router.get("")
async def list_graphs() -> list[dict]:
    def _q():
        from backend.db import get_db
        conn = get_db()
        rows = conn.execute("""
            SELECT g.id, g.name, g.content_type, g.config_json, g.created_at,
                   COUNT(DISTINCT p.video_id) as ppr_count,
                   MAX(p.computed_at) as ppr_computed_at,
                   COUNT(DISTINCT c.video_id) as cosine_count
            FROM graphs g
            LEFT JOIN ppr_scores p ON p.graph_id = g.id
            LEFT JOIN cosine_scores c ON c.graph_id = g.id
            GROUP BY g.id
            ORDER BY g.id
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await run_in_threadpool(_q)


@router.post("")
async def create_graph(body: GraphCreate) -> dict:
    if body.content_type not in ("mixed", "music", "video", "album", "artist"):
        raise HTTPException(status_code=422, detail="content_type must be mixed, music, video, album, or artist")
    def _q():
        from backend.db import get_db
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO graphs (name, content_type, config_json, created_at) VALUES (?,?,?,?)",
                (body.name, body.content_type, body.config_json, time.time()),
            )
            conn.commit()
            gid = cur.lastrowid
            row = conn.execute("SELECT * FROM graphs WHERE id=?", (gid,)).fetchone()
            return dict(row)
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(status_code=409, detail=f"Graph '{body.name}' already exists")
            raise
        finally:
            conn.close()
    return await run_in_threadpool(_q)


@router.patch("/{graph_id}")
async def update_graph(graph_id: int, body: GraphUpdate) -> dict:
    def _q():
        from backend.db import get_db
        conn = get_db()
        row = conn.execute("SELECT * FROM graphs WHERE id=?", (graph_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Graph not found")
        updates = {}
        if body.name is not None:
            updates["name"] = body.name
        if body.config_json is not None:
            updates["config_json"] = body.config_json
        if updates:
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE graphs SET {sets} WHERE id=?", (*updates.values(), graph_id))
            conn.commit()
        row = conn.execute("SELECT * FROM graphs WHERE id=?", (graph_id,)).fetchone()
        conn.close()
        return dict(row)
    return await run_in_threadpool(_q)


@router.delete("/{graph_id}")
async def delete_graph(graph_id: int) -> dict:
    if graph_id in (1, 2, 3):
        raise HTTPException(status_code=400, detail="Cannot delete built-in graphs (id 1-3)")
    def _q():
        from backend.db import get_db
        conn = get_db()
        row = conn.execute("SELECT id FROM graphs WHERE id=?", (graph_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Graph not found")
        conn.execute("DELETE FROM graphs WHERE id=?", (graph_id,))
        conn.commit()
        conn.close()
        return {"ok": True, "deleted": graph_id}
    return await run_in_threadpool(_q)


@router.post("/{graph_id}/recompute")
async def recompute_graph(graph_id: int, req: GraphRecomputeRequest) -> dict:
    """Trigger PPR + cosine recompute for a specific graph."""
    from backend.db import get_db
    from backend.services.ppr_engine import update_ppr_scores
    from backend.services.cosine_engine import update_cosine_scores

    conn = get_db()
    row = conn.execute("SELECT name, content_type FROM graphs WHERE id=?", (graph_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Graph not found")

    content_type = row["content_type"]
    started = time.monotonic()
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: update_ppr_scores(
                graph_id=graph_id,
                content_type=content_type,
                min_seed_rating=req.min_seed_rating,
                compute_spam_mass=req.compute_spam_mass,
            ),
        )
        n_cosine = await loop.run_in_executor(
            None,
            lambda: update_cosine_scores(
                graph_id=graph_id,
                content_type=content_type,
            ),
        )
        return {
            "ok": True,
            "graph_id": graph_id,
            "content_type": content_type,
            "cosine_scored": n_cosine,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
