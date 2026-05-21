import time
import asyncio
import logging
import sys
import json
import os
from typing import Optional, Tuple
import yt_dlp

logger = logging.getLogger("ytdlp")


def _cookie_opts() -> dict:
    """Return yt-dlp cookie options from env vars, empty dict if none configured."""
    opts: dict = {}
    cookie_file = os.getenv("YTDLP_COOKIES_FILE")
    if cookie_file:
        opts["cookiefile"] = cookie_file
    browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER")
    if browser and not cookie_file:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


# ── Warm-worker pool ───────────────────────────────────────────────────────────

_WORKER_COUNT = int(os.getenv("YTDLP_WORKERS", "3"))
_WORKER_STARTUP_TIMEOUT = 20.0   # seconds to wait for "ready" on startup
_WORKER_REQUEST_TIMEOUT = 45.0   # seconds per extraction

_WORKER_SCRIPT = os.path.join(os.path.dirname(__file__), "ytdlp_worker.py")


class _Worker:
    """Wraps a single persistent ytdlp_worker.py subprocess."""

    def __init__(self, proc: asyncio.subprocess.Process):
        self._proc = proc
        self._reader: asyncio.StreamReader = proc.stdout   # type: ignore[assignment]
        self._writer: asyncio.StreamWriter = proc.stdin    # type: ignore[assignment]

    @property
    def alive(self) -> bool:
        return self._proc.returncode is None

    async def extract(self, video_id: str) -> dict:
        req = json.dumps({"video_id": video_id}) + "\n"
        self._writer.write(req.encode())
        await self._writer.drain()
        line = await asyncio.wait_for(self._reader.readline(), timeout=_WORKER_REQUEST_TIMEOUT)
        if not line:
            raise RuntimeError("worker closed stdout unexpectedly")
        return json.loads(line.decode())

    def kill(self):
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass


