import asyncio
import logging
import os
import httpx
import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request, Response
from backend.services.invidious_client import api_get
from backend.services import ytdlp_service

router = APIRouter()
logger = logging.getLogger(__name__)

def _token(request):
    return request.cookies.get("inv_token") or request.headers.get("authorization", "").removeprefix("Bearer ").strip() or None

INVIDIOUS_URL = os.getenv("INVIDIOUS_URL", "http://192.168.1.173:3000")
TOPIC_CHANNEL_PAGE_SIZE = 60


_RELATIVE_URL_KEYS = {"url", "templateUrl", "template_url"}

def _fix_thumbs(obj):
    """Recursively prefix relative /vi/... URLs with the Invidious host."""
    if isinstance(obj, list):
        return [_fix_thumbs(i) for i in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _RELATIVE_URL_KEYS and isinstance(v, str) and v.startswith("/"):
                out[k] = INVIDIOUS_URL + v
            else:
                out[k] = _fix_thumbs(v)
        return out
    return obj


def _topic_page_bounds(page: int) -> tuple[int, int]:
    current_page = max(1, page)
    start = (current_page - 1) * TOPIC_CHANNEL_PAGE_SIZE + 1
    end = start + TOPIC_CHANNEL_PAGE_SIZE - 1
    return start, end


def _topic_display_name(title: str | None) -> str:
    if not title:
        return ""
    prefix = "Uploads from "
    return title[len(prefix):].strip() if title.startswith(prefix) else title.strip()


def _topic_entry_thumbs(entry: dict) -> list[dict]:
    thumbs = []
    for thumb in entry.get("thumbnails") or []:
        if not isinstance(thumb, dict):
            continue
        url = thumb.get("url")
        if not url:
            continue
        item = {"url": url}
        if thumb.get("id"):
            item["quality"] = str(thumb["id"])
        if thumb.get("width"):
            item["width"] = thumb["width"]
        if thumb.get("height"):
            item["height"] = thumb["height"]
        thumbs.append(item)
    return thumbs


def _extract_topic_channel(channel_id: str, page: int) -> dict:
    start, end = _topic_page_bounds(page)
    with yt_dlp.YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playliststart": start,
        "playlistend": end,
    }) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/channel/{channel_id}", download=False) or {}


async def _topic_channel_fallback(channel_id: str, page: int) -> dict:
    loop = asyncio.get_running_loop()
    playlist = await loop.run_in_executor(None, _extract_topic_channel, channel_id, page)
    entries = playlist.get("entries") or []
    if not entries:
        raise Exception(f"No channel entries found for {channel_id}")

    raw_author = (
        _topic_display_name(playlist.get("title"))
        or playlist.get("channel")
        or playlist.get("uploader")
        or channel_id
    )

    search_meta = None
    try:
        results = await api_get("/search", {"q": raw_author, "page": 1, "type": "channel"})
        if isinstance(results, list):
            search_meta = next((item for item in results if item.get("authorId") == channel_id), None)
    except Exception as exc:
        logger.warning("Topic channel enrichment failed for %s: %s", channel_id, exc)

    author = (search_meta or {}).get("author") or raw_author
    videos = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("id")
        if not video_id:
            continue
        videos.append({
            "type": "video",
            "videoId": video_id,
            "title": entry.get("title") or video_id,
            "author": author,
            "authorId": channel_id,
            "lengthSeconds": entry.get("duration"),
            "published": entry.get("timestamp"),
            "videoThumbnails": _topic_entry_thumbs(entry),
        })

    if not videos:
        raise Exception(f"No playable topic channel entries found for {channel_id}")

    return _fix_thumbs({
        "author": author,
        "authorId": channel_id,
        "authorThumbnails": (search_meta or {}).get("authorThumbnails") or [],
        "authorBanners": [],
        "subCount": (search_meta or {}).get("subCount") or 0,
        "videos": videos,
    })


