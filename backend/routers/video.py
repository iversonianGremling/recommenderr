import os
import logging
import asyncio
import re
from difflib import SequenceMatcher
import httpx
from fastapi import APIRouter, HTTPException, Request, Query, BackgroundTasks
from pydantic import BaseModel
from fastapi.responses import StreamingResponse, Response, RedirectResponse, FileResponse
from backend.services import ytdlp_service
from backend.db import (
    delete_video_media_override,
    get_video_media_override,
    set_video_media_override,
    get_invidious_cache,
    set_invidious_cache,
)
from backend.services.invidious_client import api_get, api_get_cached
from backend.services.music_client import (
    deezer_get_album_tracks,
    deezer_search_album,
    itunes_search_album,
    spotify_get_album_tracks,
    spotify_search_album,
)

logger = logging.getLogger("video")
logging.basicConfig(level=logging.INFO)

router = APIRouter()

INVIDIOUS_URL = os.getenv("INVIDIOUS_URL", "http://192.168.1.173:3000")
_MEDIA_OVERRIDE_VALUES = {"music_video", "not_music"}
FULL_ALBUM_VIDEO_RE = re.compile(
    r"\b(full album|complete album|album stream|full ep|full lp|full soundtrack|full ost)\b",
    re.IGNORECASE,
)
TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")


def _abs(url: str) -> str:
    if url and url.startswith("/"):
        return INVIDIOUS_URL + url
    return url


def _norm_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _pick_primary_thumb(data: dict) -> str:
    thumbs = data.get("videoThumbnails") or []
    if not thumbs:
        return ""
    return _abs((thumbs[-1] or {}).get("url", ""))


def _music_cover_score(album_hint: str, artist_hint: str, candidate: dict) -> float:
    clean_album = _norm_text(album_hint)
    clean_artist = _norm_text(artist_hint)
    cand_album = _norm_text(candidate.get("title"))
    cand_artist = _norm_text(candidate.get("artist"))
    if not clean_album or not cand_album:
        return 0.0

    album_score = SequenceMatcher(None, clean_album, cand_album).ratio()
    artist_score = SequenceMatcher(None, clean_artist, cand_artist).ratio() if clean_artist and cand_artist else 0.0
    if clean_album == cand_album:
        album_score = 1.0
    elif clean_album in cand_album or cand_album in clean_album:
        album_score = max(album_score, 0.92)
    if clean_artist and cand_artist and (clean_artist == cand_artist or clean_artist in cand_artist or cand_artist in clean_artist):
        artist_score = max(artist_score, 0.95)
    return (album_score * 0.8) + (artist_score * 0.2)


def _description_has_timestamps(description: str | None) -> bool:
    if not description:
        return False
    return bool(TIMESTAMP_RE.search(description))


def _extract_album_hint(data: dict) -> tuple[str, str]:
    album = (data.get("album") or "").strip()
    artist = (data.get("artist") or data.get("author") or "").strip()
    if album:
        return album, artist

    title = (data.get("title") or "").strip()
    if not FULL_ALBUM_VIDEO_RE.search(title):
        return "", artist

    cleaned = FULL_ALBUM_VIDEO_RE.sub(" ", title)
    cleaned = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    if " - " in cleaned:
        maybe_artist, maybe_album = cleaned.split(" - ", 1)
        maybe_artist = maybe_artist.strip()
        maybe_album = maybe_album.strip()
        if maybe_artist and maybe_album:
            return maybe_album, artist or maybe_artist
    return cleaned, artist


async def _search_music_album_candidate(album: str, artist: str) -> dict | None:
    if not album:
        return None
    query = f"{artist} {album}".strip()
    results = await asyncio.gather(
        itunes_search_album(query, limit=3),
        deezer_search_album(query, limit=3),
        spotify_search_album(query, limit=3),
        return_exceptions=True,
    )

    best = None
    best_score = 0.0
    for batch in results:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            score = _music_cover_score(album, artist, item)
            if score > best_score:
                best = item
                best_score = score

    if best and best_score >= 0.6:
        return best
    return None


