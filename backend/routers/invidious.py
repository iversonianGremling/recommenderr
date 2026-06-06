import asyncio
import logging
import os
import httpx
import yt_dlp
from fastapi import APIRouter, HTTPException, Query, Request, Response
from backend.services.invidious_client import api_get, api_get_cached
from backend.services import ytdlp_service, exit_manager
from backend.db import get_invidious_cache, set_invidious_cache

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
    name = title[len(prefix):] if title.startswith(prefix) else title
    name = name.strip()
    # The /videos tab titles itself "<Channel> - Videos"; drop that suffix.
    if name.endswith(" - Videos"):
        name = name[:-len(" - Videos")].strip()
    return name


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
        # Locked-down egress: a bare YoutubeDL hits www.youtube.com directly and
        # gets Connection refused. Route through the same rotating Mullvad SOCKS
        # proxy + cookies the (working) video service uses.
        **ytdlp_service._proxy_opts(),
        **ytdlp_service._cookie_opts(),
    }) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/channel/{channel_id}/videos", download=False) or {}


async def _topic_channel_fallback(channel_id: str, page: int) -> dict:
    loop = asyncio.get_running_loop()
    # Egress through the shared Mullvad SOCKS exit is flaky under load — a single
    # attempt can hit a transient "general SOCKS server failure". Retry a couple
    # times (a fresh attempt usually succeeds) before giving up.
    playlist: dict = {}
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            playlist = await loop.run_in_executor(None, _extract_topic_channel, channel_id, page)
            if playlist.get("entries"):
                break
        except Exception as exc:
            last_exc = exc
            logger.warning("[channel] extract attempt %d/3 failed for %s: %s",
                           attempt + 1, channel_id, str(exc)[:140])
            if attempt < 2:
                # Connection/SOCKS failure usually means the shared exit IP is
                # being refused by YouTube — rotate to a fresh one before retry.
                if ytdlp_service._is_conn_error(exc):
                    exit_manager.note_conn_fail()
                    if exit_manager.should_rotate_now():
                        await exit_manager.rotate("ytdlp")
                    else:
                        await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(1.0)
    entries = playlist.get("entries") or []
    if not entries:
        if last_exc is not None:
            raise Exception(f"Channel extraction failed for {channel_id}: {last_exc}")
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

        # yt-dlp fallback for video results when Invidious is down/empty.
        # Page 1 only — yt-dlp flat search isn't paginated like Invidious.
        if not videos and page == 1:
            try:
                from backend.services import ytdlp_service
                videos = await ytdlp_service.search_youtube(q, limit=20)
                if videos:
                    logger.info("[search] Invidious empty for %r — served %d videos via yt-dlp", q, len(videos))
            except Exception as e:
                logger.warning("[search] yt-dlp search fallback failed for %r: %s", q, e)

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
            api_get_cached(f"/channels/{channel_id}", ttl=24 * 3600),
            api_get_cached(f"/channels/{channel_id}/videos", {"page": page}, ttl=6 * 3600),
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
    # Per-video recommendations cache, checked FIRST so a warm (or negatively
    # cached) entry skips the upstream Invidious call entirely — important when
    # egress is down, since that call otherwise blocks ~10s on timeout every
    # time before any fallback runs. `is not None` so a cached-empty list counts.
    _recs_key = f"video_recs:{video_id}"
    _cached_recs = get_invidious_cache(_recs_key)
    if _cached_recs is not None:
        return _cached_recs
    try:
        data = await api_get_cached(f"/videos/{video_id}", ttl=6 * 3600)
        recs = _fix_thumbs(data.get("recommendedVideos", []))
        # Real recs: cache 6h. Empty: short negative-cache TTL so repeated views
        # don't re-run the slow path (and hammer egress) when nothing is available.
        set_invidious_cache(_recs_key, recs, 6 * 3600 if recs else 600)
        return recs
    except Exception:
        pass
    # No cache — fall back to yt-dlp related_videos
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
        set_invidious_cache(_recs_key, result, 6 * 3600 if result else 600)
        return result
    except Exception as e:
        # camoufox fallback: scrape YouTube directly
        try:
            from backend.services.invidious_client import camoufox_get
            data = await camoufox_get(f"/videos/{video_id}")
            recs = _fix_thumbs(data.get("recommendedVideos", []))
            set_invidious_cache(_recs_key, recs, 6 * 3600 if recs else 600)
            return recs
        except Exception:
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
    from backend.services import storyboard_service
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
    from backend.services import storyboard_service
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
