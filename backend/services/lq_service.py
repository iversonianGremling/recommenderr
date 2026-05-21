"""
Download lowest-quality video+audio stream to local disk.

Used for:
  1. Storyboard generation — local file reads ~10x faster than HTTP seeking.
  2. Playback fallback — fully downloaded file supports instant seeking.
"""
import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger("lq")

_LQ_CACHE_DIR = os.getenv("LQ_CACHE_DIR", "/tmp/ytfrontend_lq")
_DOWNLOAD_TIMEOUT = 600.0

_in_progress: dict[str, asyncio.Event] = {}
_status: dict[str, str] = {}  # 'downloading' | 'done' | 'failed'


def _candidate_paths(video_id: str) -> list[str]:
    os.makedirs(_LQ_CACHE_DIR, exist_ok=True)
    return [
        os.path.join(_LQ_CACHE_DIR, f"{video_id}.webm"),
        os.path.join(_LQ_CACHE_DIR, f"{video_id}.mp4"),
    ]


def get_lq_path(video_id: str) -> Optional[str]:
    for p in _candidate_paths(video_id):
        if os.path.exists(p) and os.path.getsize(p) > 4096:
            return p
    return None


def get_lq_event(video_id: str) -> Optional[asyncio.Event]:
    return _in_progress.get(video_id)


def get_status(video_id: str) -> dict:
    path = get_lq_path(video_id)
    if path:
        return {"status": "done", "url": f"/api/video/{video_id}/lq"}
    return {"status": _status.get(video_id, "none")}


async def _do_download(video_id: str) -> Optional[str]:
    from services import ytdlp_service

    raw_info = ytdlp_service.get_raw_info(video_id)
    if not raw_info:
        try:
            await ytdlp_service.extract_formats(video_id)
            raw_info = ytdlp_service.get_raw_info(video_id)
        except Exception as exc:
            logger.warning("[lq] yt-dlp failed for %s: %s", video_id, exc)
            return None
    if not raw_info:
        return None

    video_url, audio_url, vcodec, acodec = ytdlp_service.get_mux_urls(video_id, target_height=144)
    if not video_url or not audio_url:
        logger.warning("[lq] no mux URLs for %s (vcodec=%r acodec=%r)", video_id, vcodec, acodec)
        return None

    os.makedirs(_LQ_CACHE_DIR, exist_ok=True)

    is_vp9_or_av1 = (
        vcodec.startswith("vp9") or vcodec.startswith("vp09")
        or vcodec.startswith("av1") or vcodec.startswith("av01")
    )
    is_opus = acodec.startswith("opus")
    is_aac = acodec.startswith("mp4a") or "aac" in acodec.lower()

    if is_vp9_or_av1:
        out_path = os.path.join(_LQ_CACHE_DIR, f"{video_id}.webm")
        container = "webm"
        audio_args = ["-c:a", "copy"] if is_opus else ["-c:a", "libopus", "-b:a", "48k"]
    else:
        out_path = os.path.join(_LQ_CACHE_DIR, f"{video_id}.mp4")
        container = "mp4"
        audio_args = ["-c:a", "copy"] if is_aac else ["-c:a", "aac", "-b:a", "48k"]

    extra = ["-movflags", "+faststart"] if container == "mp4" else []

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", video_url,
        "-i", audio_url,
        "-c:v", "copy",
        *audio_args,
        *extra,
        "-f", container,
        "-y", out_path,
    ]

    logger.info("[lq] downloading %s → %s (%s + %s)", video_id, container, vcodec, acodec)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_DOWNLOAD_TIMEOUT)
        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:300]
            logger.warning("[lq] ffmpeg failed for %s (rc=%d): %s", video_id, proc.returncode, err)
            if os.path.exists(out_path):
                os.unlink(out_path)
            return None
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("[lq] download timed out for %s", video_id)
        if os.path.exists(out_path):
            os.unlink(out_path)
        return None

    if not os.path.exists(out_path) or os.path.getsize(out_path) < 4096:
        logger.warning("[lq] output missing or empty for %s", video_id)
        return None

    logger.info("[lq] done for %s (%.1f MB)", video_id, os.path.getsize(out_path) / 1e6)
    return out_path


async def download_bg(video_id: str) -> None:
    """Start download in background; idempotent."""
    if get_lq_path(video_id):
        return
    if video_id in _in_progress:
        return

    event = asyncio.Event()
    _in_progress[video_id] = event
    _status[video_id] = "downloading"
    try:
        path = await _do_download(video_id)
        _status[video_id] = "done" if path else "failed"
    except Exception as exc:
        _status[video_id] = "failed"
        logger.warning("[lq] download_bg error for %s: %s", video_id, exc)
    finally:
        _in_progress.pop(video_id, None)
        event.set()
