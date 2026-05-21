"""Admin UI — serve the React SPA and diagnostic endpoints."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

DB_PATH = os.environ.get("DB_PATH", "/opt/recommenderr/data/recommenderr.db")
ADMIN_UI_DIR = Path(__file__).parent.parent.parent / "admin-ui"
DIST_DIR = ADMIN_UI_DIR / "dist"
INDEX_HTML = DIST_DIR / "index.html"

router = APIRouter()


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
            "category_rec_jobs", "sources", "items", "schemes",
        ]
        counts = {}
        for t in tables:
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                counts[t] = None
        try:
            counts["crawl_queue_pending"] = con.execute(
                "SELECT COUNT(*) FROM crawl_queue WHERE status='pending'"
            ).fetchone()[0]
        except Exception:
            pass
        con.close()
        return {"table_counts": counts}
    except Exception as exc:
        return {"error": str(exc)}


# SPA catch-all: any path under /admin/ → serve dist/index.html.
# Assets (/admin/assets/*) are served by the StaticFiles mount in main.py
# and never reach this route.
@router.get("", response_model=None)
@router.get("/", response_model=None)
@router.get("/{path:path}", response_model=None)
async def spa(path: str = ""):
    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML, media_type="text/html")
    # Fallback when SPA hasn't been built yet
    return HTMLResponse(
        "<h1>recommenderr admin</h1>"
        "<p>Run <code>cd admin-ui && npm install && npm run build</code> to build the UI.</p>"
    )
