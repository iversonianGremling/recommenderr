"""
Generate scrub-preview sprite sheets from video frames using ffmpeg.

Flow:
  - video_info triggers generate_bg() as an asyncio background task.
  - storyboards endpoint calls get_or_wait() which returns the cached result
    or waits up to `wait_secs` for an in-progress generation to finish.
  - Sprites are stored on disk at CACHE_DIR/{video_id}.jpg, metadata at .json.

Extraction strategy: the video stream is divided into _SEGMENTS parallel
ffmpeg processes.  Each seeks to its start offset (HTTP Range request) and
reads only its share of the stream, so total wall-clock time is roughly
1/_SEGMENTS of a sequential pass regardless of video length.
"""

import asyncio
import json
import logging
import os
import shutil
import time
from typing import Optional

logger = logging.getLogger("storyboard")

_CACHE_DIR = os.getenv("STORYBOARD_CACHE_DIR", "/tmp/ytfrontend_storyboards")
_COLS = 10
_ROWS = 10
_FRAME_COUNT = _COLS * _ROWS  # 100 total frames
_THUMB_W = 160
_THUMB_H = 90
_SEGMENT_TIMEOUT = 120.0  # per-segment ffmpeg timeout
_TILE_TIMEOUT = 30.0
_SEGMENTS = 5  # parallel ffmpeg processes

# Track in-progress generations so parallel requests don't double-generate
_in_progress: dict[str, asyncio.Event] = {}

# Negative cache: videos that failed (unavailable, age-restricted, etc.)
_failed_videos: dict[str, float] = {}
_FAIL_TTL = 600.0  # 10 minutes — don't retry a failed video within this window


def _is_failed(video_id: str) -> bool:
    ts = _failed_videos.get(video_id)
    if ts is None:
        return False
    if time.time() - ts < _FAIL_TTL:
        return True
    del _failed_videos[video_id]
    return False


def _mark_failed(video_id: str) -> None:
    _failed_videos[video_id] = time.time()


def _sprite_path(video_id: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"{video_id}.jpg")


def _meta_path(video_id: str) -> str:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"{video_id}.json")


def get_cached(video_id: str) -> Optional[dict]:
    path = _meta_path(video_id)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return None


async def _run_segment(
    video_url: str,
    seg_idx: int,
    start_frame: int,
    n_frames: int,
    start_sec: float,
    duration_sec: float,
    interval: int,
    frame_dir: str,
) -> int:
    """Extract n_frames frames starting at start_sec; returns count of frames written."""
    pattern = os.path.join(frame_dir, f"s{seg_idx}_%04d.jpg")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-skip_frame", "noref",
        "-ss", str(int(start_sec)),
        "-i", video_url,
        "-t", str(int(duration_sec) + interval),  # small over-read to avoid off-by-one
        "-vf", (
            f"fps=1/{interval},"
            f"scale={_THUMB_W}:{_THUMB_H}:flags=fast_bilinear"
        ),
        "-frames:v", str(n_frames),
        "-q:v", "5",
        "-y", pattern,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_SEGMENT_TIMEOUT)
        if proc.returncode != 0:
            logger.warning(
                "[storyboard] segment %d failed (rc=%d): %s",
                seg_idx, proc.returncode,
                stderr.decode(errors="replace")[:200],
            )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("[storyboard] segment %d timed out", seg_idx)
        return 0

    # Rename s{seg}_0001.jpg … → frame_{abs_idx:04d}.jpg
    written = 0
    for local_i in range(n_frames):
        src = os.path.join(frame_dir, f"s{seg_idx}_{local_i + 1:04d}.jpg")
        dst = os.path.join(frame_dir, f"frame_{start_frame + local_i:04d}.jpg")
        if os.path.exists(src):
            os.rename(src, dst)
            written += 1
    return written


async def _ensure_black_frame(path: str) -> None:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=black:s={_THUMB_W}x{_THUMB_H}:r=1",
        "-frames:v", "1", "-q:v", "5", "-y", path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()