async def _spawn_worker() -> Optional[_Worker]:
    """Spawn one worker process and wait for its READY signal."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, _WORKER_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Wait for the READY line.
        ready_line = await asyncio.wait_for(proc.stdout.readline(), timeout=_WORKER_STARTUP_TIMEOUT)  # type: ignore[union-attr]
        msg = json.loads(ready_line.decode())
        if not msg.get("ready"):
            proc.kill()
            return None
        return _Worker(proc)
    except Exception as exc:
        logger.warning("[worker] spawn failed: %s", exc)
        return None


class _WorkerPool:
    """
    Pool of N persistent yt-dlp worker subprocesses.

    Workers stay alive between requests so yt-dlp's internal HTTP session and
    extractor cache are reused across calls, cutting per-request overhead.
    """

    def __init__(self, size: int = _WORKER_COUNT):
        self._size = size
        self._idle: asyncio.Queue[_Worker] = asyncio.Queue()
        self._started = False

    async def start(self):
        if self._started:
            return
        self._started = True
        tasks = [asyncio.create_task(_spawn_worker()) for _ in range(self._size)]
        workers = await asyncio.gather(*tasks)
        ready = 0
        for w in workers:
            if w is not None:
                await self._idle.put(w)
                ready += 1
        logger.info("[worker-pool] started %d/%d workers", ready, self._size)

    async def extract(self, video_id: str) -> dict:
        """
        Borrow an idle worker, run the extraction, return it to the pool.
        Auto-restarts the worker if it died.  Raises on extraction error.
        """
        worker = await self._idle.get()
        ok = False
        try:
            if not worker.alive:
                worker.kill()
                worker = await _spawn_worker()
                if worker is None:
                    raise RuntimeError("failed to respawn worker")

            result = await worker.extract(video_id)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "unknown yt-dlp error"))

            ok = True
            return result["info"]
        finally:
            if ok and worker.alive:
                await self._idle.put(worker)
            else:
                # Worker may have bad state — kill and respawn.
                worker.kill()
                new = await _spawn_worker()
                if new is not None:
                    await self._idle.put(new)


_pool = _WorkerPool()


async def start_worker_pool():
    """Called once from the FastAPI lifespan to pre-warm the workers."""
    await _pool.start()


# ── End warm-worker pool ───────────────────────────────────────────────────────

CACHE_TTL = int(os.getenv("YTDLP_CACHE_TTL", 1800))
DOWNLOAD_DIR = os.getenv("YTDLP_DOWNLOAD_DIR", "/opt/ytfrontend/data/downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# {video_id: (timestamp, {format_id: url})}
_url_cache: dict[str, tuple[float, dict]] = {}

# {video_id: (timestamp, raw_formats_list)}
_raw_cache: dict[str, tuple[float, list]] = {}

# {video_id: (timestamp, {"subtitles": {lang: [tracks]}, "auto": {lang: [tracks]}})}
_subtitle_cache: dict[str, tuple[float, dict]] = {}

# {video_id: (timestamp, full yt-dlp info dict)}
_info_cache: dict[str, tuple[float, dict]] = {}

# Track in-progress extractions to avoid parallel duplicates
_in_progress: dict[str, asyncio.Lock] = {}

# Download state: {video_id: {"status": "none"|"downloading"|"done"|"failed", "progress": int, "path": str|None}}
_download_state: dict[str, dict] = {}
_download_in_progress: set[str] = set()
_cancelled: set[str] = set()


def _cache_fresh(video_id: str) -> bool:
    # Check _raw_cache (yt-dlp specific), not _url_cache which also stores invidious URLs
    entry = _raw_cache.get(video_id)
    return entry is not None and (time.time() - entry[0]) < CACHE_TTL


def _extract(video_id: str) -> dict:
    logger.info(f"[extract] starting yt-dlp extraction for {video_id}")
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "js_runtimes": {"node": {}},
            **_cookie_opts(),
        }) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            fmts = info.get("formats", [])
            video_only = [f for f in fmts if f.get("vcodec", "none") != "none" and f.get("acodec", "none") == "none" and f.get("height")]
            audio_only = [f for f in fmts if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"]
            combined = [f for f in fmts if f.get("vcodec", "none") != "none" and f.get("acodec", "none") != "none"]
            urls = sum(1 for f in fmts if f.get("url"))
            logger.info(f"[extract] {video_id}: {len(fmts)} formats ({len(video_only)} video-only, {len(audio_only)} audio-only, {len(combined)} combined, {urls} with URLs)")
            return info
    except Exception as e:
        logger.error(f"[extract] {video_id} FAILED: {type(e).__name__}: {e}")
        raise


async def extract_formats(video_id: str) -> list[dict]:
    """Extract via yt-dlp. Deduplicates concurrent calls. Caches results."""
    if video_id not in _in_progress:
        _in_progress[video_id] = asyncio.Lock()

    async with _in_progress[video_id]:
        if _cache_fresh(video_id):
            return _build_format_list(video_id)

        if _pool._started:
            # Use a pre-warmed worker subprocess; blocks until one is idle.
            try:
                info = await _pool.extract(video_id)
                _store_info(video_id, info)
                return _build_format_list(video_id)
            except Exception as exc:
                logger.warning("[extract] worker pool failed (%s), falling back to thread executor", exc)

        # Fallback: run in the thread pool (used before pool is ready or on pool error).
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _extract, video_id)
        _store_info(video_id, info)

    return _build_format_list(video_id)


def _store_info(video_id: str, info: dict):
    url_map = _url_cache.get(video_id, (time.time(), {}))[1].copy()
    raw_formats = []

    for f in info.get("formats", []):
        if f.get("url"):
            raw_formats.append(f)
            fid = f"ytdlp_{f['format_id']}"
            url_map[fid] = f["url"]

    _url_cache[video_id] = (time.time(), url_map)
    _raw_cache[video_id] = (time.time(), raw_formats)
    _subtitle_cache[video_id] = (time.time(), {
        "subtitles": info.get("subtitles", {}),
        "auto": info.get("automatic_captions", {}),
    })
    _info_cache[video_id] = (time.time(), info)


def _build_format_list(video_id: str) -> list[dict]:
    entry = _raw_cache.get(video_id)
    if not entry:
        return []

    formats = []
    seen = set()
    for f in entry[1]:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")
        proto = f.get("protocol", "https")
        if vcodec == "none" or not height:
            continue
        # HLS manifests can't be byte-range proxied; the mux endpoint handles
        # those quality levels via ffmpeg. Exclude them from the format list.
        if "m3u8" in proto:
            continue
        has_audio = acodec != "none"
        key = (height, has_audio)
        if key in seen:
            continue
        seen.add(key)
        formats.append({
            "format_id": f"ytdlp_{f['format_id']}",
            "height": height,
            "fps": f.get("fps"),
            "vcodec": vcodec,
            "has_audio": has_audio,
            "source": "ytdlp",
        })

    return sorted(formats, key=lambda x: -x["height"])


def store_url(video_id: str, format_id: str, url: str):
    ts, m = _url_cache.get(video_id, (time.time(), {}))
    _url_cache[video_id] = (ts, {**m, format_id: url})


def invalidate(video_id: str):
    """Force-expire cached URLs so next extract_formats call re-fetches from YouTube."""
    _url_cache.pop(video_id, None)
    _raw_cache.pop(video_id, None)
    _subtitle_cache.pop(video_id, None)
    _info_cache.pop(video_id, None)


def get_url(video_id: str, format_id: str) -> Optional[str]:
    entry = _url_cache.get(video_id)
    if not entry:
        return None
    return entry[1].get(format_id)


def get_subtitle_url(video_id: str, lang: str, is_auto: bool) -> Optional[str]:
    """Return a direct VTT URL for the given language from yt-dlp's extracted subtitle data."""
    entry = _subtitle_cache.get(video_id)
    if not entry:
        return None
    pool = entry[1]["auto" if is_auto else "subtitles"]
    # Try exact lang code, then base language (e.g. "de-DE" → "de")
    candidates = pool.get(lang) or pool.get(lang.split("-")[0]) or []
    for track in candidates:
        if track.get("ext") == "vtt":
            return track.get("url")
    # Fallback: any format (browser can handle json3 too if needed, but prefer vtt)
    return candidates[0].get("url") if candidates else None



