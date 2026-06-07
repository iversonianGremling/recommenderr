"""Music category recommendation endpoints.

A music category is an individual ``music_tag`` (kind=``tag``) or a
``music_tag_group`` (kind=``group``). Recs are computed lazily by the
``music_category_recs`` worker (external discovery is egress-heavy, so a category
is only computed once requested). ytmusic proxies these.
"""
from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from backend.services import music_category_recs as mcr

router = APIRouter()


@router.get("")
@router.get("/")
async def list_music_categories():
    """Catalog of music categories: tag groups + tags (with track counts)."""
    return await run_in_threadpool(mcr.list_categories)


def _check_kind(kind: str):
    if kind not in mcr.VALID_KINDS:
        raise HTTPException(400, f"kind must be one of {mcr.VALID_KINDS}")


@router.get("/{kind}/{ref_id}/recommendations")
async def music_cat_recommendations(kind: str, ref_id: int, limit: int = Query(40)):
    _check_kind(kind)
    items = await run_in_threadpool(mcr.get_recommendations, kind, ref_id, limit)
    job = await run_in_threadpool(mcr.get_job_status, kind, ref_id)
    # No results and nothing queued/running → schedule a compute and report status.
    if not items and (not job or job.get("status") not in ("pending", "running")):
        await run_in_threadpool(mcr.mark_dirty, kind, ref_id)
        job = await run_in_threadpool(mcr.get_job_status, kind, ref_id)
    status = "ready" if items else ((job or {}).get("status") or "computing")
    return {"status": status, "items": items, "last_run_at": (job or {}).get("last_run_at")}


@router.post("/{kind}/{ref_id}/recompute")
async def music_cat_recompute(kind: str, ref_id: int):
    _check_kind(kind)
    await run_in_threadpool(mcr.mark_dirty, kind, ref_id)
    return {"ok": True}
