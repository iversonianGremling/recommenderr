"""Sources router: expose + manage the source registry over HTTP."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services import source_registry

router = APIRouter()


class SourcePatch(BaseModel):
    enabled: bool | None = None
    weight: float | None = None
    rate_limit_per_min: int | None = None
    credentials: dict[str, str] | None = None  # write-only; values stored, never returned


@router.get("")
@router.get("/")
def list_sources() -> list[dict]:
    return source_registry.list_sources()


@router.get("/{name}")
def get_source(name: str) -> dict:
    sources = source_registry.list_sources()
    for s in sources:
        if s["name"] == name:
            return s
    raise HTTPException(status_code=404, detail=f"Source '{name}' not found")


@router.patch("/{name}")
def patch_source(name: str, body: SourcePatch) -> dict:
    from backend.db import get_db
    import time

    conn = get_db()
    row = conn.execute("SELECT name FROM sources WHERE name = ?", (name,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Source '{name}' not found")

    updates: dict[str, Any] = {}
    if body.enabled is not None:
        updates["enabled"] = 1 if body.enabled else 0
    if body.weight is not None:
        if not (0.0 <= body.weight <= 10.0):
            raise HTTPException(status_code=422, detail="weight must be between 0 and 10")
        updates["weight"] = body.weight
    if body.rate_limit_per_min is not None:
        updates["rate_limit_per_min"] = body.rate_limit_per_min
    if body.credentials:
        conn2 = get_db()
        existing_json = conn2.execute(
            "SELECT credentials_json FROM sources WHERE name = ?", (name,)
        ).fetchone()
        conn2.close()
        try:
            existing: dict = json.loads((existing_json or {}).get("credentials_json") or "{}")
        except Exception:
            existing = {}
        existing.update(body.credentials)
        updates["credentials_json"] = json.dumps(existing)

    if updates:
        source_registry._set_fields(name, **updates)

    return get_source(name)


@router.post("/{name}/reset-circuit")
def reset_circuit(name: str) -> dict:
    from backend.db import get_db
    conn = get_db()
    row = conn.execute("SELECT name FROM sources WHERE name = ?", (name,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Source '{name}' not found")
    source_registry.reset_circuit(name)
    return get_source(name)


@router.post("/{name}/probe")
async def probe_source(name: str) -> dict:
    """Fire a lightweight probe against the source and return the outcome."""
    from backend.db import get_db
    conn = get_db()
    row = conn.execute("SELECT name FROM sources WHERE name = ?", (name,)).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Source '{name}' not found")

    result: dict[str, Any] = {"source": name, "ok": False, "detail": ""}
    try:
        if name == "lastfm":
            from backend.services.music_client import lastfm_search_track
            hits = await lastfm_search_track("test", limit=1)
            result["ok"] = isinstance(hits, list)
        elif name == "spotify":
            from backend.services.music_client import spotify_search
            hits = await spotify_search("test", limit=1)
            result["ok"] = isinstance(hits, list)
        elif name == "deezer":
            from backend.services.music_client import deezer_search
            hits = await deezer_search("test", limit=1)
            result["ok"] = isinstance(hits, list)
        elif name == "itunes":
            from backend.services.music_client import itunes_search
            hits = await itunes_search("test", limit=1)
            result["ok"] = isinstance(hits, list)
        elif name == "musicbrainz":
            from backend.services.music_client import musicbrainz_search_recording
            hits = await musicbrainz_search_recording("test", limit=1)
            result["ok"] = isinstance(hits, list)
        elif name == "discogs":
            from backend.services.music_client import discogs_search
            hits = await discogs_search("test", limit=1)
            result["ok"] = isinstance(hits, list)
        elif name == "bandcamp":
            from backend.services.music_client import bandcamp_search
            hits = await bandcamp_search("test", limit=1)
            result["ok"] = isinstance(hits, list)
        elif name == "invidious":
            from backend.services.invidious_client import api_get
            r = await api_get("/stats")
            result["ok"] = bool(r)
        else:
            result["detail"] = f"No probe defined for '{name}'"
            return result
        if result["ok"]:
            source_registry.mark_success(name)
        else:
            source_registry.mark_failure(name, "probe returned empty")
    except Exception as exc:
        source_registry.mark_failure(name, str(exc))
        result["detail"] = str(exc)

    return result