def get_raw_info(video_id: str) -> Optional[dict]:
    entry = _info_cache.get(video_id)
    if not entry:
        return None
    return entry[1]

def get_status(video_id: str) -> dict:
    """Non-blocking status check."""
    ready = _cache_fresh(video_id)
    if not ready:
        return {"ready": False, "can_mux": False, "has_combined": False}
    v, a, _, _ = get_mux_urls(video_id)
    combined = _get_best_combined(video_id)
    return {
        "ready": True,
        "can_mux": bool(v and a),
        "has_combined": bool(combined),
    }


def _get_best_combined(video_id: str, max_height: int = 9999) -> Optional[str]:
    """Best yt-dlp combined (video+audio) format at or below max_height.
    Prefers mp4 (avc1) for broadest browser compatibility."""
    entry = _raw_cache.get(video_id)
    if not entry:
        return None
    candidates = [
        f for f in entry[1]
        if f.get("vcodec", "none") != "none"
        and f.get("acodec", "none") != "none"
        and f.get("height")
        and f.get("url")
        and (f.get("height") or 0) <= max_height
        # exclude HLS/DASH manifests — only direct HTTP streams work in browsers
        and f.get("protocol", "https") in ("https", "http", "")
        and f.get("ext", "") not in ("m3u8", "mpd")
    ]
    if not candidates:
        return None
    def sort_key(f):
        vc = f.get("vcodec", "")
        is_h264 = vc.startswith("avc")
        return (not is_h264, -(f.get("height") or 0))
    candidates.sort(key=sort_key)
    return f"ytdlp_{candidates[0]['format_id']}"


