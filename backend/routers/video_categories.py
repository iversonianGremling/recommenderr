"""Per-category video recommendation endpoints.

Category definitions/assignments are mirrored from ytvideo by
`services.user_data_sync._sync_categories_data`; the `category_recs` worker ranks
them via the PPR engine. This router only exposes the read/recompute surface that
the ytvideo backend proxies to (`/api/local/v2/categories/{id}/recommendations`).
CRUD stays authoritative in ytvideo.
"""
from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool

from backend.services import category_recs

router = APIRouter()


@router.get("/{cat_id}/recommendations")
async def cat_recommendations(cat_id: int, limit: int = Query(30)):
    items = await run_in_threadpool(category_recs.get_recommendations, cat_id, limit)
    job = await run_in_threadpool(category_recs.get_job_status, cat_id)
    # Only schedule a job if there's nothing queued/running and no results.
    if not items and (not job or job.get("status") not in ("pending", "running")):
        await run_in_threadpool(category_recs.mark_dirty, cat_id)
        job = await run_in_threadpool(category_recs.get_job_status, cat_id)
    status = "ready" if items else ((job or {}).get("status") or "computing")
    return {"status": status, "items": items, "last_run_at": (job or {}).get("last_run_at")}


@router.post("/{cat_id}/recompute")
async def cat_recompute(cat_id: int):
    await run_in_threadpool(category_recs.mark_dirty, cat_id)
    return {"ok": True}
