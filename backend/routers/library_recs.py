"""Library recommendations: ingest an external music taste profile (e.g. from
yamtrack) and recommend new songs/albums/artists from it via the catalog PPR.

Mounted at /v1/music alongside routers/music.py.
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel

from backend.auth import require_service_token
from backend.db import get_db
from backend.services import library_recommendations as librec

router = APIRouter()

_KINDS = ("song", "album", "artist")


class Seed(BaseModel):
    kind: str
    artist: str = ""
    album: str = ""
    track: str = ""
    score: float | None = None


class SeedPayload(BaseModel):
    source: str = "yamtrack"
    seeds: list[Seed]


@router.put("/library/seeds", dependencies=[Depends(require_service_token)])
def put_library_seeds(payload: SeedPayload, background_tasks: BackgroundTasks):
    """Replace all seeds for a source (transactional bulk upsert).

    Schedules a recommendations recompute in the background so each sync
    refreshes the recs (the recompute itself is guarded against overlap).
    """
    src = (payload.source or "yamtrack").strip() or "yamtrack"
    now = time.time()
    rows = [
        (src, s.kind, (s.artist or "").strip(), (s.album or "").strip(),
         (s.track or "").strip(), s.score, now)
        for s in payload.seeds
        if s.kind in _KINDS and ((s.artist or "").strip() or (s.album or "").strip() or (s.track or "").strip())
    ]

    conn = get_db()
    try:
        conn.execute("DELETE FROM external_music_seeds WHERE source = ?", (src,))
        conn.executemany(
            """INSERT OR REPLACE INTO external_music_seeds
               (source, kind, artist, album, track, score, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        counts = {
            k: conn.execute(
                "SELECT COUNT(*) FROM external_music_seeds WHERE source = ? AND kind = ?",
                (src, k),
            ).fetchone()[0]
            for k in _KINDS
        }
    finally:
        conn.close()
    background_tasks.add_task(librec.recompute)
    return {"ok": True, "source": src, "ingested": len(rows), "counts": counts}


@router.get("/library/seeds")
def get_library_seeds(
    source: str | None = None,
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=2000),
):
    """Inspect stored seeds (totals by kind + a score-ordered sample)."""
    conn = get_db()
    try:
        conds, args = [], []
        if source:
            conds.append("source = ?")
            args.append(source)
        if kind:
            conds.append("kind = ?")
            args.append(kind)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM external_music_seeds{where}", args
        ).fetchone()[0]
        by_kind = {
            r["kind"]: r["n"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM external_music_seeds GROUP BY kind"
            ).fetchall()
        }
        rows = [
            dict(r)
            for r in conn.execute(
                f"SELECT source, kind, artist, album, track, score, updated_at "
                f"FROM external_music_seeds{where} "
                f"ORDER BY score IS NULL, score DESC LIMIT ?",
                [*args, limit],
            ).fetchall()
        ]
    finally:
        conn.close()
    return {"total": total, "by_kind": by_kind, "seeds": rows}


@router.get("/recommendations/library")
def get_library_recommendations(limit: int = Query(50, ge=1, le=500)):
    """Return the persisted library recommendations grouped by kind."""
    return librec.read_results(limit)


@router.post("/recommendations/library/recompute")
async def recompute_library_recommendations(wait: bool = Query(False)):
    """Recompute library recommendations. Fire-and-forget unless ?wait=1."""
    if wait:
        return await librec.recompute()
    asyncio.create_task(librec.recompute())
    return {"status": "started"}


@router.post("/library/app-ratings/sync")
def sync_app_rating_seeds_endpoint(min_rating: int = Query(7, ge=1, le=10)):
    """Mirror the app's own album ratings (ytmusic.db — yamtrack, RYM, manual…)
    into external_music_seeds as source='app_ratings'. No egress; just refreshes
    the seed set so a subsequent recompute reflects the full rating history."""
    return librec.sync_app_rating_seeds(min_rating=min_rating)


@router.post("/library/favorites-tag/sync")
def sync_favorites_tag_endpoint(
    min_rating: int = Query(8, ge=1, le=10),
    include_artist: bool = Query(True),
):
    """Project highly-rated favorites (album_ratings) onto a 'Favorites' music
    tag by matching library tracks. Additive + idempotent; re-run after adding/
    downloading rated albums so their tracks join the tag automatically."""
    from backend.services.favorites_sync import sync_favorites_tag
    return sync_favorites_tag(min_rating=min_rating, include_artist=include_artist)


@router.get("/library/status")
def get_library_status():
    """Live state of the yamtrack → catalog-PPR → library-recs lane, for the
    pipeline canvas: seed counts by source/kind, result counts by kind, and the
    recompute engine state."""
    conn = get_db()
    try:
        seed_total = conn.execute("SELECT COUNT(*) FROM external_music_seeds").fetchone()[0]
        seed_by_source = {}
        for r in conn.execute(
            "SELECT source, COUNT(*) AS n FROM external_music_seeds GROUP BY source"
        ).fetchall():
            seed_by_source[r["source"]] = r["n"]
        seed_by_kind = {
            r["kind"]: r["n"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM external_music_seeds GROUP BY kind"
            ).fetchall()
        }
        last_seed_at = conn.execute(
            "SELECT MAX(updated_at) FROM external_music_seeds"
        ).fetchone()[0]
        result_by_kind = {
            r["kind"]: r["n"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM library_rec_results GROUP BY kind"
            ).fetchall()
        }
        result_computed_at = conn.execute(
            "SELECT MAX(computed_at) FROM library_rec_results"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "seeds": {
            "total": seed_total,
            "by_source": seed_by_source,
            "by_kind": seed_by_kind,
            "last_seed_at": last_seed_at,
        },
        "results": {
            "by_kind": result_by_kind,
            "total": sum(result_by_kind.values()),
            "computed_at": result_computed_at,
        },
        "engine": {
            "running": librec._state["running"],
            "last_computed": librec._state["last_computed"],
            "last_error": librec._state["last_error"],
        },
    }


@router.get("/library/config")
def get_library_config():
    """Catalog (library) PPR engine config — its own independent engine."""
    cfg = librec.get_catalog_config()
    return {**cfg, "_defaults": librec.CATALOG_CONFIG_DEFAULTS}


class CatalogConfigUpdate(BaseModel):
    alpha: float | None = None
    album_seed_cap: float | None = None
    song_seed_cap: float | None = None
    related_per_artist: float | None = None
    albums_per_artist: float | None = None
    song_recs_from_albums: float | None = None


@router.put("/library/config")
def put_library_config(body: CatalogConfigUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"ok": False, "error": "No fields to update"}
    librec.set_catalog_config(updates)
    return {"ok": True, "updated": list(updates.keys())}