def get_mux_urls(video_id: str, target_height: int = 720) -> Tuple[Optional[str], Optional[str], str, str]:
    """Get best video-only and audio-only URLs for ffmpeg muxing."""
    entry = _raw_cache.get(video_id)
    if not entry:
        return None, None, "", ""

    raw_formats = entry[1]

    video_candidates = [
        f for f in raw_formats
        if f.get("vcodec", "none") != "none"
        and f.get("acodec", "none") == "none"
        and f.get("height")
        and f.get("url")
    ]

    def video_sort_key(f):
        h = f.get("height", 0)
        vc = f.get("vcodec", "")
        # Prefer VP9 — open codec, works in all browsers including LibreWolf
        is_vp9 = vc.startswith("vp9") or vc.startswith("vp09")
        at_or_below = h <= target_height
        return (not at_or_below, -h if at_or_below else h, not is_vp9)

    video_candidates.sort(key=video_sort_key)
    video_url = video_candidates[0]["url"] if video_candidates else None
    vcodec = video_candidates[0].get("vcodec", "") if video_candidates else ""
    is_avc = vcodec.startswith("avc")

    audio_candidates = [
        f for f in raw_formats
        if f.get("vcodec", "none") == "none"
        and f.get("acodec", "none") != "none"
        and f.get("url")
    ]

    def audio_sort_key(f):
        ac = f.get("acodec", "")
        abr = f.get("abr") or f.get("tbr") or 0
        if is_avc:
            # AVC video → prefer AAC so both fit natively in fMP4
            is_preferred = ac.startswith("mp4a") or "aac" in ac.lower()
        else:
            # VP9/AV1 → prefer Opus for WebM container
            is_preferred = ac.startswith("opus")
        return (not is_preferred, -abr)

    audio_candidates.sort(key=audio_sort_key)
    audio_url = audio_candidates[0]["url"] if audio_candidates else None
    acodec = audio_candidates[0].get("acodec", "") if audio_candidates else ""

    if video_url and audio_url:
        logger.info(f"[mux] picked video: {video_candidates[0].get('format_id')} {video_candidates[0].get('height')}p {vcodec}")
        logger.info(f"[mux] picked audio: {audio_candidates[0].get('format_id')} {acodec} {audio_candidates[0].get('abr')}kbps")

    return video_url, audio_url, vcodec, acodec


# ── Background download ────────────────────────────────────────────────────────

def get_download_state(video_id: str) -> dict:
    state = _download_state.get(video_id)
    if state:
        path = state.get("path")
        if state.get("status") != "done" or not path or os.path.exists(path):
            return state
        _download_state.pop(video_id, None)

    path = _find_downloaded_file(video_id)
    if path:
        ext = os.path.splitext(path)[1].lower()
        mode = "audio" if ext in (".m4a", ".mp3", ".opus", ".ogg", ".aac", ".wav") else "video"
        restored = {
            "status": "done",
            "progress": 100,
            "path": path,
            "eta": None,
            "phase": "done",
            "mode": mode,
        }
        _download_state[video_id] = restored
        return restored

    return {"status": "none", "progress": 0, "path": None, "eta": None, "phase": None, "mode": "video"}


def _find_downloaded_file(video_id: str) -> Optional[str]:
    for ext in ("webm", "mp4", "mkv", "m4a", "mp3", "opus", "ogg", "aac", "wav", "flac"):
        p = os.path.join(DOWNLOAD_DIR, f"{video_id}.{ext}")
        if os.path.exists(p):
            return p
    return None


