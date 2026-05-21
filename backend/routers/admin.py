"""Admin UI endpoints — serve the test UI and provide diagnostic endpoints."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

DB_PATH = os.environ.get("DB_PATH", "/opt/recommenderr/data/recommenderr.db")
ADMIN_UI_DIR = Path(__file__).parent.parent.parent / "admin-ui"

router = APIRouter()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_ui() -> HTMLResponse:
    index = ADMIN_UI_DIR / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text())
    return HTMLResponse("<h1>recommenderr admin</h1><p>admin-ui/index.html not found</p>")


@router.get("/status")
async def status() -> dict:
    """Return worker queue depths and table row counts."""
    if not Path(DB_PATH).exists():
        return {"error": "DB not found"}
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        tables = [
            "crawl_queue", "music_jobs", "recommendation_edges",
            "ppr_scores", "music_library", "artist_release_events",
            "category_rec_jobs",
        ]
        counts = {}
        for t in tables:
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                counts[t] = None
        # worker queue depths
        try:
            pending = con.execute(
                "SELECT COUNT(*) FROM crawl_queue WHERE status='pending'"
            ).fetchone()[0]
            counts["crawl_queue_pending"] = pending
        except Exception:
            pass
        con.close()
        return {"table_counts": counts}
    except Exception as exc:
        return {"error": str(exc)}
