"""Radio endpoint — seed tracks → ranked music-confirmed YouTube stream."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from backend.services.radio_service import generate_radio

router = APIRouter()


class RadioSeed(BaseModel):
    video_id: str | None = None
    track: str | None = None
    artist: str | None = None


class RadioRequest(BaseModel):
    seeds: list[RadioSeed]
    limit: int = 30
    hops: int = 1
    exclude_video_ids: list[str] = []


@router.post("")
async def radio(req: RadioRequest) -> dict:
    seed_pairs: list[tuple[str, str]] = []
    for s in req.seeds:
        t = (s.track or "").strip()
        a = (s.artist or "").strip()
        if t or a:
            seed_pairs.append((t, a))

    if not seed_pairs:
        return {"tracks": [], "total": 0, "seeds": 0}

    tracks = await generate_radio(
        seed_pairs,
        limit=max(1, min(req.limit, 100)),
        hops=max(1, min(req.hops, 2)),
        exclude_video_ids=set(req.exclude_video_ids),
    )
    return {"tracks": tracks, "total": len(tracks), "seeds": len(seed_pairs)}


@router.get("/search")
async def radio_track_search(q: str) -> list[dict]:
    """Quick track search via Last.fm for the radio seed picker."""
    from backend.services.music_client import lastfm_search_track
    try:
        results = await lastfm_search_track(q, limit=12)
        return [{"track": r.get("track", ""), "artist": r.get("artist", "")} for r in results]
    except Exception:
        return []