async def _do_generate(video_id: str) -> Optional[dict]:
    from backend.services import ytdlp_service

    raw_info = ytdlp_service.get_raw_info(video_id)
    if not raw_info:
        try:
            await ytdlp_service.extract_formats(video_id)
            raw_info = ytdlp_service.get_raw_info(video_id)
        except Exception as exc:
            logger.warning("[storyboard] yt-dlp failed for %s: %s", video_id, exc)
            return None

    if not raw_info:
        return None

    duration = int(raw_info.get("duration") or 0)
    if duration < 2:
        return None

    # Pick lowest-height video-only stream for fastest frame extraction
    formats = raw_info.get("formats") or []
    video_only = [
        f for f in formats
        if f.get("vcodec", "none") != "none"
        and f.get("acodec", "none") == "none"
        and f.get("url")
        and f.get("height")
    ]
    if not video_only:
        video_only = [
            f for f in formats
            if f.get("vcodec", "none") != "none"
            and f.get("url")
            and f.get("height")
        ]
    if not video_only:
        logger.warning("[storyboard] no usable video format for %s", video_id)
        return None

    video_only.sort(key=lambda f: f.get("height") or 9999)
    video_url = video_only[0]["url"]
    height_used = video_only[0].get("height", "?")

    interval = max(1, duration // _FRAME_COUNT)
    count = min(_FRAME_COUNT, max(1, duration // interval))

    # --- Fast path: locally downloaded LQ file ---
    frame_dir = os.path.join(_CACHE_DIR, f"frames_{video_id}")
    os.makedirs(frame_dir, exist_ok=True)

    try:
        from backend.services import lq_service
        lq_path = lq_service.get_lq_path(video_id)
        if not lq_path:
            lq_event = lq_service.get_lq_event(video_id)
            if lq_event:
                try:
                    await asyncio.wait_for(asyncio.shield(lq_event.wait()), timeout=30.0)
                except asyncio.TimeoutError:
                    pass
                lq_path = lq_service.get_lq_path(video_id)
    except Exception:
        lq_path = None

    if lq_path:
        logger.info("[storyboard] using local LQ file for %s", video_id)
        sprite = _sprite_path(video_id)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-skip_frame", "noref",
            "-i", lq_path,
            "-vf", (
                f"fps=1/{interval},"
                f"scale={_THUMB_W}:{_THUMB_H}:flags=fast_bilinear,"
                f"tile={_COLS}x{_ROWS}"
            ),
            "-frames:v", "1",
            "-q:v", "5",
            "-y", sprite,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TILE_TIMEOUT * 4)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("[storyboard] local-file extraction timed out for %s", video_id)
            # fall through to parallel HTTP path below
            lq_path = None  # signal fallthrough

        if lq_path:
            if proc.returncode != 0 or not os.path.exists(sprite):
                err = stderr.decode(errors="replace")[:300]
                logger.warning("[storyboard] local-file extraction failed for %s: %s", video_id, err)
                lq_path = None  # fall through

        if lq_path and os.path.exists(sprite):
            meta = {
                "url": f"/api/video/{video_id}/storyboards/sprite",
                "templateUrl": f"/api/video/{video_id}/storyboards/sprite",
                "width": _THUMB_W,
                "height": _THUMB_H,
                "count": count,
                "interval": interval,
                "storyboardWidth": _COLS,
                "storyboardHeight": _ROWS,
                "storyboardCount": 1,
            }
            with open(_meta_path(video_id), "w") as f:
                json.dump(meta, f)
            logger.info("[storyboard] done (local) for %s (%d frames)", video_id, count)
            shutil.rmtree(frame_dir, ignore_errors=True)
            return meta
        # else fall through to parallel HTTP path

    logger.info(
        "[storyboard] generating %s (dur=%ds interval=%ds height=%s segments=%d)",
        video_id, duration, interval, height_used, _SEGMENTS,
    )

    try:
        # Build segments: each handles a contiguous slice of frames.
        frames_per_seg = max(1, count // _SEGMENTS)
        segs = []
        for s in range(_SEGMENTS):
            start_f = s * frames_per_seg
            end_f = count if s == _SEGMENTS - 1 else (s + 1) * frames_per_seg
            n = end_f - start_f
            if n <= 0:
                continue
            start_t = start_f * interval
            seg_dur = n * interval
            segs.append((s, start_f, n, float(start_t), float(seg_dur)))

        results = await asyncio.gather(
            *[_run_segment(video_url, s, sf, n, st, sd, interval, frame_dir)
              for s, sf, n, st, sd in segs],
            return_exceptions=True,
        )

        total_written = sum(r for r in results if isinstance(r, int))
        if total_written == 0:
            logger.warning("[storyboard] no frames extracted for %s", video_id)
            return None

        # Fill any gaps so ffmpeg tiling gets a contiguous sequence
        black_path = os.path.join(frame_dir, "_black.jpg")
        black_created = False
        missing = []
        for i in range(count):
            if not os.path.exists(os.path.join(frame_dir, f"frame_{i:04d}.jpg")):
                missing.append(i)
        if missing:
            await _ensure_black_frame(black_path)
            black_created = True
            for i in missing:
                shutil.copy2(black_path, os.path.join(frame_dir, f"frame_{i:04d}.jpg"))

        sprite = _sprite_path(video_id)
        tile_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-start_number", "0",
            "-i", os.path.join(frame_dir, "frame_%04d.jpg"),
            "-vf", f"tile={_COLS}x{_ROWS}",
            "-frames:v", "1",
            "-q:v", "5",
            "-y", sprite,
        ]
        proc = await asyncio.create_subprocess_exec(
            *tile_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_TILE_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("[storyboard] tiling timed out for %s", video_id)
            return None

        if proc.returncode != 0 or not os.path.exists(sprite):
            logger.warning(
                "[storyboard] tiling failed for %s (rc=%d): %s",
                video_id, proc.returncode,
                stderr.decode(errors="replace")[:300],
            )
            return None

        meta = {
            "url": f"/api/video/{video_id}/storyboards/sprite",
            "templateUrl": f"/api/video/{video_id}/storyboards/sprite",
            "width": _THUMB_W,
            "height": _THUMB_H,
            "count": count,
            "interval": interval,
            "storyboardWidth": _COLS,
            "storyboardHeight": _ROWS,
            "storyboardCount": 1,
        }
        with open(_meta_path(video_id), "w") as f:
            json.dump(meta, f)

        logger.info(
            "[storyboard] done for %s (%d/%d frames, %d missing)",
            video_id, total_written, count, len(missing),
        )
        return meta

    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)


async def generate_bg(video_id: str) -> None:
    """Start generation in the background; idempotent if already cached/running."""
    if get_cached(video_id):
        return
    if _is_failed(video_id):
        return
    if video_id in _in_progress:
        return

    event = asyncio.Event()
    _in_progress[video_id] = event
    try:
        result = await _do_generate(video_id)
        if result is None:
            _mark_failed(video_id)
    except Exception as exc:
        logger.warning("[storyboard] generate_bg error for %s: %s", video_id, exc)
        _mark_failed(video_id)
    finally:
        _in_progress.pop(video_id, None)
        event.set()


async def get_or_wait(video_id: str, wait_secs: float = 8.0) -> Optional[dict]:
    """Return cached storyboard or wait briefly for an in-progress generation."""
    cached = get_cached(video_id)
    if cached:
        return cached

    if _is_failed(video_id):
        return None

    if video_id in _in_progress:
        event = _in_progress[video_id]
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_secs)
        except asyncio.TimeoutError:
            pass
        return get_cached(video_id)

    # Not started yet — generate synchronously (shouldn't happen if video_info triggered bg)
    event = asyncio.Event()
    _in_progress[video_id] = event
    try:
        result = await _do_generate(video_id)
        if result is None:
            _mark_failed(video_id)
        return result
    except Exception as exc:
        logger.warning("[storyboard] get_or_wait error for %s: %s", video_id, exc)
        _mark_failed(video_id)
        return None
    finally:
        _in_progress.pop(video_id, None)
        event.set()
