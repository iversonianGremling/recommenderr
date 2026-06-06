"""Custom scorer/filter module management."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

router = APIRouter()

_SCORER_TEMPLATE = '''\
def score(candidates):
    """
    Score candidates. Receives a list of dicts:
      video_id, title, author, duration (seconds),
      score (PPR), ppr_score, cosine_score
    Returns dict {video_id: float}.
    """
    result = {}
    for c in candidates:
        # Example: boost shorter videos
        duration = c.get('duration') or 0
        boost = 1.5 if duration < 600 else 1.0
        result[c['video_id']] = c.get('score', 0) * boost
    return result
'''

_FILTER_TEMPLATE = '''\
def filter_items(items):
    """
    Filter/reorder items. Receives a list of dicts with the same fields as score().
    Returns a filtered/reordered list.
    """
    # Example: remove very long videos (> 2 hours)
    return [i for i in items if (i.get('duration') or 0) < 7200]
'''


class ModuleCreate(BaseModel):
    name: str
    type: str  # 'scorer' | 'filter'
    code: str | None = None
    enabled: bool = True


class ModuleUpdate(BaseModel):
    name: str | None = None
    code: str | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
async def list_modules() -> list:
    def _q():
        from backend.db import get_db
        conn = get_db()
        rows = conn.execute(
            "SELECT id, name, type, enabled, created_at, updated_at FROM custom_modules ORDER BY type, name"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await run_in_threadpool(_q)


@router.post("")
async def create_module(body: ModuleCreate) -> dict:
    if body.type not in ("scorer", "filter"):
        raise HTTPException(400, "type must be 'scorer' or 'filter'")
    if not body.name.strip():
        raise HTTPException(400, "name required")

    template = _SCORER_TEMPLATE if body.type == "scorer" else _FILTER_TEMPLATE
    code = body.code if body.code is not None else template

    from backend.services.module_engine import validate_module
    errors = await run_in_threadpool(validate_module, code, body.type)
    if errors:
        raise HTTPException(400, detail={"errors": errors})

    def _insert():
        from backend.db import get_db
        now = time.time()
        conn = get_db()
        try:
            cur = conn.execute(
                "INSERT INTO custom_modules (name, type, code, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (body.name.strip(), body.type, code, int(body.enabled), now, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM custom_modules WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)
        except Exception as e:
            raise HTTPException(409, f"Name conflict: {e}")
        finally:
            conn.close()

    return await run_in_threadpool(_insert)


@router.get("/{module_id}")
async def get_module(module_id: int) -> dict:
    def _q():
        from backend.db import get_db
        conn = get_db()
        row = conn.execute("SELECT * FROM custom_modules WHERE id = ?", (module_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(404, "Module not found")
        return dict(row)
    return await run_in_threadpool(_q)


@router.put("/{module_id}")
async def update_module(module_id: int, body: ModuleUpdate) -> dict:
    def _q():
        from backend.db import get_db
        conn = get_db()
        existing = conn.execute("SELECT * FROM custom_modules WHERE id = ?", (module_id,)).fetchone()
        conn.close()
        if not existing:
            raise HTTPException(404, "Module not found")
        return dict(existing)

    existing = await run_in_threadpool(_q)

    code = body.code if body.code is not None else existing["code"]
    if body.code is not None:
        from backend.services.module_engine import validate_module
        errors = await run_in_threadpool(validate_module, code, existing["type"])
        if errors:
            raise HTTPException(400, detail={"errors": errors})

    def _update():
        from backend.db import get_db
        now = time.time()
        conn = get_db()
        sets = []
        vals = []
        if body.name is not None:
            sets.append("name = ?"); vals.append(body.name.strip())
        if body.code is not None:
            sets.append("code = ?"); vals.append(code)
        if body.enabled is not None:
            sets.append("enabled = ?"); vals.append(int(body.enabled))
        sets.append("updated_at = ?"); vals.append(now)
        vals.append(module_id)
        conn.execute(f"UPDATE custom_modules SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
        row = conn.execute("SELECT * FROM custom_modules WHERE id = ?", (module_id,)).fetchone()
        conn.close()
        return dict(row)

    return await run_in_threadpool(_update)


@router.delete("/{module_id}")
async def delete_module(module_id: int) -> dict:
    def _q():
        from backend.db import get_db
        conn = get_db()
        conn.execute("DELETE FROM custom_modules WHERE id = ?", (module_id,))
        conn.commit()
        conn.close()
        return {"ok": True}
    return await run_in_threadpool(_q)


# ---------------------------------------------------------------------------
# Test run
# ---------------------------------------------------------------------------

class TestRequest(BaseModel):
    limit: int = 20


@router.post("/{module_id}/test")
async def test_module(module_id: int, req: TestRequest) -> dict:
    """Run the module against the current top PPR candidates and return results."""
    import time as _time

    def _q():
        from backend.db import get_db
        from backend.services.module_engine import run_scorer, run_filter

        conn = get_db()
        row = conn.execute("SELECT * FROM custom_modules WHERE id = ?", (module_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Module not found")

        lim = max(5, min(req.limit, 100))
        candidates_raw = conn.execute("""
            SELECT fr.video_id, fr.title, fr.author, fr.duration,
                   p.score as ppr_score,
                   cs.score as cosine_score
            FROM feed_recommendations fr
            LEFT JOIN ppr_scores p ON p.video_id = fr.video_id AND p.graph_id = 1
            LEFT JOIN cosine_scores cs ON cs.video_id = fr.video_id AND cs.graph_id = 1
            LEFT JOIN watch_history wh ON wh.video_id = fr.video_id
            WHERE wh.video_id IS NULL AND fr.title IS NOT NULL
            GROUP BY fr.video_id
            ORDER BY COALESCE(p.score, 0) DESC
            LIMIT ?
        """, (lim,)).fetchall()
        conn.close()

        candidates = [
            {
                "video_id": r["video_id"],
                "title": r["title"],
                "author": r["author"],
                "duration": r["duration"],
                "score": r["ppr_score"] or 0.0,
                "ppr_score": r["ppr_score"] or 0.0,
                "cosine_score": r["cosine_score"] or 0.0,
            }
            for r in candidates_raw
        ]

        t0 = _time.perf_counter()
        try:
            if row["type"] == "scorer":
                scores = run_scorer(row["code"], candidates)
                elapsed = round(_time.perf_counter() - t0, 4)
                results = sorted(
                    [
                        {**c, "module_score": scores.get(c["video_id"], 0.0)}
                        for c in candidates
                    ],
                    key=lambda x: x["module_score"],
                    reverse=True,
                )
            else:
                filtered = run_filter(row["code"], candidates)
                elapsed = round(_time.perf_counter() - t0, 4)
                results = filtered
            return {"ok": True, "elapsed_seconds": elapsed, "results": results}
        except Exception as e:
            return {"ok": False, "error": str(e), "results": []}

    return await run_in_threadpool(_q)


# ---------------------------------------------------------------------------
# Recompute (scorer only)
# ---------------------------------------------------------------------------

@router.post("/{module_id}/recompute")
async def recompute_module(module_id: int) -> dict:
    import time as _t

    def _q():
        from backend.services.module_engine import update_module_scores
        t0 = _t.time()
        n = update_module_scores(module_id)
        return {"ok": True, "scored": n, "elapsed_seconds": round(_t.time() - t0, 2)}

    try:
        return await run_in_threadpool(_q)
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Scores (scorer only)
# ---------------------------------------------------------------------------

@router.get("/{module_id}/scores")
async def get_module_scores(module_id: int, limit: int = 100) -> list:
    def _q():
        from backend.db import get_db
        conn = get_db()
        rows = conn.execute("""
            SELECT cms.video_id, cms.score, cms.computed_at,
                   COALESCE(fr.title, wh.title) as title,
                   fr.author,
                   fr.thumbnail, fr.duration
            FROM custom_module_scores cms
            LEFT JOIN feed_recommendations fr ON fr.video_id = cms.video_id
            LEFT JOIN watch_history wh ON wh.video_id = cms.video_id
            WHERE cms.module_id = ?
            GROUP BY cms.video_id
            ORDER BY cms.score DESC
            LIMIT ?
        """, (module_id, max(1, min(limit, 500)))).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    return await run_in_threadpool(_q)
