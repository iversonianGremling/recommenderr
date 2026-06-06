"""recommenderr — aggregator + recommendation engine + admin UI."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "/opt/recommenderr/data/recommenderr.db")
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9001"))
DISABLE_WORKERS = os.environ.get("DISABLE_WORKERS", "0") == "1"
SCHEMA_VERSION = 11
ADMIN_ASSETS_DIR = Path(__file__).parent.parent / "admin-ui" / "dist" / "assets"


def init_db() -> None:
    # Read DB_PATH fresh so monkeypatched env vars in tests work correctly.
    db_path = os.environ.get("DB_PATH", "/opt/recommenderr/data/recommenderr.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None (autocommit) is required so that executescript()
    # doesn't block trying to commit a phantom transaction.
    con = sqlite3.connect(db_path, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        # Skip expensive schema application if the base schema is already present
        # (e.g. test fixtures pre-apply it, or the DB persists across restarts).
        schema_exists = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        ).fetchone()
        if not schema_exists:
            con.executescript(SCHEMA_PATH.read_text())
        # Always mark base schema version 1 as applied (idempotent).
        # migrate_to_items_v1 is responsible for bumping to version 2.
        con.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (1, ?)",
            (time.time(),),
        )
        con.commit()
        from backend.services.migration import migrate_to_items_v1, migrate_to_graphs_v3, migrate_to_graphs_v4, migrate_to_v5, migrate_to_v6, migrate_to_v7, migrate_to_v8, migrate_to_v9, migrate_to_v10, migrate_to_v11, migrate_to_v12, migrate_to_v13
        migrate_to_items_v1(con)
        migrate_to_graphs_v3(con)
        migrate_to_graphs_v4(con)
        migrate_to_v5(con)
        from backend.services.source_registry import seed_sources_table
        seed_sources_table()
        migrate_to_v6(con)
        migrate_to_v7(con)
        migrate_to_v8(con)
        migrate_to_v9(con)
        migrate_to_v10(con)
        migrate_to_v11(con)
        migrate_to_v12(con)
        migrate_to_v13(con)
    finally:
        con.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    tasks = []
    if not DISABLE_WORKERS:
        from backend.services.crawler import crawl_worker
        from backend.services.music_worker import music_worker
        from backend.services.artist_release_worker import artist_release_worker
        from backend.services.category_recs import category_recs_worker

        from backend.services.persona_worker import persona_worker
        tasks = [
            asyncio.create_task(crawl_worker()),
            asyncio.create_task(music_worker()),
            asyncio.create_task(artist_release_worker()),
            asyncio.create_task(category_recs_worker(0)),
            asyncio.create_task(category_recs_worker(1)),
            asyncio.create_task(category_recs_worker(2)),
            asyncio.create_task(category_recs_worker(3)),
            asyncio.create_task(persona_worker()),
        ]
    # Warm the feed cache in the background so first requests are instant.
    # Skipped when DISABLE_WORKERS=1 (tests) to avoid network calls to ytvideo.
    if not DISABLE_WORKERS:
        from backend.services import feed_cache as _feed_cache
        asyncio.ensure_future(_feed_cache.warm())
        # Live exit/method health bus ("everything communicating constantly").
        from backend.services import fetch_health
        tasks.append(asyncio.create_task(fetch_health.heartbeat()))
    yield
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="recommenderr", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from backend.routers import invidious, video, music, comments, radio, admin, artists, auth, ppr, crawl, items, sources, personas, modules, graphs, pipeline_export, pipeline_consumers, signal_sources, graph_sources, feed_named, fetch, library_recs  # noqa: E402

# Versioned API paths (internal / new clients)
app.include_router(invidious.router, prefix="/v1/invidious", tags=["invidious"])
app.include_router(video.router,     prefix="/v1/video",     tags=["video"])
app.include_router(music.router,     prefix="/v1/music",     tags=["music"])
app.include_router(library_recs.router, prefix="/v1/music",  tags=["library-recs"])
app.include_router(comments.router,  prefix="/v1/comments",  tags=["comments"])
app.include_router(radio.router,     prefix="/v1/radio",     tags=["radio"])
app.include_router(artists.router,   prefix="/v1/artists",   tags=["artists"])
app.include_router(ppr.router,       prefix="/v1/ppr",       tags=["ppr"])
app.include_router(crawl.router,     prefix="/v1/crawl",     tags=["crawl"])
app.include_router(feed_named.router, prefix="/v1/feed",      tags=["feed"])
app.include_router(items.router,     prefix="/v1/items",     tags=["items"])
app.include_router(sources.router,   prefix="/v1/sources",   tags=["sources"])
app.include_router(personas.router,  prefix="/v1/personas",  tags=["personas"])
app.include_router(modules.router,    prefix="/v1/modules",    tags=["modules"])
app.include_router(graphs.router,          prefix="/v1/graphs",    tags=["graphs"])
app.include_router(graph_sources.router,   prefix="/v1/graphs/{graph_id}/sources", tags=["graphs"])
app.include_router(pipeline_export.router, prefix="/v1/pipeline",  tags=["pipeline"])
app.include_router(pipeline_consumers.router, prefix="/v1/pipeline", tags=["pipeline"])
app.include_router(signal_sources.router, prefix="/v1/signal-sources", tags=["signal-sources"])
app.include_router(fetch.router,          prefix="/fetch",        tags=["fetch"])
app.include_router(fetch.router,          prefix="/v1/fetch",     tags=["fetch"])
app.include_router(fetch.router,          prefix="/api/fetch",    tags=["fetch"])

# StaticFiles must be mounted BEFORE the admin router — the admin catch-all
# /{path:path} would otherwise intercept every /admin/assets/* request first.
if ADMIN_ASSETS_DIR.exists():
    app.mount("/admin/assets", StaticFiles(directory=ADMIN_ASSETS_DIR), name="admin-assets")

app.include_router(admin.router,     prefix="/admin",        tags=["admin"])

# Legacy /api/ paths — mirror the monolith surface so nginx can drop the monolith fallback.
# invidious.router at /api covers: /api/search, /api/trending, /api/channel/...,
#   /api/video/{id}/recommendations, /api/video/{id}/storyboards, /api/vi/..., etc.
# video.router at /api/video covers: /api/video/{id}/stream, /formats, /info, /mux, etc.
# comments.router at /api/video covers: /api/video/{id}/comments
# auth.router at /api/auth covers: /api/auth/login, /logout, /me
app.include_router(invidious.router, prefix="/api",          tags=["invidious"])
app.include_router(video.router,     prefix="/api/video",    tags=["video"])
app.include_router(comments.router,  prefix="/api/video",    tags=["comments"])
app.include_router(auth.router,      prefix="/api/auth",     tags=["auth"])
# ytmusic frontend calls /api/music/* (nginx routes non-9003 paths here) — must
# mirror the /v1/music surface so /api/music/search, /artist, /album, etc. resolve.
app.include_router(music.router,     prefix="/api/music",    tags=["music"])


@app.get("/health")
def health() -> dict:
    return {
        "service": "recommenderr",
        "status": "ok",
        "schema_version": SCHEMA_VERSION,
        "workers": "disabled" if DISABLE_WORKERS else "running",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)
