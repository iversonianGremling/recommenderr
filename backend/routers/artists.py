"""Artists poll endpoint — accepts an artist list from ytmusic and returns release events."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import require_service_token
from backend.db import (
    save_artist_follow,
    list_artist_release_events,
)

router = APIRouter(dependencies=[Depends(require_service_token)])


class ArtistEntry(BaseModel):
    artist_name: str
    image: str | None = None
    source: str | None = None
    spotify_artist_id: str | None = None
    deezer_artist_id: str | None = None
    itunes_artist_id: str | None = None


class ArtistPollRequest(BaseModel):
    artists: list[ArtistEntry]
    limit: int = 50


@router.post("/poll")
async def poll_artist_releases(req: ArtistPollRequest) -> dict:
    """Sync ytmusic's followed-artist list into recommenderr and return known release events.

    Upserts each artist so the background worker will check them; returns all
    events already recorded for the supplied artists.
    """
    for a in req.artists:
        name = (a.artist_name or "").strip()
        if not name:
            continue
        save_artist_follow(
            name,
            image=a.image,
            source=a.source,
            spotify_artist_id=a.spotify_artist_id,
            deezer_artist_id=a.deezer_artist_id,
            itunes_artist_id=a.itunes_artist_id,
        )

    releases = list_artist_release_events(req.limit)
    return {"releases": releases}
