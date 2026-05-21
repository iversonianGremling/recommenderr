"""
Pre-warmed yt-dlp worker process.

Keeps one YoutubeDL instance alive between extractions so the HTTP session,
cookies, and yt-dlp extractor cache are reused.  The parent process communicates
via newline-delimited JSON on stdin/stdout.

  stdin  ← {"video_id": "<id>"}\n
  stdout → {"ok": true, "info": {...}}\n
          {"ok": false, "error": "...", "type": "<ExcClass>"}\n

On startup it writes {"ready": true}\n so the parent knows imports finished.
"""
import sys
import json
import logging
import yt_dlp

# Silence yt-dlp's logger — the parent process does its own logging.
logging.disable(logging.CRITICAL)

_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
}


def _make_ydl() -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL(_YDL_OPTS)


def _to_json_safe(obj):
    """Recursively strip any non-JSON-serialisable values."""
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    return str(obj)


ydl = _make_ydl()

# Signal that imports and YDL init are done.
sys.stdout.write(json.dumps({"ready": True}) + "\n")
sys.stdout.flush()

for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue

    video_id = ""
    try:
        req = json.loads(raw)
        video_id = req.get("video_id", "")
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}",
            download=False,
        )
        out = {"ok": True, "info": _to_json_safe(info)}
    except Exception as exc:
        # Reset the YDL instance so stale state from the failed call doesn't
        # contaminate the next request.
        try:
            ydl = _make_ydl()
        except Exception:
            pass
        out = {"ok": False, "error": str(exc), "type": type(exc).__name__}

    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