async def _fetch_album_tracks(candidate: dict) -> tuple[list[dict], str]:
    batches: list[tuple[int, list[dict], str]] = []

    if candidate.get("deezer_album_id"):
        tracks = await deezer_get_album_tracks(candidate.get("deezer_album_id"), limit=100)
        if tracks:
            batches.append((3, tracks, "deezer"))
    if candidate.get("spotify_album_id"):
        tracks = await spotify_get_album_tracks(candidate.get("spotify_album_id"), limit=100)
        if tracks:
            batches.append((4, tracks, "spotify"))

    if not batches:
        return [], ""

    batches.sort(key=lambda item: (len(item[1]), item[0]), reverse=True)
    _, tracks, source = batches[0]
    return tracks, source


def _build_track_markers(tracks: list[dict], duration: int | None) -> list[dict]:
    if not tracks or not duration or duration < 1 or len(tracks) < 2:
        return []

    ordered = sorted(
        tracks,
        key=lambda item: (int(item.get("disc_number") or 1), int(item.get("position") or 0)),
    )
    total_track_duration = sum(int(item.get("duration") or 0) for item in ordered)
    if total_track_duration <= 0:
        return []

    scale = float(duration) / float(total_track_duration)
    if scale < 0.5 or scale > 1.55:
        return []

    markers = []
    cursor = 0
    for index, item in enumerate(ordered):
        raw_duration = int(item.get("duration") or 0)
        scaled_duration = max(1, round(raw_duration * scale))
        markers.append({
            "index": index + 1,
            "title": item.get("title") or f"Track {index + 1}",
            "start": cursor,
            "duration": scaled_duration,
            "source": item.get("source") or "",
        })
        cursor += scaled_duration

    return markers


async def _build_album_track_markers(data: dict) -> tuple[list[dict], str]:
    if _description_has_timestamps(data.get("description")):
        return [], ""

    duration = int(data.get("lengthSeconds") or 0)
    # Long uploads only (full albums / mixes); short EPs still qualify at ~8+ minutes
    if duration < 480:
        return [], ""

    album_hint, artist_hint = _extract_album_hint(data)
    if not album_hint:
        return [], ""

    candidate = await _search_music_album_candidate(album_hint, artist_hint)
    if not candidate:
        return [], ""

    tracks, source = await _fetch_album_tracks(candidate)
    markers = _build_track_markers(tracks, duration)
    return markers, source


async def _build_video_payload(video_id: str, data: dict, formats: list[dict], subtitles: list[dict]) -> dict:
    media_override = get_video_media_override(video_id)
    album_track_markers, album_track_markers_source = await _build_album_track_markers(data)

    thumbs = [
        {"quality": t.get("quality"), "url": _abs(t.get("url", ""))}
        for t in data.get("videoThumbnails", [])
    ]

    return {
        "id": video_id,
        "title": data.get("title"),
        "description": data.get("description"),
        "duration": data.get("lengthSeconds"),
        "uploader": data.get("author"),
        "uploader_id": data.get("authorId"),
        "genre": data.get("genre"),
        "music_video_type": data.get("musicVideoType"),
        "track": data.get("track") or data.get("song"),
        "artist": data.get("artist"),
        "album": data.get("album"),
        "upload_date": data.get("published"),
        "view_count": data.get("viewCount"),
        "like_count": data.get("likeCount"),
        "thumbnail": _pick_primary_thumb(data),
        "thumbnails": thumbs,
        "formats": formats,
        "subtitles": subtitles,
        "media_override": media_override,
        "playback_kind": "video",
        "is_music_video": False,
        "download_mode": "video",
        "album_track_markers": album_track_markers,
        "album_track_markers_source": album_track_markers_source,
    }