@router.get("/search")
async def search(q: str = Query(...), page: int = Query(1)):
    import asyncio
    try:
        videos_task = api_get("/search", {"q": q, "page": page, "type": "video"})
        playlists_task = api_get("/search", {"q": q, "page": page, "type": "playlist"})
        channels_task = api_get("/search", {"q": q, "page": page, "type": "channel"})
        videos, playlists, channels = await asyncio.gather(
            videos_task,
            playlists_task,
            channels_task,
            return_exceptions=True,
        )
        if isinstance(videos, Exception):
            videos = []
        if isinstance(playlists, Exception):
            playlists = []
        if isinstance(channels, Exception):
            channels = []
        return {
            "videos": _fix_thumbs(videos),
            "playlists": _fix_thumbs(playlists),
            "channels": _fix_thumbs(channels),
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/trending")
async def trending(region: str = Query("US")):
    try:
        return _fix_thumbs(await api_get("/trending", {"region": region}))
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/channel/{channel_id}")
async def channel(channel_id: str, page: int = Query(1)):
    info = None
    vids = None
    try:
        info, vids = await asyncio.gather(
            api_get(f"/channels/{channel_id}"),
            api_get(f"/channels/{channel_id}/videos", {"page": page}),
            return_exceptions=True,
        )
        if not isinstance(info, Exception) and not isinstance(vids, Exception):
            result = _fix_thumbs(info)
            result["videos"] = _fix_thumbs(vids).get("videos", [])
            return result

        logger.info(
            "Falling back to yt-dlp channel extraction for %s (info_error=%s videos_error=%s)",
            channel_id,
            isinstance(info, Exception),
            isinstance(vids, Exception),
        )
        fallback = await _topic_channel_fallback(channel_id, page)
        if not isinstance(info, Exception):
            result = _fix_thumbs(info)
            result["videos"] = fallback.get("videos", [])
            if not result.get("authorThumbnails") and fallback.get("authorThumbnails"):
                result["authorThumbnails"] = fallback["authorThumbnails"]
            if not result.get("subCount") and fallback.get("subCount") is not None:
                result["subCount"] = fallback["subCount"]
            return result
        return fallback
    except Exception as e:
        if not isinstance(info, Exception) and isinstance(vids, Exception):
            result = _fix_thumbs(info)
            result["videos"] = []
            return result
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/video/{video_id}/recommendations")
async def recommendations(video_id: str):
    try:
        data = await api_get(f"/videos/{video_id}")
        return _fix_thumbs(data.get("recommendedVideos", []))
    except Exception:
        pass
    # Invidious failed — fall back to yt-dlp related_videos
    try:
        info = ytdlp_service.get_raw_info(video_id)
        if not info:
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, lambda: __import__('yt_dlp').YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}).extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False))
            ytdlp_service._store_info(video_id, raw)
            info = ytdlp_service.get_raw_info(video_id)
        related = info.get("related_videos") or [] if info else []
        result = []
        for v in related:
            vid_id = v.get("id")
            if not vid_id:
                continue
            thumbs = [{"quality": "medium", "url": t["url"]} for t in (v.get("thumbnails") or []) if t.get("url")]
            ts = v.get("timestamp")
            result.append({
                "videoId": vid_id,
                "title": v.get("title") or vid_id,
                "videoThumbnails": thumbs,
                "author": v.get("uploader") or v.get("channel") or "",
                "authorId": v.get("channel_id") or "",
                "lengthSeconds": int(v.get("duration") or 0),
                "viewCountText": str(v.get("view_count") or ""),
                **({"published": int(ts)} if ts else {}),
            })
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/playlist/{playlist_id}")
async def playlist(playlist_id: str, page: int = Query(1)):
    try:
        return await api_get(f"/playlists/{playlist_id}", {"page": page})
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/feed")
async def feed(request: Request):
    token = _token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return await api_get("/auth/feed", token=token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/subscriptions")
async def subscriptions(request: Request):
    token = _token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return await api_get("/auth/subscriptions", token=token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/video/{video_id}/storyboards/sprite")
async def storyboard_sprite(video_id: str):
    from services import storyboard_service
    from fastapi.responses import FileResponse
    sprite = storyboard_service._sprite_path(video_id)
    if not os.path.exists(sprite):
        raise HTTPException(status_code=404, detail="Sprite not generated yet")
    return FileResponse(sprite, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/video/{video_id}/storyboards")
async def storyboards(video_id: str):
    # Try Invidious first
    try:
        data = await api_get(f"/storyboards/{video_id}")
        fixed = _fix_thumbs(data)
        boards = fixed if isinstance(fixed, list) else (fixed.get("storyboards") or fixed.get("storyboard") or [])
        if boards:
            return fixed
    except Exception:
        pass
    # Fall back to generated sprite-sheet storyboard
    from services import storyboard_service
    meta = await storyboard_service.get_or_wait(video_id)
    if meta:
        return [meta]
    raise HTTPException(status_code=404, detail="No storyboard available")


@router.get("/playlists")
async def playlists(request: Request):
    token = _token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        return await api_get("/auth/playlists", token=token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/vi/{path:path}")
async def thumbnail_proxy(path: str):
    """Proxy thumbnail images from Invidious so stored /vi/... paths render correctly."""
    url = f"{INVIDIOUS_URL}/vi/{path}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            if r.status_code != 200:
                raise HTTPException(status_code=r.status_code, detail="Thumbnail not found")
            return Response(
                content=r.content,
                media_type=r.headers.get("content-type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=str(e))