def _do_download(video_id: str, mode: str):
    """Blocking yt-dlp download — runs in a thread pool executor."""
    state = _download_state[video_id]
    # Track file index so progress accumulates (0→45% per file, up to 90%).
    file_index = [0]
    phase_names = ["audio", "metadata"] if mode == "audio" else ["video", "audio", "merging"]

    def progress_hook(d):
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        idx = file_index[0]
        if d["status"] == "downloading" and total > 0:
            file_pct = done / total
            base = idx * 45
            state["progress"] = min(90, int(base + file_pct * 45))
            state["eta"] = d.get("eta")
            state["phase"] = phase_names[min(idx, len(phase_names) - 1)]
        elif d["status"] == "finished":
            file_index[0] += 1
            state["progress"] = min(90, file_index[0] * 45)
            state["eta"] = None
            state["phase"] = phase_names[min(file_index[0], len(phase_names) - 1)]

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{video_id}.%(ext)s"),
        "progress_hooks": [progress_hook],
        "js_runtimes": {"node": {}},
    }
    if mode == "audio":
        ydl_opts["format"] = "bestaudio/best"
    else:
        ydl_opts["format"] = (
            "bestvideo[ext=webm][height<=720]+bestaudio[ext=webm]"
            "/bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]"
            "/best[ext=webm][height<=720]"
            "/best[ext=mp4][height<=720]"
            "/best[height<=720]"
        )
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])


async def start_download(video_id: str, mode: str = "video") -> None:
    """Fire-and-forget: start background download if not already running or done."""
    state = _download_state.get(video_id, {})
    state_mode = state.get("mode") or "video"
    if state.get("status") in ("downloading", "done") and state_mode == mode:
        return
    if state.get("status") in ("downloading", "done") and state_mode != mode:
        delete_download(video_id)
    if video_id in _download_in_progress:
        return

    # Already on disk from a previous session?
    path = _find_downloaded_file(video_id)
    if path:
        _download_state[video_id] = {"status": "done", "progress": 100, "path": path, "mode": mode, "eta": None, "phase": "done"}
        return

    _download_in_progress.add(video_id)
    initial_phase = "audio" if mode == "audio" else "video"
    _download_state[video_id] = {
        "status": "downloading",
        "progress": 0,
        "path": None,
        "eta": None,
        "phase": initial_phase,
        "mode": mode,
    }
    asyncio.create_task(_download_task(video_id, mode))


async def _bandcamp_audio_fallback(video_id: str) -> Optional[str]:
    """After yt-dlp fails, try [bandcamp-dl] against a Bandcamp match for the same metadata."""
    try:
        from backend.services.invidious_client import api_get
        from backend.services.music_client import bandcamp_lookup
        from backend.services.bandcamp_download import download_bandcamp_release_sync

        data = await api_get(f"/videos/{video_id}")
    except Exception as exc:
        logger.warning("[download] bandcamp fallback: no Invidious metadata: %s", exc)
        return None

    title = (data.get("title") or "").strip()
    author = (data.get("author") or "").strip()
    query = f"{author} {title}".strip()
    if len(query) < 3:
        return None

    try:
        best = await bandcamp_lookup(
            query, track="", artist=author, title=title, author=author, limit=4
        )
    except Exception as exc:
        logger.warning("[download] bandcamp fallback: Bandcamp lookup failed: %s", exc)
        return None

    url = ((best or {}).get("url") or "").strip()
    if not url:
        return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, download_bandcamp_release_sync, url, video_id, DOWNLOAD_DIR
    )