def _parse_invidious_formats(data: dict, video_id: str) -> list[dict]:
    formats = []

    for f in data.get("formatStreams", []):
        url = f.get("url", "")
        if not url:
            continue
        fid = f"inv_{f.get('itag', len(formats))}"
        ytdlp_service.store_url(video_id, fid, _abs(url))
        formats.append({
            "format_id": fid,
            "height": int(f.get("resolution", "0p").rstrip("p") or 0),
            "fps": f.get("fps", 30),
            "vcodec": (f.get("type", "").split('"')[1] if '"' in f.get("type", "") else ""),
            "has_audio": True,
            "source": "invidious",
        })

    seen_heights = set()
    for f in data.get("adaptiveFormats", []):
        mime = f.get("type", "")
        if not mime.startswith("video/"):
            continue
        height = f.get("height") or 0
        if height < 144:
            continue
        if height in seen_heights:
            continue
        seen_heights.add(height)
        url = f.get("url", "")
        if not url:
            continue
        fid = f"inv_ada_{f.get('itag', height)}"
        ytdlp_service.store_url(video_id, fid, _abs(url))
        formats.append({
            "format_id": fid,
            "height": height,
            "fps": f.get("fps", 30),
            "vcodec": mime.split(";")[0].split("/")[1] if "/" in mime else "",
            "has_audio": False,
            "source": "invidious",
        })

    return sorted(formats, key=lambda x: (-x["height"], not x["has_audio"]))


def _parse_subtitles(data: dict, video_id: str) -> list[dict]:
    out = []
    for caption in data.get("captions", []):
        lang = caption.get("language_code", "").strip()
        label = caption.get("label", lang)
        url = caption.get("url", "")
        if url and lang:
            out.append({"language": lang, "label": label})
    return out



async def _ytdlp_video_meta(video_id: str) -> dict:
    """Build an Invidious-compatible metadata dict from yt-dlp when Invidious is unavailable."""
    await ytdlp_service.extract_formats(video_id)
    info = ytdlp_service.get_raw_info(video_id)
    if not info:
        raise ValueError("yt-dlp extraction returned no metadata")

    thumbs = []
    for t in info.get("thumbnails") or []:
        url = t.get("url", "")
        if url:
            thumbs.append({"quality": str(t.get("preference") or t.get("id") or "medium"), "url": url})

    categories = info.get("categories") or []
    genre = categories[0] if categories else ""

    format_streams = []
    adaptive_formats = []
    for f in info.get("formats") or []:
        if not f.get("url"):
            continue
        proto = f.get("protocol", "")
        if "m3u8" in proto:
            # HLS manifests can't be byte-range proxied; skip them.
            # The mux endpoint handles quality/seeking for these formats.
            continue
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height") or 0
        ext = f.get("ext", "mp4")
        if vcodec != "none" and acodec != "none" and height >= 144:
            format_streams.append({
                "itag": f.get("format_id", ""),
                "url": f["url"],
                "type": f'video/{ext}; codecs="{vcodec}"',
                "resolution": f"{height}p",
                "fps": f.get("fps", 30),
            })
        elif vcodec != "none" and acodec == "none" and height >= 144:
            adaptive_formats.append({
                "itag": f.get("format_id", ""),
                "url": f["url"],
                "type": f'video/{ext}; codecs="{vcodec}"',
                "height": height,
                "fps": f.get("fps", 30),
            })

    captions = []
    for lang, tracks in (info.get("subtitles") or {}).items():
        if tracks:
            captions.append({"language_code": lang, "label": lang, "url": (tracks[0] or {}).get("url", "")})
    for lang, tracks in (info.get("automatic_captions") or {}).items():
        if tracks:
            captions.append({"language_code": lang, "label": f"{lang} (auto)", "url": (tracks[0] or {}).get("url", "")})

    return {
        "title": info.get("title") or video_id,
        "description": info.get("description") or "",
        "lengthSeconds": int(info.get("duration") or 0),
        "author": info.get("uploader") or info.get("channel") or "",
        "authorId": info.get("channel_id") or "",
        "published": int(info.get("timestamp") or 0),
        "viewCount": info.get("view_count") or 0,
        "likeCount": info.get("like_count") or 0,
        "videoThumbnails": thumbs,
        "genre": genre,
        "musicVideoType": None,
        "track": info.get("track") or "",
        "song": "",
        "artist": info.get("artist") or info.get("creator") or "",
        "album": info.get("album") or "",
        "formatStreams": format_streams,
        "adaptiveFormats": adaptive_formats,
        "captions": captions,
        "recommendedVideos": [],
    }

@router.get("/{video_id}/prefetch")
async def prefetch(video_id: str):
    """Kick off yt-dlp extraction immediately and return without waiting."""
    asyncio.create_task(_bg_extract(video_id))
    return {}


