"""Crawl router — exposes /v1/crawl/* endpoints expected by ytvideo."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class EnqueueRequest(BaseModel):
    video_id: str
    title: str = ""


@router.post("/enqueue")
async def enqueue_crawl(req: EnqueueRequest) -> dict:
    """Add a video to the crawl queue."""
    from backend.db import get_db
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO crawl_queue (video_id, title, status, added_at) VALUES (?, ?, 'pending', ?)",
            (req.video_id, req.title or req.video_id, time.time()),
        )
        conn.commit()
        conn.close()
        return {"ok": True, "video_id": req.video_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