async def _download_task(video_id: str, mode: str):
    loop = asyncio.get_event_loop()
    try:
        logger.info(f"[download] {video_id} starting ({mode})")
        await loop.run_in_executor(None, _do_download, video_id, mode)
        # If cancelled while downloading, clean up whatever yt-dlp wrote and bail
        if video_id in _cancelled:
            _purge_files(video_id)
            return
        path = _find_downloaded_file(video_id)
        if path:
            _download_state[video_id] = {"status": "done", "progress": 100, "path": path, "mode": mode, "eta": None, "phase": "done"}
            logger.info(f"[download] {video_id} complete → {path}")
            enforce_disk_quota()
        else:
            raise FileNotFoundError("Output file not found after download")
    except Exception as e:
        logger.error(f"[download] {video_id} failed: {e}")
        recovered: Optional[str] = None
        if mode == "audio" and video_id not in _cancelled:
            try:
                recovered = await _bandcamp_audio_fallback(video_id)
            except Exception as fb_exc:
                logger.warning("[download] bandcamp fallback raised: %s", fb_exc)
        if recovered:
            _download_state[video_id] = {
                "status": "done",
                "progress": 100,
                "path": recovered,
                "mode": mode,
                "eta": None,
                "phase": "done",
                "source": "bandcamp",
            }
            logger.info("[download] %s recovered via bandcamp-dl → %s", video_id, recovered)
            enforce_disk_quota()
        else:
            _download_state[video_id] = {
                "status": "failed",
                "progress": 0,
                "path": None,
                "error": str(e),
                "mode": mode,
                "eta": None,
                "phase": None,
            }
    finally:
        _download_in_progress.discard(video_id)
        _cancelled.discard(video_id)


# ── Cleanup / disk quota ───────────────────────────────────────────────────────

MAX_DOWNLOAD_FILES = int(os.getenv("YTDLP_MAX_FILES", 3))
MAX_DOWNLOAD_BYTES = int(os.getenv("YTDLP_MAX_BYTES", str(2 * 1024 ** 3)))  # 2 GB


def _purge_files(video_id: str):
    """Delete all files in DOWNLOAD_DIR that start with video_id."""
    try:
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.startswith(video_id + ".") or fname == video_id:
                p = os.path.join(DOWNLOAD_DIR, fname)
                try:
                    os.remove(p)
                    logger.info(f"[cleanup] deleted {p}")
                except OSError as e:
                    logger.warning(f"[cleanup] could not delete {p}: {e}")
    except OSError as e:
        logger.warning(f"[cleanup] listdir failed: {e}")


def delete_download(video_id: str) -> bool:
    """Delete a downloaded file and reset state. Safe to call at any time."""
    _cancelled.add(video_id)
    _download_in_progress.discard(video_id)
    _download_state.pop(video_id, None)
    existed = bool(_find_downloaded_file(video_id))
    _purge_files(video_id)
    return existed


def cleanup_all_downloads():
    """Wipe the entire downloads directory (called on startup to clear stale files)."""
    _download_state.clear()
    _download_in_progress.clear()
    _cancelled.clear()
    try:
        removed = 0
        for fname in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, fname)
            try:
                os.remove(p)
                removed += 1
            except OSError as e:
                logger.warning(f"[cleanup] startup: could not delete {p}: {e}")
        if removed:
            logger.info(f"[cleanup] startup: removed {removed} stale file(s)")
    except OSError as e:
        logger.warning(f"[cleanup] startup scan failed: {e}")


def enforce_disk_quota(max_files: int = MAX_DOWNLOAD_FILES, max_bytes: int = MAX_DOWNLOAD_BYTES):
    """Delete oldest completed downloads when over the file count or byte limit."""
    try:
        entries = []
        for fname in os.listdir(DOWNLOAD_DIR):
            if fname.endswith((".webm", ".mp4", ".mkv", ".m4a", ".mp3", ".opus", ".flac")):
                p = os.path.join(DOWNLOAD_DIR, fname)
                entries.append((os.path.getmtime(p), os.path.getsize(p), p, fname.split(".")[0]))
        entries.sort()  # oldest first

        total = sum(sz for _, sz, _, _ in entries)
        while entries and (len(entries) > max_files or total > max_bytes):
            mtime, sz, p, vid = entries.pop(0)
            try:
                os.remove(p)
                total -= sz
                _download_state.pop(vid, None)
                logger.info(f"[quota] evicted {p} ({sz // 1024 // 1024} MB)")
            except OSError as e:
                logger.warning(f"[quota] could not evict {p}: {e}")
    except OSError as e:
        logger.warning(f"[quota] scan failed: {e}")