async def _fallback_metadata(video_id: str) -> dict:
    """Return cached metadata from ytvideo watch_history when both Invidious and yt-dlp fail."""
    ytvideo_db = os.getenv("YTVIDEO_DB_PATH", "/opt/ytvideo/data/ytvideo.db")
    try:
        import sqlite3
        loop = asyncio.get_running_loop()
        def _query():
            conn = sqlite3.connect(ytvideo_db)
            try:
                return conn.execute(
                    "SELECT title, thumbnail, author, duration FROM watch_history WHERE video_id = ? LIMIT 1",
                    (video_id,),
                ).fetchone()
            finally:
                conn.close()
        row = await loop.run_in_executor(None, _query)
        if row:
            title, thumbnail, author, duration = row
            return {
                "title": title or video_id,
                "videoThumbnails": [{"quality": "medium", "url": thumbnail}] if thumbnail else [],
                "author": author or "",
                "authorId": "",
                "lengthSeconds": int(duration) if duration else 0,
                "description": "",
                "genre": "",
            }
    except Exception as e:
        logger.warning(f"[video_info] watch_history fallback failed for {video_id}: {e}")
    return {"title": video_id, "videoThumbnails": [], "author": "", "authorId": "", "lengthSeconds": 0, "description": "", "genre": ""}


def _fh_record(method: str, ok: bool, err: str = "") -> None:
    """Report a fetch outcome to the fetch-health bus (best-effort)."""
    try:
        from backend.services import fetch_health
        if ok:
            fetch_health.record_success(method)
        else:
            fetch_health.record_failure(method, err)
    except Exception:  # noqa: BLE001 — health reporting must never break a fetch
        pass


@router.get("/{video_id}/info")
async def video_info(video_id: str):
    # Start yt-dlp extraction immediately so it runs concurrently with the
    # Invidious fetch and music-metadata work; formats will be ready (or close
    # to it) by the time the mux endpoint is called.
    asyncio.create_task(_bg_extract(video_id))
    asyncio.create_task(_bg_storyboard(video_id))
    asyncio.create_task(_bg_lq_download(video_id))

    # Aggressive per-video payload cache. The assembled payload (description,
    # album-track markers built from external music APIs, thumbnails, subtitles)
    # is deterministic per video and expensive to rebuild, yet today it is
    # recomputed on every hit even though the raw Invidious data is cached. Cache
    # the whole payload for 6h (aligned with the raw Invidious TTL, so embedded
    # format URLs are no staler than before). media_override is user-mutable, so
    # it is refreshed live on every read rather than served from cache.
    _payload_cache_key = f"video_payload:{video_id}"
    _cached_payload = get_invidious_cache(_payload_cache_key)
    if _cached_payload is not None:
        _cached_payload["media_override"] = get_video_media_override(video_id)
        return _cached_payload

    inv_err_str: str | None = None
    camoufox_err_str: str | None = None
    ytdlp_err_str: str | None = None
    served_by = "invidious"

    # Fetch cascade: Invidious → yt-dlp → camoufox → static fallback.
    # yt-dlp is fast (it reuses the background extraction already in flight) and,
    # with the rotating Mullvad exit + PO tokens, now reliable — so it's the
    # primary fast path. camoufox (a real browser, but slow: up to ~70s and it
    # runs every strategy before giving up) is the deep fallback for when yt-dlp
    # is genuinely bot-blocked, where its browser fingerprint still gets through.
    try:
        data = await api_get_cached(f"/videos/{video_id}", ttl=6 * 3600)
        _fh_record("invidious", True)
    except Exception as inv_err:
        inv_err_str = str(inv_err)
        _fh_record("invidious", False, inv_err_str)
        logger.warning(f"[video_info] Invidious failed for {video_id}: {inv_err}, trying yt-dlp")
        try:
            data = await _ytdlp_video_meta(video_id)
            served_by = "ytdlp"
        except Exception as ytdlp_err:
            ytdlp_err_str = str(ytdlp_err)
            logger.warning(f"[video_info] yt-dlp failed for {video_id}: {ytdlp_err}, trying camoufox")
            try:
                from backend.services.invidious_client import camoufox_get
                data = await camoufox_get(f"/videos/{video_id}")
                served_by = "camoufox"
                _fh_record("camoufox", True)
                logger.info(f"[video_info] camoufox succeeded for {video_id}")
            except Exception as camou_err:
                camoufox_err_str = str(camou_err)
                _fh_record("camoufox", False, camoufox_err_str)
                logger.warning(f"[video_info] camoufox also failed for {video_id}: {camou_err}")
                served_by = "fallback"
                data = await _fallback_metadata(video_id)

    formats = _parse_invidious_formats(data, video_id)
    payload = await _build_video_payload(video_id, data, formats, _parse_subtitles(data, video_id))

    payload["source"] = served_by
    if inv_err_str is not None:
        bot_detected = ytdlp_err_str is not None and (
            "Sign in to confirm" in ytdlp_err_str or "not a bot" in ytdlp_err_str
        )
        payload["stream_error"] = {
            "invidious": inv_err_str,
            "camoufox": camoufox_err_str,
            "ytdlp": ytdlp_err_str,
            "bot_detected": bot_detected,
            "served_by": served_by,
        }

    # Cache any complete payload except the degraded static fallback. yt-dlp and
    # camoufox results are full metadata too, and Invidious is frequently
    # rate-limited, so gating on invidious-only would leave the cache empty.
    # Strip the transient stream_error so cache hits serve clean metadata.
    if served_by != "fallback" and payload.get("title"):
        try:
            _to_cache = dict(payload)
            _to_cache.pop("stream_error", None)
            set_invidious_cache(_payload_cache_key, _to_cache, 6 * 3600)
        except Exception:
            logger.warning(f"[video_info] payload cache write failed for {video_id}", exc_info=True)

    return payload


