"""Optional [bandcamp-dl](https://github.com/Evolution0/bandcamp-dl) integration for local audio files.

Used only after a YouTube/yt-dlp download attempt fails (see ``ytdlp_service``).
Install CLI via PyPI package ``bandcamp-downloader`` (``bandcamp-dl`` on PATH)
or set ``BANDCAMP_DL_BIN`` to the executable.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("bandcamp_download")


def download_bandcamp_release_sync(
    bandcamp_url: str,
    video_id: str,
    download_dir: str,
) -> str | None:
    """
    Run ``bandcamp-dl`` into a temp folder, then move a single audio file next to
    yt-dlp outputs: ``{download_dir}/{video_id}.<ext>`` (same layout as YouTube downloads).
    """
    exe = os.getenv("BANDCAMP_DL_BIN", "bandcamp-dl")
    job_dir = os.path.join(download_dir, f"_bcjob_{video_id}")
    shutil.rmtree(job_dir, ignore_errors=True)
    os.makedirs(job_dir, exist_ok=True)
    # Let bandcamp-dl use its default folder layout under job_dir; pick the largest
    # audio file afterward (single-track URLs yield one file; albums yield many).
    cmd = [
        exe,
        "--no-confirm",
        bandcamp_url,
        "--base-dir",
        job_dir,
    ]
    timeout = int(os.getenv("BANDCAMP_DL_TIMEOUT", "600"))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=download_dir,
        )
    except FileNotFoundError:
        logger.warning("bandcamp-dl not found (%s); install bandcamp-downloader", exe)
        shutil.rmtree(job_dir, ignore_errors=True)
        return None
    except subprocess.TimeoutExpired:
        logger.error("bandcamp-dl timed out for %s", video_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        return None

    if proc.returncode != 0:
        logger.warning(
            "bandcamp-dl failed rc=%s stderr=%s",
            proc.returncode,
            (proc.stderr or "")[:500],
        )
        shutil.rmtree(job_dir, ignore_errors=True)
        return None

    audios: list[Path] = []
    for ext in (".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav"):
        audios.extend(Path(job_dir).rglob(f"*{ext}"))
    if not audios:
        shutil.rmtree(job_dir, ignore_errors=True)
        return None
    audio = max(audios, key=lambda p: p.stat().st_size)

    dest = os.path.join(download_dir, f"{video_id}{audio.suffix.lower()}")
    try:
        shutil.move(str(audio), dest)
    except OSError as exc:
        logger.error("could not move bandcamp download: %s", exc)
        shutil.rmtree(job_dir, ignore_errors=True)
        return None
    shutil.rmtree(job_dir, ignore_errors=True)
    logger.info("bandcamp-dl saved %s", dest)
    return dest
