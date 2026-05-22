"""Personas router — /v1/personas/*"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db():
    from backend.db import get_db
    return get_db()


def _bump_version(conn, persona_id: int) -> None:
    conn.execute(
        "UPDATE personas SET version = version + 1, updated_at = ? WHERE id = ?",
        (time.time(), persona_id),
    )
    conn.execute(
        "UPDATE persona_jobs SET status='pending', next_run_at=0 WHERE persona_id=?",
        (persona_id,),
    )


def _resolve_item(conn, scheme: str, external_id: str) -> int:
    """Resolve (scheme, external_id) → item.id. 404 if not found."""
    row = conn.execute(
        "SELECT id FROM items WHERE scheme=? AND external_id=?",
        (scheme, external_id),
    ).fetchone()
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Item not found: scheme={scheme!r} external_id={external_id!r}",
        )
    return row["id"]


# ---------------------------------------------------------------------------
# CRUD — personas
# ---------------------------------------------------------------------------

class PersonaCreate(BaseModel):
    name: str
    description: str | None = None
    scheme: str = "yt_video"
    alpha: float = 0.15
    min_seed_rating: int = 0


class PersonaPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    alpha: float | None = None
    min_seed_rating: int | None = None


@router.get("")
@router.get("/")
async def list_personas() -> list:
    def _q():
        conn = _get_db()
        rows = conn.execute("""
            SELECT p.id, p.name, p.description, p.scheme, p.alpha, p.min_seed_rating,
                   p.created_at, p.updated_at, p.version,
                   COUNT(ps.item_id) as seed_count,
                   pj.status as job_status, pj.last_run_at, pj.last_error
            FROM personas p
            LEFT JOIN persona_seeds ps ON ps.persona_id = p.id
            LEFT JOIN persona_jobs pj ON pj.persona_id = p.id
            GROUP BY p.id
            ORDER BY p.name
        """).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await run_in_threadpool(_q)


@router.post("", status_code=201)
async def create_persona(body: PersonaCreate) -> dict:
    if not body.name.strip():
        raise HTTPException(400, "name required")
    if not (0.01 <= body.alpha <= 0.99):
        raise HTTPException(400, "alpha must be between 0.01 and 0.99")

    def _q():
        conn = _get_db()
        now = time.time()
        try:
            cur = conn.execute(
                "INSERT INTO personas (name, description, scheme, alpha, min_seed_rating, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (body.name.strip(), body.description, body.scheme, body.alpha,
                 body.min_seed_rating, now, now),
            )
            pid = cur.lastrowid
            conn.execute(
                "INSERT INTO persona_jobs (persona_id, status, next_run_at) VALUES (?, 'pending', ?)",
                (pid, now),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"Persona name {body.name!r} already exists")
            raise
        row = conn.execute("SELECT * FROM personas WHERE id=?", (pid,)).fetchone()
        conn.close()
        return dict(row)
    return await run_in_threadpool(_q)


@router.get("/{persona_id}")
async def get_persona(persona_id: int) -> dict:
    def _q():
        conn = _get_db()
        row = conn.execute("""
            SELECT p.*, COUNT(ps.item_id) as seed_count,
                   pj.status as job_status, pj.last_run_at, pj.last_error, pj.next_run_at as job_next_run
            FROM personas p
            LEFT JOIN persona_seeds ps ON ps.persona_id = p.id
            LEFT JOIN persona_jobs pj ON pj.persona_id = p.id
            WHERE p.id = ?
            GROUP BY p.id
        """, (persona_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(404, "Persona not found")
        return dict(row)
    return await run_in_threadpool(_q)


@router.patch("/{persona_id}")
async def patch_persona(persona_id: int, body: PersonaPatch) -> dict:
    if body.alpha is not None and not (0.01 <= body.alpha <= 0.99):
        raise HTTPException(400, "alpha must be between 0.01 and 0.99")

    def _q():
        conn = _get_db()
        updates = {}
        if body.name is not None:
            updates["name"] = body.name.strip()
        if body.description is not None:
            updates["description"] = body.description
        if body.alpha is not None:
            updates["alpha"] = body.alpha
        if body.min_seed_rating is not None:
            updates["min_seed_rating"] = body.min_seed_rating
        if not updates:
            row = conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
            conn.close()
            if not row:
                raise HTTPException(404, "Persona not found")
            return dict(row)
        set_clause = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [time.time(), persona_id]
        conn.execute(f"UPDATE personas SET {set_clause}, updated_at=? WHERE id=?", vals)
        _bump_version(conn, persona_id)
        conn.commit()
        row = conn.execute("SELECT * FROM personas WHERE id=?", (persona_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(404, "Persona not found")
        return dict(row)
    return await run_in_threadpool(_q)


@router.delete("/{persona_id}", status_code=204)
async def delete_persona(persona_id: int):
    def _q():
        conn = _get_db()
        conn.execute("DELETE FROM personas WHERE id=?", (persona_id,))
        conn.commit()
        conn.close()
    await run_in_threadpool(_q)


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------

class SeedItem(BaseModel):
    scheme: str
    external_id: str
    weight: float = 1.0


class SeedBatch(BaseModel):
    seeds: list[SeedItem]
    merge: bool = False


@router.get("/{persona_id}/seeds")
async def list_seeds(persona_id: int) -> list:
    def _q():
        conn = _get_db()
        rows = conn.execute("""
            SELECT ps.item_id, ps.weight, i.scheme, i.external_id, i.metadata_json
            FROM persona_seeds ps
            JOIN items i ON i.id = ps.item_id
            WHERE ps.persona_id = ?
            ORDER BY ps.weight DESC
        """, (persona_id,)).fetchall()
        conn.close()
        import json
        result = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r["metadata_json"] or "{}")
            except Exception:
                pass
            result.append({
                "item_id": r["item_id"],
                "scheme": r["scheme"],
                "external_id": r["external_id"],
                "weight": r["weight"],
                "title": meta.get("title") or meta.get("track") or r["external_id"],
                "author": meta.get("author") or meta.get("artist"),
            })
        return result
    return await run_in_threadpool(_q)


@router.post("/{persona_id}/seeds")
async def set_seeds(persona_id: int, body: SeedBatch) -> dict:
    def _q():
        conn = _get_db()
        if not conn.execute("SELECT 1 FROM personas WHERE id=?", (persona_id,)).fetchone():
            conn.close()
            raise HTTPException(404, "Persona not found")
        if not body.merge:
            conn.execute("DELETE FROM persona_seeds WHERE persona_id=?", (persona_id,))
        for seed in body.seeds:
            item_id = _resolve_item(conn, seed.scheme, seed.external_id)
            conn.execute(
                "INSERT OR REPLACE INTO persona_seeds (persona_id, item_id, weight) VALUES (?,?,?)",
                (persona_id, item_id, seed.weight),
            )
        _bump_version(conn, persona_id)
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) as c FROM persona_seeds WHERE persona_id=?", (persona_id,)
        ).fetchone()["c"]
        conn.close()
        return {"ok": True, "seed_count": count}
    return await run_in_threadpool(_q)


@router.delete("/{persona_id}/seeds/{item_id}", status_code=204)
async def delete_seed(persona_id: int, item_id: int):
    def _q():
        conn = _get_db()
        conn.execute(
            "DELETE FROM persona_seeds WHERE persona_id=? AND item_id=?",
            (persona_id, item_id),
        )
        _bump_version(conn, persona_id)
        conn.commit()
        conn.close()
    await run_in_threadpool(_q)


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------

@router.get("/{persona_id}/scores")
async def get_persona_scores(persona_id: int, limit: int = 100) -> list:
    def _q():
        conn = _get_db()
        rows = conn.execute(
            """
            SELECT ps.video_id, ps.score, ps.spam_mass, ps.computed_at,
                   COALESCE(fr.title, wh.title, pv.title) as title,
                   COALESCE(fr.author, wh.author, pv.author) as author,
                   COALESCE(fr.thumbnail, wh.thumbnail, pv.thumbnail) as thumbnail,
                   COALESCE(fr.duration, wh.duration, pv.duration) as duration
            FROM persona_scores ps
            LEFT JOIN feed_recommendations fr ON fr.video_id = ps.video_id
            LEFT JOIN watch_history wh ON wh.video_id = ps.video_id
            LEFT JOIN playlist_videos pv ON pv.video_id = ps.video_id
            WHERE ps.persona_id = ?
            GROUP BY ps.video_id
            ORDER BY ps.score DESC
            LIMIT ?
            """,
            (persona_id, max(1, min(limit, 500))),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await run_in_threadpool(_q)


# ---------------------------------------------------------------------------
# Recompute (synchronous, for UI "Run now" button)
# ---------------------------------------------------------------------------

@router.post("/{persona_id}/recompute")
async def recompute_persona(persona_id: int) -> dict:
    import asyncio
    from backend.services.persona_engine import compute_persona_ppr
    import time as _time

    t0 = _time.monotonic()
    try:
        loop = asyncio.get_running_loop()
        count = await loop.run_in_executor(None, compute_persona_ppr, persona_id)
        return {"ok": True, "scored": count, "elapsed_seconds": round(_time.monotonic() - t0, 2)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