async def _bg_extract(video_id: str):
    try:
        await ytdlp_service.extract_formats(video_id)
    except Exception:
        pass


async def _bg_storyboard(video_id: str):
    try:
        from backend.services import storyboard_service
        await storyboard_service.generate_bg(video_id)
    except Exception:
        pass


async def _bg_lq_download(video_id: str):
    try:
        from backend.services import lq_service
        await lq_service.download_bg(video_id)
    except Exception:
        pass


@router.get("/{video_id}/ytdlp-status")
async def ytdlp_status(video_id: str):
    """Non-blocking: returns whether yt-dlp extraction is cached and ready."""
    return ytdlp_service.get_status(video_id)


@router.get("/{video_id}/formats")
async def ytdlp_formats(video_id: str):
    """Returns yt-dlp formats. Blocks until extraction completes."""
    try:
        formats = await ytdlp_service.extract_formats(video_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return formats


async def _ensure_url(video_id: str, format_id: str) -> str:
    url = ytdlp_service.get_url(video_id, format_id)
    if url:
        return url

    try:
        await ytdlp_service.extract_formats(video_id)
    except Exception:
        pass
    url = ytdlp_service.get_url(video_id, format_id)
    if url:
        return url

    if format_id.startswith("inv_"):
        try:
            data = await api_get(f"/videos/{video_id}")
            _parse_invidious_formats(data, video_id)
        except Exception:
            pass
        url = ytdlp_service.get_url(video_id, format_id)

    return url


@router.get("/{video_id}/stream")
async def stream_video(video_id: str, request: Request, format_id: str = Query(...)):
    url = await _ensure_url(video_id, format_id)
    if not url:
        raise HTTPException(status_code=404, detail="Format URL not found")

    range_header = request.headers.get("range")
    result = await _proxy_stream(url, range_header)
    if result is None:
        try:
            await ytdlp_service.extract_formats(video_id)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"yt-dlp refresh failed: {e}")
        url = await _ensure_url(video_id, format_id)
        if not url:
            raise HTTPException(status_code=502, detail="Could not refresh stream URL")
        result = await _proxy_stream(url, range_header)
        if result is None:
            raise HTTPException(status_code=502, detail="Stream URL invalid after refresh")

    status, headers, body_gen = result
    return StreamingResponse(body_gen, status_code=status, headers=headers)


@router.head("/{video_id}/stream")
async def stream_video_head(video_id: str, format_id: str = Query(...)):
    url = await _ensure_url(video_id, format_id)
    if not url:
        raise HTTPException(status_code=404, detail="Format URL not found")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.head(url)

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="Upstream error")

    headers = {
        "Content-Type": resp.headers.get("content-type", "video/mp4"),
        "Accept-Ranges": "bytes",
    }
    if "content-length" in resp.headers:
        headers["Content-Length"] = resp.headers["content-length"]

    return Response(content=b"", status_code=200, headers=headers)


