"""Radio endpoint — seed (track / artist / video_id) → ranked in-library stream.

Backed by `radio_library.build_radio`, which ranks the user's own music library
by similarity to the seeds (no external YouTube resolution), so radio works even
while egress is rate-limited/blocked. Response stays on the RadioTrack contract
the ytmusic frontend already consumes (`useRadioAutoExtend`, Start-radio menus).
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from backend.services.radio_library import build_radio

router = APIRouter()


class RadioSeed(BaseModel):
    video_id: str | None = None
    track: str | None = None
    artist: str | None = None


class RadioRequest(BaseModel):
    seeds: list[RadioSeed]
    limit: int = 30
    hops: int = 1  # accepted for backwards-compat; in-library radio ignores it
    exclude_video_ids: list[str] = []


@router.post("")
async def radio(req: RadioRequest) -> dict:
    seeds = [
        {"video_id": s.video_id, "track": s.track, "artist": s.artist}
        for s in req.seeds
        if (s.video_id or s.track or s.artist)
    ]
    if not seeds:
        return {"tracks": [], "total": 0, "seeds": 0}

    tracks = await run_in_threadpool(
        build_radio,
        seeds,
        limit=max(1, min(req.limit, 100)),
        exclude_video_ids=set(req.exclude_video_ids),
    )
    return {"tracks": tracks, "total": len(tracks), "seeds": len(seeds)}


@router.get("/search")
async def radio_track_search(q: str) -> list[dict]:
    """Quick track search via Last.fm for the radio seed picker."""
    from backend.services.music_client import lastfm_search_track
    try:
        results = await lastfm_search_track(q, limit=12)
        return [{"track": r.get("track", ""), "artist": r.get("artist", "")} for r in results]
    except Exception:
        return []