async def _probe_url(url: str) -> bool:
    """Quick HEAD/range check — returns False if URL is stale (403/404)."""
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as c:
            r = await c.get(url, headers={"Range": "bytes=0-0"})
            return r.status_code < 400
    except Exception:
        return False


@router.get("/{video_id}/mux")
async def mux_stream(video_id: str, quality: int = Query(default=720), start: float = Query(default=0.0)):
    """Mux VP9 video + Opus audio into WebM via ffmpeg. Works in all browsers including LibreWolf."""
    try:
        await ytdlp_service.extract_formats(video_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"yt-dlp extraction failed: {e}")

    video_url, audio_url, vcodec, acodec = ytdlp_service.get_mux_urls(video_id, quality)
    if not video_url or not audio_url:
        raise HTTPException(status_code=404, detail="no yt-dlp formats available")

    # Probe both URLs — if either is stale, force a fresh extraction and retry once
    v_ok, a_ok = await asyncio.gather(_probe_url(video_url), _probe_url(audio_url))
    if not v_ok or not a_ok:
        logger.warning(f"[mux] {video_id} stale URLs (video={v_ok} audio={a_ok}), re-extracting")
        ytdlp_service.invalidate(video_id)
        try:
            await ytdlp_service.extract_formats(video_id)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"yt-dlp re-extraction failed: {e}")
        video_url, audio_url, vcodec, acodec = ytdlp_service.get_mux_urls(video_id, quality)
        if not video_url or not audio_url:
            raise HTTPException(status_code=502, detail="no formats after re-extraction")

    # VP9/VP8/AV1 can be stream-copied from position 0; AVC must always be transcoded (LibreWolf has no H.264).
    # When seeking (start > 0) we must transcode even for VP9: stream-copying a mid-stream VP9 chunk
    # omits the codec sequence header that the browser needs to initialise its decoder, so it decodes
    # only the first keyframe then freezes while audio keeps playing.
    needs_transcode = start > 0 or not (vcodec.startswith(("vp9", "vp09", "vp8", "av1", "av01")))
    audio_needs_transcode = not (acodec.startswith("opus") or acodec.startswith("vorbis"))
    video_codec_args = ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "1200k"] if needs_transcode else ["-c:v", "copy"]
    audio_codec_args = ["-c:a", "libopus", "-b:a", "96k"] if audio_needs_transcode else ["-c:a", "copy"]
    action = ("transcode→VP8(seek)" if start > 0 else "transcode→VP8") if needs_transcode else "copy"
    logger.info(f"[mux] {video_id} quality={quality} start={start:.1f}s {vcodec}+{acodec} ({action})→webm")

    # Build ffmpeg args — fast-seek both inputs to the requested start position
    # -reconnect/-reconnect_streamed: retry if YouTube CDN drops the audio or video URL
    # mid-stream; without these ffmpeg silently produces muted output when the audio
    # connection drops.
    reconnect = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        ff += ["-ss", f"{start:.3f}"]
    ff += reconnect + ["-i", video_url]
    if start > 0:
        ff += ["-ss", f"{start:.3f}"]
    ff += reconnect + ["-i", audio_url] + video_codec_args + audio_codec_args + ["-map", "0:v:0", "-map", "1:a:0", "-avoid_negative_ts", "make_zero", "-f", "webm", "pipe:1"]

    proc = await asyncio.create_subprocess_exec(
        *ff,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Give ffmpeg a moment to fail fast (e.g. bad URL, missing codec)
    await asyncio.sleep(0.5)
    if proc.returncode is not None:
        stderr = await proc.stderr.read()
        logger.error(f"[mux] {video_id} ffmpeg exited immediately (rc={proc.returncode}): {stderr.decode(errors='replace')}")
        raise HTTPException(status_code=502, detail="ffmpeg failed to start stream")

    async def generate():
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            stderr = await proc.stderr.read()
            if proc.returncode not in (0, -9):  # -9 = killed by us on disconnect
                logger.error(f"[mux] {video_id} ffmpeg error (rc={proc.returncode}): {stderr.decode(errors='replace').strip()}")

    return StreamingResponse(
        generate(),
        media_type="video/webm",
        headers={
            "Accept-Ranges": "none",
            "Cache-Control": "no-cache",
        },
    )


# ── Background download endpoints ─────────────────────────────────────────────

class MediaOverrideRequest(BaseModel):
    mode: str


@router.get("/{video_id}/media-override")
async def get_media_override(video_id: str):
    return {"media_override": get_video_media_override(video_id)}


@router.post("/{video_id}/media-override")
async def save_media_override(video_id: str, body: MediaOverrideRequest):
    if body.mode not in _MEDIA_OVERRIDE_VALUES:
        raise HTTPException(status_code=400, detail="Unsupported media override")
    set_video_media_override(video_id, body.mode)
    return {"ok": True, "media_override": body.mode}


@router.delete("/{video_id}/media-override")
async def clear_media_override(video_id: str):
    delete_video_media_override(video_id)
    return {"ok": True}

@router.post("/{video_id}/download")
async def trigger_download(video_id: str):
    """Start background yt-dlp download (idempotent, non-blocking)."""
    try:
        data = await api_get(f"/videos/{video_id}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Invidious error: {e}")
    await ytdlp_service.start_download(video_id, mode="video")
    return ytdlp_service.get_download_state(video_id)


@router.get("/{video_id}/download-status")
async def download_status(video_id: str):
    """Poll download progress."""
    return ytdlp_service.get_download_state(video_id)


@router.delete("/{video_id}/download")
async def delete_download(video_id: str):
    """Delete a downloaded video file and reset its state."""
    deleted = ytdlp_service.delete_download(video_id)
    return {"ok": True, "deleted": deleted}


@router.get("/{video_id}/local")
async def local_stream(video_id: str, request: Request):
    """Serve the locally-downloaded video file with full Range support."""
    state = ytdlp_service.get_download_state(video_id)
    if state.get("status") != "done" or not state.get("path"):
        raise HTTPException(status_code=404, detail="Not downloaded yet")
    path = state["path"]
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found on disk")
    ext = os.path.splitext(path)[1].lower()
    media_type = {
        ".webm": "audio/webm" if state.get("mode") == "audio" else "video/webm",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".opus": "audio/ogg",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
        ".wav": "audio/wav",
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
    }.get(ext, "application/octet-stream")
    return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})


@router.get("/{video_id}/lq/status")
async def lq_status(video_id: str):
    from backend.services import lq_service
    return lq_service.get_status(video_id)


@router.get("/{video_id}/lq")
async def lq_video(video_id: str):
    from backend.services import lq_service
    path = lq_service.get_lq_path(video_id)
    if not path:
        raise HTTPException(status_code=404, detail="LQ file not ready")
    media_type = "video/webm" if path.endswith(".webm") else "video/mp4"
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": "no-cache", "Accept-Ranges": "bytes"},
    )


# ─────────────────────────────────────────────────────────────────────────────

def _apply_range_to_url(url: str, range_header: str | None) -> tuple[str, dict[str, str], int | None, int | None]:
    """For YouTube CDN URLs, convert HTTP Range header to ?range= URL param.

    YouTube's pre-muxed CDN URLs (googlevideo.com) ignore the HTTP Range header
    and require byte ranges as a URL parameter instead. Returns the (possibly
    modified) URL, request headers, and the parsed start/end bytes so the caller
    can synthesise a 206 response.
    """
    req_headers: dict[str, str] = {"Accept-Encoding": "identity"}
    range_start: int | None = None
    range_end: int | None = None

    if not range_header:
        return url, req_headers, None, None

    is_ytcdn = "googlevideo.com" in url
    m = re.match(r"bytes=(\d+)-(\d*)", range_header)

    if not m or not is_ytcdn:
        # Non-YouTube CDN or unparseable range — forward the header as-is.
        req_headers["Range"] = range_header
        return url, req_headers, None, None

    range_start = int(m.group(1))
    range_end = int(m.group(2)) if m.group(2) else None
    range_param = f"{range_start}-{range_end}" if range_end is not None else f"{range_start}-"
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}range={range_param}"
    return url, req_headers, range_start, range_end


async def _proxy_stream(url: str, range_header: str | None):
    url, req_headers, range_start, range_end = _apply_range_to_url(url, range_header)

    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=8.0, read=None, write=None, pool=None), follow_redirects=True, http2=False)
    try:
        resp = await client.send(
            client.build_request("GET", url, headers=req_headers),
            stream=True,
        )
    except Exception as e:
        logger.warning(f"[stream] connection error: {e}")
        await client.aclose()
        return None

    if resp.status_code >= 400:
        logger.warning(f"[stream] upstream {resp.status_code}")
        await resp.aclose()
        await client.aclose()
        return None

    content_type = resp.headers.get("content-type", "video/mp4").split(";")[0].strip()

    # When we rewrote Range → ?range=, YouTube returns 200 with only the
    # requested bytes. Synthesise a 206 response so the browser can seek.
    if range_start is not None and resp.status_code == 200:
        status = 206
        cl = resp.headers.get("content-length")
        end_str = str(range_end) if range_end is not None else (str(range_start + int(cl) - 1) if cl else "*")
        total = resp.headers.get("x-content-length", "*")
        content_range = f"bytes {range_start}-{end_str}/{total}"
    else:
        status = resp.status_code
        content_range = resp.headers.get("content-range")

    resp_headers: dict[str, str] = {
        "Content-Type": content_type,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
    }
    if content_range:
        resp_headers["Content-Range"] = content_range
    if "content-length" in resp.headers:
        resp_headers["Content-Length"] = resp.headers["content-length"]

    logger.info(f"[stream] proxying status={status} ct={content_type} range_rewrite={range_start is not None}")

    async def body():
        try:
            async for chunk in resp.aiter_bytes(65536):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return status, resp_headers, body()


@router.get("/{video_id}/subtitle")
async def get_subtitle(video_id: str, label: str = Query(...)):
    is_auto = "auto" in label.lower()

    # Try Invidious first to resolve label → language_code (Invidious may use display names)
    lang = None
    try:
        data = await api_get(f"/videos/{video_id}")
        for caption in data.get("captions", []):
            if caption.get("label") == label:
                lang = caption.get("language_code", "").strip()
                break
    except Exception:
        pass

    # Fall back: yt-dlp labels are the lang code directly (e.g. "en", "en (auto)")
    if not lang:
        lang = label.replace(" (auto)", "").strip()

    # Prefer yt-dlp subtitle URL (avoids invidious rate-limiting)
    url = ytdlp_service.get_subtitle_url(video_id, lang, is_auto)
    if not url:
        try:
            await ytdlp_service.extract_formats(video_id)
        except Exception:
            pass
        url = ytdlp_service.get_subtitle_url(video_id, lang, is_auto)

    if url:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url)
        if r.status_code == 200:
            vtt = _add_vtt_padding(r.text)
            return Response(content=vtt.encode(), media_type="text/vtt",
                            headers={"Access-Control-Allow-Origin": "*"})

    raise HTTPException(status_code=502, detail="Could not fetch subtitle")


def _add_vtt_padding(vtt: str) -> str:
    """Normalise cue positioning: strip any source position/align/line settings and
    replace with centered, above-controls placement."""
    import re
    lines = vtt.splitlines()
    out = []
    for line in lines:
        if "-->" in line:
            # Strip all positioning cue settings from the source (auto-generated captions
            # frequently include position:X% that pins them to the left/right of the frame)
            line = re.sub(r'\b(line|position|align|region|size):[^\s]+', '', line).rstrip()
            line += " line:85% position:50% align:center"
        out.append(line)
    return "\n".join(out)


class PPRFeedRequest(BaseModel):
    seeds: list[str] = []
    limit: int = 100
    offset: int = 0
    category: str = ""
    sort: str = "score"


@router.post("/ppr/feed")
async def ppr_feed(req: PPRFeedRequest) -> dict:
    """Return PPR-ranked recommendations, syncing user data from ytvideo first."""
    from backend.services.user_data_sync import sync_user_data_cache
    from backend.db import get_ppr_feed
    await sync_user_data_cache()
    rows = get_ppr_feed(
        limit=req.limit,
        offset=req.offset,
        category=req.category or None,
        sort=req.sort,
    )
    return {"items": rows, "total": len(rows)}
