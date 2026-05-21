import asyncio
import logging
import math
import os
import re
from difflib import SequenceMatcher

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.services import music_worker
from backend.services.artist_release_worker import check_followed_artist, check_followed_artists_once
from backend.db import (
    delete_artist_follow,
    delete_music_library_item,
    get_album_rating,
    get_artist_follow,
    get_db,
    get_music_library_genres_for_video_ids,
    get_music_tags_for_video_ids,
    get_playlists_for_video_ids,
    get_ratings_for_video_ids,
    list_artist_follows,
    list_artist_release_events,
    normalize_album_key,
    save_artist_follow,
    set_music_library_genre,
    sync_artist_follows_from_album_ratings,
)
from backend.services.bandcamp_recommendations import (
    bandcamp_sidebar_to_music_recommendation_rows,
    get_shared_bandcamp_recommender,
)
from backend.services.invidious_client import api_get
from backend.services.music_client import (
    bandcamp_album_details,
    bandcamp_search_albums,
    bandcamp_lookup,
    deezer_get_album_tracks,
    deezer_get_artist_albums,
    deezer_get_related_artists,
    deezer_search,
    deezer_search_album,
    deezer_search_artist,
    itunes_search,
    itunes_search_album,
    itunes_search_artist,
    spotify_search,
    spotify_get_album_tracks,
    spotify_get_artist_albums,
    spotify_search_album,
    spotify_search_artist,
)
from backend.services.music_recognition import quick_recognize, recognize
from backend.services.music_recommendations import (
    dedupe_music_recommendation_rows,
    get_playlist_aggregate_recommendations,
    get_recommendations,
    get_same_artist_catalog_tracks,
    resolve_bandcamp_recommendation_row,
)

logger = logging.getLogger("music")
from backend.services.music_tags import (
    create_music_tag_group,
    create_music_tag,
    delete_music_tag,
    get_music_tag_descendant_ids,
    list_music_tag_groups,
    list_music_tags,
    manual_assign_music_tags,
    manual_upsert_music_library_row,
    merge_music_tag,
    move_music_tag,
    rename_music_tag,
    sync_tag_playlist_content,
    update_music_tag_group,
)

router = APIRouter()

INVIDIOUS_URL = os.getenv("INVIDIOUS_URL", "http://192.168.1.173:3000")
ALBUM_HINT_RE = re.compile(
    r"\b(full album|album|ep|lp|ost|soundtrack|official audios?|official videos?)\b",
    re.IGNORECASE,
)
VERSION_LABEL_RE = re.compile(
    r"\b(japan(?:ese)?|deluxe|expanded|bonus|collector'?s?|special edition|anniversary|remaster(?:ed)?|mono|stereo|instrumental|karaoke|live)\b",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(19|20)\d{2}")
FULL_ALBUM_VIDEO_RE = re.compile(
    r"\b(full album|complete album|album stream|full ep|full lp|full soundtrack|full ost)\b",
    re.IGNORECASE,
)
VIDEO_REJECTION_RE = re.compile(
    r"\b(review|reaction|ranking|explained|interview|podcast|analysis)\b",
    re.IGNORECASE,
)
COMPILATION_TITLE_RE = re.compile(
    r"\b(best of|greatest hits|anthology|collection|box set|rarities|singles|essentials)\b",
    re.IGNORECASE,
)
LIVE_RELEASE_RE = re.compile(
    r"\b(live|unplugged|paramount|reading|acoustic)\b",
    re.IGNORECASE,
)
VARIANT_TAIL_RE = re.compile(
    r"\b(deluxe|expanded|bonus|collector'?s?|special|edition|anniversary|remaster(?:ed)?|mono|stereo|instrumental|karaoke|live|acoustic|super|reissue|version|disc|demo|session|volume|vol|part|pt)\b",
    re.IGNORECASE,
)
TRACK_CANONICAL_RE = re.compile(
    r"\b(official (?:music )?video|official audio|lyrics?|lyric video|visualizer|audio|topic)\b",
    re.IGNORECASE,
)
TRACK_PREFIX_RE = re.compile(
    r"^\s*(?:disc\s*\d+\s*[-:]\s*)?(?:track\s*)?\d+\s*[-:.)]\s*",
    re.IGNORECASE,
)
TRACK_FEATURE_RE = re.compile(r"\b(?:ft|feat|featuring)\b.*$", re.IGNORECASE)
ARTIST_MATCH_THRESHOLD = 0.82
PLAYLIST_MATCH_THRESHOLD = 0.42
PLAYLIST_CONFIDENT_THRESHOLD = 0.55
VIDEO_MATCH_THRESHOLD = 0.72
PLAYLIST_INTEGRITY_THRESHOLD = 0.54
PLAYLIST_INTEGRITY_TITLE_THRESHOLD = 0.42
_MUSIC_LIBRARY_ENRICH_CONCURRENCY = 6
_music_library_enrich_sem = asyncio.Semaphore(_MUSIC_LIBRARY_ENRICH_CONCURRENCY)


def _normalize_thumb_url(url: str | None) -> str | None:
    if not url:
        return None
    return f"{INVIDIOUS_URL}{url}" if url.startswith("/") else url


def _needs_music_library_enrichment(row: dict) -> bool:
    title = (row.get("title") or "").strip()
    return not title or title == row.get("video_id")


async def _fetch_music_library_meta(video_id: str, retries: int = 2) -> dict | None:
    async with _music_library_enrich_sem:
        for attempt in range(retries):
            try:
                data = await api_get(
                    f"/videos/{video_id}",
                    {"fields": "title,videoThumbnails,lengthSeconds,author,authorId"},
                )
                thumbs = data.get("videoThumbnails") or []
                thumb = None
                for item in thumbs:
                    if item.get("quality") in {"medium", "default", "high"}:
                        thumb = item.get("url")
                        break
                if thumb is None and thumbs:
                    thumb = thumbs[0].get("url")
                title = (data.get("title") or "").strip()
                if not title or title == video_id:
                    return None
                return {
                    "title": title,
                    "thumbnail": _normalize_thumb_url(thumb),
                    "duration": data.get("lengthSeconds"),
                    "author": (data.get("author") or "").strip(),
                    "author_id": (data.get("authorId") or "").strip(),
                }
            except Exception:
                if attempt < retries - 1:
                    await asyncio.sleep(0.5)
        return None


async def _enrich_music_library_rows(rows: list) -> list[dict]:
    items = [dict(row) for row in rows]
    needs_enrichment = [item for item in items if _needs_music_library_enrichment(item)]
    if not needs_enrichment:
        return items

    results = await asyncio.gather(
        *[_fetch_music_library_meta(item["video_id"]) for item in needs_enrichment],
        return_exceptions=True,
    )

    updates: dict[str, dict] = {}
    conn = get_db()
    try:
        for item, meta in zip(needs_enrichment, results):
            if isinstance(meta, Exception) or not meta or not meta.get("title"):
                continue
            conn.execute(
                """
                UPDATE music_library
                SET
                    title=COALESCE(NULLIF(?, ''), title),
                    thumbnail=COALESCE(?, thumbnail),
                    duration=COALESCE(?, duration),
                    author=COALESCE(NULLIF(?, ''), author),
                    author_id=COALESCE(NULLIF(?, ''), author_id)
                WHERE video_id=?
                """,
                (
                    meta["title"],
                    meta.get("thumbnail"),
                    meta.get("duration"),
                    meta.get("author"),
                    meta.get("author_id"),
                    item["video_id"],
                ),
            )
            conn.execute(
                """
                UPDATE playlist_videos
                SET
                    title=COALESCE(NULLIF(?, ''), title),
                    thumbnail=COALESCE(?, thumbnail),
                    duration=COALESCE(?, duration),
                    author=COALESCE(NULLIF(?, ''), author),
                    author_id=COALESCE(NULLIF(?, ''), author_id)
                WHERE video_id=?
                  AND (title IS NULL OR title='' OR title=video_id)
                """,
                (
                    meta["title"],
                    meta.get("thumbnail"),
                    meta.get("duration"),
                    meta.get("author"),
                    meta.get("author_id"),
                    item["video_id"],
                ),
            )
            updates[item["video_id"]] = meta
        conn.commit()
    finally:
        conn.close()

    for item in items:
        meta = updates.get(item["video_id"])
        if not meta:
            continue
        item["title"] = meta["title"]
        if meta.get("thumbnail"):
            item["thumbnail"] = meta["thumbnail"]
        if meta.get("duration") is not None:
            item["duration"] = meta["duration"]
        if meta.get("author"):
            item["author"] = meta["author"]
        if meta.get("author_id"):
            item["author_id"] = meta["author_id"]

    return items
PLAYLIST_INTEGRITY_COUNT_THRESHOLD = 0.68
PLAYLIST_INTEGRITY_DURATION_THRESHOLD = 0.35
PLAYLIST_DURATION_MIN_RATIO = 0.62
PLAYLIST_DURATION_MAX_RATIO = 1.38
TRACK_TITLE_MATCH_THRESHOLD = 0.62
UNAVAILABLE_VIDEO_RE = re.compile(r"\b(private|deleted|unavailable)\s+video\b", re.IGNORECASE)
LRCLIB_TIME_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{2,3}))?\]")


def _clean_music_hint(value: str | None) -> str:
    if not value:
        return ""
    cleaned = TRACK_PREFIX_RE.sub("", value.strip())
    cleaned = TRACK_FEATURE_RE.sub("", cleaned)
    cleaned = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned


def _music_identity_from_row(row: dict | None) -> tuple[str, str, str, str, str]:
    if not row:
        return "", "", "", "", ""
    track = _clean_music_hint(row.get("track") or row.get("title"))
    artist = _clean_music_hint(row.get("artist") or row.get("author"))
    album = _clean_music_hint(row.get("album"))
    title = (row.get("title") or "").strip()
    author = (row.get("author") or "").strip()
    return track, artist, album, title, author


def _lookup_music_identity(video_id: str) -> tuple[str, str, str, str, str]:
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT
                ml.track,
                ml.artist,
                ml.album,
                COALESCE(ml.title, wh.title) AS title,
                COALESCE(ml.author, wh.author) AS author
            FROM music_library ml
            LEFT JOIN watch_history wh ON wh.video_id = ml.video_id
            WHERE ml.video_id = ?
            LIMIT 1
            """,
            (video_id,),
        ).fetchone()
        if row:
            return _music_identity_from_row(dict(row))

        row = conn.execute(
            """
            SELECT
                COALESCE(ml.track, wh.title) AS track,
                COALESCE(ml.artist, at.album_artist, wh.author) AS artist,
                COALESCE(ml.album, at.album_title) AS album,
                wh.title AS title,
                wh.author AS author
            FROM watch_history wh
            LEFT JOIN music_library ml ON ml.video_id = wh.video_id
            LEFT JOIN album_tracks at ON at.video_id = wh.video_id
            WHERE wh.video_id = ?
            LIMIT 1
            """,
            (video_id,),
        ).fetchone()
        if row:
            return _music_identity_from_row(dict(row))
    finally:
        conn.close()
    return "", "", "", "", ""


async def _lookup_music_identity_from_api(video_id: str) -> tuple[str, str, str, str, str]:
    try:
        data = await api_get(f"/videos/{video_id}")
    except Exception:
        return "", "", "", "", ""
    return (
        _clean_music_hint(data.get("track") or data.get("song") or data.get("title")),
        _clean_music_hint(data.get("artist") or data.get("author")),
        _clean_music_hint(data.get("album")),
        (data.get("title") or "").strip(),
        (data.get("author") or "").strip(),
    )


def _fallback_query(track: str, artist: str, title: str, author: str) -> str:
    if track and artist and not UNAVAILABLE_VIDEO_RE.search(track):
        return f"{artist} {track}".strip()
    if track and not UNAVAILABLE_VIDEO_RE.search(track):
        return track
    if title and artist and not UNAVAILABLE_VIDEO_RE.search(title):
        return f"{artist} {title}".strip()
    if title and author and not UNAVAILABLE_VIDEO_RE.search(title):
        return f"{author} {title}".strip()
    if title and not UNAVAILABLE_VIDEO_RE.search(title):
        return title
    return ""


def _parse_synced_lyrics(text: str | None) -> list[dict]:
    if not text:
        return []

    lines: list[dict] = []
    for raw_line in text.splitlines():
        matches = list(LRCLIB_TIME_RE.finditer(raw_line))
        if not matches:
            continue
        lyric_text = LRCLIB_TIME_RE.sub("", raw_line).strip()
        if not lyric_text:
            continue
        for match in matches:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction = match.group(3) or "0"
            milliseconds = int(fraction.ljust(3, "0")[:3])
            lines.append({
                "start": (minutes * 60) + seconds + (milliseconds / 1000.0),
                "text": lyric_text,
            })

    lines.sort(key=lambda item: item["start"])
    return lines


async def _fetch_lrclib_lyrics(track: str, artist: str, album: str = "") -> dict | None:
    if not track and not artist:
        return None

    params = {
        "track_name": track,
        "artist_name": artist,
    }
    if album:
        params["album_name"] = album

    headers = {"User-Agent": "YTFrontend/1.0"}
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.get("https://lrclib.net/api/get", params=params, headers=headers)
            if response.status_code == 404:
                response = await client.get("https://lrclib.net/api/search", params=params, headers=headers)
                response.raise_for_status()
                items = response.json() if isinstance(response.json(), list) else []
                data = items[0] if items else None
            else:
                response.raise_for_status()
                data = response.json()
    except Exception:
        return None

    if not data:
        return None

    synced_lyrics = (data.get("syncedLyrics") or "").strip()
    plain_lyrics = (data.get("plainLyrics") or "").strip()
    lines = _parse_synced_lyrics(synced_lyrics)
    return {
        "track": track,
        "artist": artist,
        "album": album,
        "source": "lrclib",
        "synced": len(lines) > 0,
        "plain_lyrics": plain_lyrics or synced_lyrics,
        "lines": lines,
    }


def _fix_thumbs(obj):
    if isinstance(obj, list):
        return [_fix_thumbs(i) for i in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "url" and isinstance(v, str) and v.startswith("/"):
                out[k] = INVIDIOUS_URL + v
            else:
                out[k] = _fix_thumbs(v)
        return out
    return obj


def _norm_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _album_query_title(title: str | None) -> str:
    if not title:
        return ""
    cleaned = re.sub(r"\b(full album|official audios?|official videos?)\b", " ", title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned.strip()


def _clean_playlist_title(title: str | None) -> str:
    if not title:
        return ""
    cleaned = _album_query_title(title)
    cleaned = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned.strip()


def _base_album_title(title: str | None) -> str:
    if not title:
        return ""
    cleaned = re.sub(
        r"(\(|\[).{0,40}\b(japan(?:ese)?|deluxe|expanded|bonus|collector'?s?|special edition|anniversary|remaster(?:ed)?|mono|stereo|instrumental|karaoke|live)\b.{0,40}(\)|\])",
        " ",
        title,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned.strip() or title


def _is_variant_extension(shorter: str, longer: str) -> bool:
    clean_short = _norm_text(shorter)
    clean_long = _norm_text(longer)
    if not clean_short or not clean_long:
        return False
    if clean_short == clean_long:
        return True
    if clean_short not in clean_long:
        return False

    extra = clean_long.replace(clean_short, " ", 1)
    extra = re.sub(r"\b(19|20)\d{2}\b", " ", extra)
    extra = VARIANT_TAIL_RE.sub(" ", extra)
    extra = re.sub(r"[^a-z0-9]+", " ", extra)
    return not extra.strip()


def _version_label(title: str | None) -> str:
    if not title:
        return ""
    lowered = title.lower()
    if "japan" in lowered:
        return "Japanese Edition"
    if "deluxe" in lowered:
        return "Deluxe"
    if "expanded" in lowered:
        return "Expanded"
    if "bonus" in lowered:
        return "Bonus Tracks"
    if "collector" in lowered:
        return "Collector's"
    if "anniversary" in lowered:
        return "Anniversary"
    if "remaster" in lowered:
        return "Remaster"
    if "live" in lowered:
        return "Live"
    if "instrumental" in lowered:
        return "Instrumental"
    if "karaoke" in lowered:
        return "Karaoke"
    if "mono" in lowered:
        return "Mono"
    if "stereo" in lowered:
        return "Stereo"
    return ""


def _extract_year(value: str | None) -> int | None:
    if not value:
        return None
    match = YEAR_RE.search(str(value))
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _pick_playlist_thumb(playlist: dict) -> str:
    return (
        playlist.get("playlistThumbnail")
        or (playlist.get("videos") or [{}])[0].get("videoThumbnails", [{}])[0].get("url", "")
        or ""
    )


def _playlist_author(playlist: dict, fallback: dict | None = None) -> str:
    return (
        (playlist.get("author") or "").strip()
        or ((fallback or {}).get("author") or "").strip()
    )


def _is_album_like(playlist: dict) -> bool:
    title = playlist.get("title", "") or ""
    count = int(playlist.get("videoCount") or 0)
    author = _playlist_author(playlist)
    if ALBUM_HINT_RE.search(title):
        return True
    return bool(author and 3 <= count <= 30)


def _album_score(title: str, artist: str, candidate: dict) -> float:
    clean_title = _norm_text(title)
    clean_artist = _norm_text(artist)
    cand_title = _norm_text(candidate.get("title"))
    cand_artist = _norm_text(candidate.get("artist"))

    if not clean_title or not cand_title:
        return 0.0

    title_score = SequenceMatcher(None, clean_title, cand_title).ratio()
    artist_score = SequenceMatcher(None, clean_artist, cand_artist).ratio() if clean_artist and cand_artist else 0.0

    if clean_title == cand_title:
        title_score = 1.0
    elif _is_variant_extension(clean_title, cand_title) or _is_variant_extension(cand_title, clean_title):
        title_score = max(title_score, 0.9)
    elif clean_title in cand_title or cand_title in clean_title:
        shorter = clean_title if len(clean_title) <= len(cand_title) else cand_title
        title_score = min(title_score, 0.38 if " " not in shorter else 0.58)

    if clean_artist and cand_artist and (clean_artist == cand_artist or clean_artist in cand_artist or cand_artist in clean_artist):
        artist_score = max(artist_score, 0.95)

    return (title_score * 0.75) + (artist_score * 0.25)


def _album_search_query(title: str, artist: str) -> str:
    clean_title = (title or "").strip()
    clean_artist = (artist or "").strip()
    if not clean_title:
        return clean_artist
    if not clean_artist or _norm_text(clean_artist) in _norm_text(clean_title):
        return clean_title
    return f"{clean_artist} {clean_title}".strip()


def _album_video_search_query(title: str, artist: str) -> str:
    base = _album_search_query(title, artist)
    if not base:
        return ""
    return f"{base} full album".strip()


def _album_query_hints(query: str) -> tuple[str, str]:
    cleaned = _clean_music_hint(query)
    if not cleaned:
        return "", ""

    for separator in (" - ", " – ", " — "):
        if separator not in cleaned:
            continue
        left, right = [part.strip() for part in cleaned.split(separator, 1)]
        if left and right and len(_norm_text(left).split()) <= 6:
            return right, left

    by_match = re.match(r"^(.+?)\s+by\s+(.+)$", cleaned, flags=re.IGNORECASE)
    if by_match:
        title = by_match.group(1).strip()
        artist = by_match.group(2).strip()
        if title and artist:
            return title, artist

    return cleaned, ""


def _source_priority(source: str) -> int:
    order = {"bandcamp": 5, "spotify": 4, "deezer": 3, "itunes": 2, "youtube": 1}
    return order.get((source or "").lower(), 0)


def _pick_video_thumb(video: dict) -> str:
    thumbs = video.get("videoThumbnails") or []
    for quality in ("high", "medium", "default"):
        for thumb in thumbs:
            if thumb.get("quality") == quality and thumb.get("url"):
                return thumb.get("url", "")
    return thumbs[0].get("url", "") if thumbs else ""


def _artist_score(query: str, artist_name: str) -> float:
    clean_query = _norm_text(query)
    clean_artist = _norm_text(artist_name)
    if not clean_query or not clean_artist:
        return 0.0

    score = SequenceMatcher(None, clean_query, clean_artist).ratio()
    if clean_query == clean_artist:
        return 1.0
    if clean_query.startswith(f"{clean_artist} "):
        return max(score, 0.96)
    if clean_artist.startswith(f"{clean_query} "):
        return max(score, 0.9)
    if clean_query in clean_artist or clean_artist in clean_query:
        return max(score, 0.82)
    return score


def _artist_query_remainder(query: str, artist_name: str) -> str:
    clean_query = _norm_text(query)
    clean_artist = _norm_text(artist_name)
    if not clean_query or not clean_artist:
        return ""
    if clean_query == clean_artist:
        return ""
    if clean_query.startswith(f"{clean_artist} "):
        return clean_query[len(clean_artist):].strip()
    if clean_query.endswith(f" {clean_artist}"):
        return clean_query[:-len(clean_artist)].strip()
    return ""


def _playlist_album_hints(playlist: dict) -> tuple[str, str]:
    raw_title = playlist.get("title", "") or ""
    author = _playlist_author(playlist)
    clean_title = _clean_playlist_title(raw_title) or raw_title
    search_title = _album_query_title(raw_title) or clean_title

    for candidate in (search_title, clean_title):
        if " - " not in candidate:
            continue
        left, right = [part.strip() for part in candidate.split(" - ", 1)]
        if not left or not right:
            continue
        if author and _artist_score(author, left) >= 0.72:
            return right, author
        if author and _artist_score(author, left) < 0.45 and len(_norm_text(left).split()) <= 5:
            return right, left
        if not author and len(_norm_text(left).split()) <= 5:
            return right, left

    return search_title or clean_title, author


def _album_query_score(query: str, album_title: str) -> float:
    clean_query = _norm_text(query)
    clean_title = _norm_text(album_title)
    if not clean_query or not clean_title:
        return 0.0
    score = SequenceMatcher(None, clean_query, clean_title).ratio()
    if clean_query == clean_title:
        return 1.0
    if clean_query in clean_title or clean_title in clean_query:
        return max(score, 0.92)
    return score


def _norm_track_title(title: str | None) -> str:
    if not title:
        return ""
    cleaned = _album_query_title(title)
    cleaned = TRACK_PREFIX_RE.sub("", cleaned)
    cleaned = TRACK_FEATURE_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return _norm_text(cleaned)


def _track_title_score(left: str | None, right: str | None) -> float:
    clean_left = _norm_track_title(left)
    clean_right = _norm_track_title(right)
    if not clean_left or not clean_right:
        return 0.0

    score = SequenceMatcher(None, clean_left, clean_right).ratio()
    if clean_left == clean_right:
        return 1.0
    if clean_left in clean_right or clean_right in clean_left:
        shorter = clean_left if len(clean_left) <= len(clean_right) else clean_right
        return max(score, 0.9 if " " in shorter else 0.72)
    return score


def _track_query_hints(query: str) -> tuple[str, str]:
    cleaned = _clean_music_hint(query)
    if not cleaned:
        return "", ""

    for separator in (" - ", " – ", " — "):
        if separator not in cleaned:
            continue
        left, right = [part.strip() for part in cleaned.split(separator, 1)]
        if left and right and len(_norm_text(left).split()) <= 6:
            return right, left

    by_match = re.match(r"^(.+?)\s+by\s+(.+)$", cleaned, flags=re.IGNORECASE)
    if by_match:
        track = by_match.group(1).strip()
        artist = by_match.group(2).strip()
        if track and artist:
            return track, artist

    return cleaned, ""


def _track_candidate_score(query: str, candidate: dict) -> float:
    track_hint, artist_hint = _track_query_hints(query)
    cand_track = candidate.get("track") or candidate.get("title") or ""
    cand_artist = candidate.get("artist") or ""
    combo = f"{cand_artist} {cand_track}".strip()

    combined_score = _track_title_score(query, combo)
    track_score = _track_title_score(track_hint or query, cand_track)
    score = max(track_score, combined_score)
    if artist_hint:
        artist_score = _artist_score(artist_hint, cand_artist)
        score = max(score, (track_score * 0.74) + (artist_score * 0.26))
        if _norm_track_title(track_hint) == _norm_track_title(cand_track) and artist_score >= 0.95:
            score = max(score, 0.995)
    elif _norm_text(query) == _norm_text(combo):
        score = 1.0

    if candidate.get("album") and _album_query_score(query, candidate.get("album") or "") >= 0.92:
        score += 0.02

    return round(min(score, 1.0), 3)


def _catalog_genres_from_item(item: dict) -> list[str]:
    out: list[str] = []
    g = (item.get("genre") or "").strip()
    if g:
        out.append(g)
    for raw in item.get("genres") or []:
        s = str(raw).strip()
        if s:
            out.append(s)
    return out


def _dedupe_genre_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for n in names:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            result.append(n)
    return result


def _enrich_music_search_track_matches(matches: list[dict]) -> None:
    ids = [str(m["matched_video_id"]) for m in matches if m.get("matched_video_id")]
    if not ids:
        return
    ratings = get_ratings_for_video_ids(ids)
    playlists = get_playlists_for_video_ids(ids, per_video_limit=6)
    tags = get_music_tags_for_video_ids(ids, per_video_limit=12)
    library_genres = get_music_library_genres_for_video_ids(ids)
    for m in matches:
        vid = str(m.get("matched_video_id") or "")
        if not vid:
            continue
        r = ratings.get(vid)
        m["rating"] = r if r is not None else None
        m["library_genre"] = library_genres.get(vid) or None
        m["playlists"] = playlists.get(vid, [])
        m["tags"] = tags.get(vid, [])


async def _search_track_candidates(query: str, limit: int = 6) -> list[dict]:
    if not query:
        return []

    results = await asyncio.gather(
        itunes_search(query, limit=max(limit, 8)),
        deezer_search(query, limit=max(limit, 8)),
        spotify_search(query, limit=max(limit, 8)),
        return_exceptions=True,
    )

    merged: dict[str, dict] = {}
    for batch in results:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            key = f"{_norm_track_title(item.get('track'))}::{_norm_text(item.get('artist'))}"
            if not key or key == "::":
                continue
            current = merged.get(key)
            if not current:
                merged[key] = {
                    **item,
                    "sources": [item.get("source")] if item.get("source") else [],
                    "catalog_genres": _dedupe_genre_names(_catalog_genres_from_item(item)),
                }
                continue

            chosen = current
            if _source_priority(item.get("source") or "") > _source_priority(current.get("source") or ""):
                chosen = {**current, **item}
            sources = {*(current.get("sources") or []), item.get("source")}
            chosen["cover_art"] = chosen.get("cover_art") or item.get("cover_art") or current.get("cover_art")
            chosen["album"] = chosen.get("album") or item.get("album") or current.get("album")
            chosen["duration"] = chosen.get("duration") or item.get("duration") or current.get("duration")
            chosen["release_date"] = chosen.get("release_date") or item.get("release_date") or current.get("release_date")
            chosen["popularity"] = max(
                int(current.get("popularity") or 0),
                int(item.get("popularity") or 0),
            )
            chosen["sources"] = [source for source in sources if source]
            chosen["catalog_genres"] = _dedupe_genre_names(
                (current.get("catalog_genres") or []) + _catalog_genres_from_item(item)
            )
            merged[key] = chosen

    ordered = list(merged.values())
    for item in ordered:
        item["sources"] = sorted(item.get("sources") or [], key=_source_priority, reverse=True)
        item["source_count"] = len(item.get("sources") or [])
        item["match_score"] = _track_candidate_score(query, item)
        if item.get("sources"):
            item["source"] = item["sources"][0]

    ordered.sort(
        key=lambda item: (
            float(item.get("match_score") or 0.0),
            int(item.get("source_count") or 0),
            _source_priority(item.get("source") or ""),
            math.log10(int(item.get("popularity") or 0) + 1),
        ),
        reverse=True,
    )
    return ordered[:limit]


def _video_track_hints(video: dict) -> tuple[str, str]:
    music = video.get("music") or {}
    music_track = _clean_music_hint((music.get("track") if isinstance(music, dict) else None) or "")
    music_artist = _clean_music_hint((music.get("artist") if isinstance(music, dict) else None) or "")
    if music_track:
        return music_track, music_artist or _clean_music_hint(video.get("author") or "")

    raw_title = (video.get("title") or "").strip()
    author = _clean_music_hint(video.get("author") or "")
    clean_title = _clean_music_hint(raw_title) or raw_title

    for separator in (" - ", " – ", " — "):
        if separator not in raw_title:
            continue
        left, right = [part.strip() for part in raw_title.split(separator, 1)]
        if not left or not right:
            continue
        if author and _artist_score(author, left) >= 0.72:
            return _clean_music_hint(right), author
        if len(_norm_text(left).split()) <= 6:
            return _clean_music_hint(right), _clean_music_hint(left)

    return clean_title, author


def _video_variant_penalty(query: str, video: dict) -> float:
    lowered_query = (query or "").lower()
    title = (video.get("title") or "").lower()
    author = (video.get("author") or "").lower()
    context = f"{title} {author}"
    if re.search(r"\b(review|reaction|meme|shitpost|analysis|explained|podcast|interview|breakdown)\b", context):
        if not re.search(r"\b(review|reaction|analysis|interview|podcast)\b", lowered_query):
            return 0.45
    if "cover" in title and "cover" not in lowered_query:
        return 0.24
    if "karaoke" in title and "karaoke" not in lowered_query:
        return 0.28
    if "nightcore" in title and "nightcore" not in lowered_query:
        return 0.28
    if ("sped up" in title or "speed up" in title) and "sped up" not in lowered_query and "speed up" not in lowered_query:
        return 0.2
    if "slowed" in title and "slowed" not in lowered_query:
        return 0.18
    if "reverb" in title and "reverb" not in lowered_query:
        return 0.12
    if "bass boosted" in title and "bass boosted" not in lowered_query:
        return 0.16
    if "remix" in title and "remix" not in lowered_query:
        return 0.14
    if "mashup" in title and "mashup" not in lowered_query:
        return 0.2
    if "tribute" in title and "tribute" not in lowered_query:
        return 0.16
    return 0.0


def _video_search_bonus(video: dict, position: int) -> float:
    title = video.get("title") or ""
    author = video.get("author") or ""
    bonus = max(0.0, 0.06 - (position * 0.0025))
    if TRACK_CANONICAL_RE.search(title):
        bonus += 0.08
    if author.lower().endswith(" - topic"):
        bonus += 0.05
    duration = int(video.get("lengthSeconds") or 0)
    if 90 <= duration <= 480:
        bonus += 0.04
    elif duration >= 900:
        bonus -= 0.08
    views = int(video.get("viewCount") or 0)
    if views > 0:
        bonus += min(math.log10(views + 1) / 80.0, 0.05)
    return bonus


def _video_query_match_score(query: str, video: dict, position: int) -> float:
    title = video.get("title") or ""
    author = video.get("author") or ""
    track_hint, artist_hint = _track_query_hints(query)
    combo = f"{author} {title}".strip()
    score = max(
        _track_title_score(query, title),
        _track_title_score(query, combo),
        _track_title_score(track_hint or query, title),
    )
    if artist_hint:
        score = max(
            score,
            (_track_title_score(track_hint, title) * 0.74) + (_artist_score(artist_hint, author) * 0.26),
        )

    score += _video_search_bonus(video, position)
    score -= _video_variant_penalty(query, video)
    return round(min(max(score, 0.0), 1.0), 3)


def _video_track_match_score(candidate: dict, video: dict, query: str, position: int) -> float:
    title_hint, artist_hint = _video_track_hints(video)
    raw_title = video.get("title") or ""
    raw_author = video.get("author") or ""

    track_score = max(
        _track_title_score(candidate.get("track"), title_hint),
        _track_title_score(candidate.get("track"), raw_title),
    )
    artist_score = _artist_score(candidate.get("artist") or "", artist_hint or raw_author) if candidate.get("artist") else 0.0
    score = track_score if not candidate.get("artist") else (track_score * 0.74) + (artist_score * 0.26)

    if candidate.get("artist") and _norm_track_title(candidate.get("track")) == _norm_track_title(title_hint) and artist_score >= 0.92:
        score += 0.08

    score += _video_search_bonus(video, position)
    score -= _video_variant_penalty(query, video)
    return round(min(max(score, 0.0), 1.0), 3)


def _serialize_track_match(candidate: dict, video: dict, score: float) -> dict:
    catalog_genres = list(candidate.get("catalog_genres") or [])
    return {
        "track_key": f"{_norm_track_title(candidate.get('track'))}::{_norm_text(candidate.get('artist'))}",
        "track": candidate.get("track") or candidate.get("title") or "",
        "artist": candidate.get("artist") or "",
        "album": candidate.get("album") or "",
        "cover_art": candidate.get("cover_art") or _pick_video_thumb(video),
        "duration": candidate.get("duration"),
        "release_date": candidate.get("release_date") or "",
        "source": candidate.get("source") or "",
        "sources": candidate.get("sources") or [],
        "source_count": candidate.get("source_count") or 0,
        "catalog_genres": catalog_genres,
        "match_score": round(score, 3),
        "matched_video_id": video.get("videoId") or "",
        "matched_video_title": video.get("title") or "",
        "matched_video_author": video.get("author") or "",
        "matched_video_thumbnail": _pick_video_thumb(video),
        "matched_video_duration": video.get("lengthSeconds"),
        "matched_video_view_count": video.get("viewCount"),
        "rating": None,
        "library_genre": None,
        "playlists": [],
        "tags": [],
    }


def _serialize_track_catalog_search(candidate: dict, catalog_match: float) -> dict:
    catalog_genres = list(candidate.get("catalog_genres") or [])
    return {
        "track_key": f"{_norm_track_title(candidate.get('track'))}::{_norm_text(candidate.get('artist'))}",
        "track": candidate.get("track") or candidate.get("title") or "",
        "artist": candidate.get("artist") or "",
        "album": candidate.get("album") or "",
        "cover_art": candidate.get("cover_art") or "",
        "duration": candidate.get("duration"),
        "release_date": candidate.get("release_date") or "",
        "source": candidate.get("source") or "",
        "sources": candidate.get("sources") or [],
        "source_count": candidate.get("source_count") or 0,
        "catalog_genres": catalog_genres,
        "match_score": round(catalog_match, 3),
        "matched_video_id": "",
        "matched_video_title": "",
        "matched_video_author": "",
        "matched_video_thumbnail": "",
        "matched_video_duration": None,
        "matched_video_view_count": None,
        "rating": None,
        "library_genre": None,
        "playlists": [],
        "tags": [],
    }


def _rank_track_search_results(videos: list[dict], query: str, track_candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    videos = videos or []
    candidate_best: dict[str, tuple[float, dict]] = {}
    ranked: list[tuple[float, int, dict]] = []

    for index, video in enumerate(videos):
        best_candidate = None
        best_candidate_score = 0.0
        for candidate in track_candidates:
            score = _video_track_match_score(candidate, video, query, index)
            if score > best_candidate_score:
                best_candidate = candidate
                best_candidate_score = score

        query_score = _video_query_match_score(query, video, index)
        effective_score = max(query_score, best_candidate_score)
        if isinstance(video.get("music"), dict):
            effective_score += min(float(video["music"].get("confidence") or 0.0), 1.0) * 0.06
        effective_score = round(min(effective_score, 1.35), 3)

        enriched = {**video}
        if best_candidate and best_candidate_score >= 0.68:
            enriched["search_match"] = {
                "track": best_candidate.get("track") or best_candidate.get("title") or "",
                "artist": best_candidate.get("artist") or "",
                "album": best_candidate.get("album") or "",
                "source": best_candidate.get("source") or "",
                "score": round(best_candidate_score, 3),
            }
            key = f"{_norm_track_title(best_candidate.get('track'))}::{_norm_text(best_candidate.get('artist'))}"
            current = candidate_best.get(key)
            if not current or best_candidate_score > current[0]:
                candidate_best[key] = (best_candidate_score, enriched)
        ranked.append((effective_score, index, enriched))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    ordered_videos = [item[2] for item in ranked]

    sorted_candidates = sorted(
        track_candidates,
        key=lambda c: (
            float(c.get("match_score") or 0.0),
            int(c.get("source_count") or 0),
            _source_priority(c.get("source") or ""),
        ),
        reverse=True,
    )

    top_tracks: list[dict] = []
    used_video_ids: set[str] = set()
    seen_track_keys: set[str] = set()

    for candidate in sorted_candidates[:14]:
        key = f"{_norm_track_title(candidate.get('track'))}::{_norm_text(candidate.get('artist'))}"
        if key in seen_track_keys:
            continue
        catalog_match = float(candidate.get("match_score") or 0.0)

        best_video = None
        best_vs = 0.0
        for index, video in enumerate(videos):
            vs = _video_track_match_score(candidate, video, query, index)
            if vs > best_vs:
                best_vs = vs
                best_video = video

        if best_video and best_vs >= 0.54:
            video_id = best_video.get("videoId") or ""
            if video_id and video_id not in used_video_ids:
                top_tracks.append(_serialize_track_match(candidate, best_video, best_vs))
                used_video_ids.add(video_id)
                seen_track_keys.add(key)
                continue

        if catalog_match >= 0.32:
            top_tracks.append(_serialize_track_catalog_search(candidate, catalog_match))
            seen_track_keys.add(key)

    if used_video_ids and ranked:
        adjusted = []
        for score, idx, enriched in ranked:
            vid = enriched.get("videoId") or ""
            adj = score - (0.055 if vid in used_video_ids else 0.0)
            adjusted.append((adj, idx, enriched))
        adjusted.sort(key=lambda item: (-item[0], item[1]))
        ordered_videos = [item[2] for item in adjusted]

    return ordered_videos, top_tracks


def _ordered_track_title_ratio(tracks: list[dict], videos: list[dict]) -> tuple[float, int]:
    metadata_titles = [
        item.get("title") or ""
        for item in sorted(
            tracks,
            key=lambda item: (int(item.get("disc_number") or 1), int(item.get("position") or 0)),
        )
        if item.get("title")
    ]
    playlist_titles = [item.get("title") or "" for item in videos if item.get("title")]
    if not metadata_titles or not playlist_titles:
        return 0.0, 0

    cursor = 0
    matched = 0
    for track_title in metadata_titles:
        best_index = -1
        best_score = 0.0
        for index in range(cursor, len(playlist_titles)):
            score = _track_title_score(track_title, playlist_titles[index])
            if score > best_score:
                best_score = score
                best_index = index
            if score >= 0.98:
                break
        if best_score >= TRACK_TITLE_MATCH_THRESHOLD and best_index >= 0:
            matched += 1
            cursor = best_index + 1

    denominator = min(len(metadata_titles), len(playlist_titles))
    if denominator <= 0:
        return 0.0, 0
    return round(matched / denominator, 3), matched


def _count_similarity(actual: int, expected: int) -> float:
    if actual <= 0 or expected <= 0:
        return 0.0
    ratio = min(actual, expected) / max(actual, expected)
    diff = abs(actual - expected)
    if diff <= 1:
        return 1.0
    if diff <= 2:
        return max(ratio, 0.85)
    return round(ratio, 3)


def _duration_similarity(actual: int, expected: int) -> tuple[float, float | None]:
    if actual <= 0 or expected <= 0:
        return 0.0, None
    ratio = float(actual) / float(expected)
    if PLAYLIST_DURATION_MIN_RATIO <= ratio <= PLAYLIST_DURATION_MAX_RATIO:
        distance = abs(1.0 - ratio)
        score = max(0.0, 1.0 - (distance / max(1.0 - PLAYLIST_DURATION_MIN_RATIO, PLAYLIST_DURATION_MAX_RATIO - 1.0)))
    else:
        score = 0.0
    return round(score, 3), round(ratio, 3)


def _sum_video_list_duration_seconds(videos: list | None) -> int:
    total = 0
    for item in videos or []:
        if isinstance(item, dict):
            total += int(item.get("lengthSeconds") or 0)
    return total


def _sum_bandcamp_tracks_duration_seconds(candidate: dict | None) -> int:
    if not isinstance(candidate, dict):
        return 0
    total = 0
    for track in candidate.get("bandcamp_tracks") or []:
        if isinstance(track, dict):
            total += int(track.get("duration") or 0)
    return total


def _resolve_album_total_duration_seconds(candidate: dict | None) -> int | None:
    if not isinstance(candidate, dict):
        return None
    meta = int(candidate.get("metadata_total_duration") or 0)
    if meta > 0:
        return meta
    pl = int(candidate.get("playlist_total_duration") or 0)
    if pl > 0:
        return pl
    bc = _sum_bandcamp_tracks_duration_seconds(candidate)
    if bc > 0:
        return bc
    return None


def _merge_bandcamp_album_details(candidate: dict, details: dict) -> dict:
    merged = {**candidate}
    streamable_track_count = sum(
        1
        for item in (details.get("bandcamp_tracks") or [])
        if isinstance(item, dict) and (item.get("audio_url") or "").strip()
    )
    merged["title"] = merged.get("title") or details.get("title") or ""
    merged["artist"] = merged.get("artist") or details.get("artist") or ""
    merged["cover_art"] = merged.get("cover_art") or details.get("cover_art") or ""
    merged["release_date"] = merged.get("release_date") or details.get("release_date") or ""
    merged["year"] = merged.get("year") or details.get("year") or ""
    merged["track_count"] = merged.get("track_count") or details.get("track_count")
    merged["bandcamp_url"] = merged.get("bandcamp_url") or details.get("bandcamp_url") or ""
    merged["bandcamp_embed_url"] = merged.get("bandcamp_embed_url") or details.get("bandcamp_embed_url") or ""
    merged["bandcamp_album_id"] = merged.get("bandcamp_album_id") or details.get("bandcamp_album_id") or ""
    merged["bandcamp_tracks"] = details.get("bandcamp_tracks") or merged.get("bandcamp_tracks") or []
    merged["bandcamp_streamable_track_count"] = streamable_track_count
    merged["bandcamp_streamable"] = streamable_track_count > 0
    merged_sources = {*(merged.get("sources") or []), "bandcamp"}
    merged["sources"] = [source for source in merged_sources if source]
    merged["source_count"] = len(merged["sources"])
    if not merged.get("source"):
        merged["source"] = "bandcamp"
    return merged


def _bandcamp_streamable_track_count(candidate: dict | None) -> int:
    if not isinstance(candidate, dict):
        return 0
    explicit = candidate.get("bandcamp_streamable_track_count")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            return 0
    return sum(
        1
        for item in (candidate.get("bandcamp_tracks") or [])
        if isinstance(item, dict) and (item.get("audio_url") or "").strip()
    )


def _bandcamp_is_streamable(candidate: dict | None) -> bool:
    if not isinstance(candidate, dict) or not candidate.get("bandcamp_url"):
        return False
    explicit = candidate.get("bandcamp_streamable")
    if explicit is not None:
        return bool(explicit)
    return _bandcamp_streamable_track_count(candidate) > 0


def _primary_album_target_kind(candidate: dict | None, fallback: str) -> str:
    return "bandcamp" if _bandcamp_is_streamable(candidate) else fallback


async def _enrich_bandcamp_album_candidate(candidate: dict) -> dict:
    if not candidate.get("bandcamp_url"):
        return candidate
    if (
        candidate.get("bandcamp_tracks")
        and candidate.get("track_count")
        and candidate.get("cover_art")
        and (candidate.get("release_date") or candidate.get("year"))
    ):
        return candidate
    details = await bandcamp_album_details(candidate.get("bandcamp_url"))
    if not details:
        return candidate
    return _merge_bandcamp_album_details(candidate, details)


async def _fetch_candidate_tracks(candidate: dict) -> tuple[list[dict], str]:
    batches: list[tuple[int, list[dict], str]] = []

    bandcamp_tracks = candidate.get("bandcamp_tracks") or []
    if not bandcamp_tracks and candidate.get("bandcamp_url"):
        details = await bandcamp_album_details(candidate.get("bandcamp_url"))
        if details:
            candidate.update(_merge_bandcamp_album_details(candidate, details))
            bandcamp_tracks = candidate.get("bandcamp_tracks") or []
    if bandcamp_tracks:
        batches.append((5, bandcamp_tracks, "bandcamp"))
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


async def _playlist_candidate_integrity(candidate: dict, playlist: dict) -> dict:
    videos = [item for item in (playlist.get("videos") or []) if isinstance(item, dict)]
    playlist_track_count = len(videos) or int(playlist.get("videoCount") or 0)
    if playlist_track_count <= 0:
        return {}

    playlist_total_duration = sum(int(item.get("lengthSeconds") or 0) for item in videos)
    tracks, track_source = await _fetch_candidate_tracks(candidate)
    metadata_track_count = int(candidate.get("track_count") or 0) or len(tracks)
    metadata_total_duration = sum(int(item.get("duration") or 0) for item in tracks)

    count_score = _count_similarity(playlist_track_count, metadata_track_count) if metadata_track_count else 0.0
    duration_score, duration_ratio = _duration_similarity(playlist_total_duration, metadata_total_duration) if metadata_total_duration else (0.0, None)
    title_ratio, matched_titles = _ordered_track_title_ratio(tracks, videos) if tracks else (0.0, 0)

    components: list[tuple[float, float]] = []
    if metadata_track_count:
        components.append((count_score, 0.34))
    if metadata_total_duration:
        components.append((duration_score, 0.33))
    if tracks:
        components.append((title_ratio, 0.33))
    if not components:
        return {}

    total_weight = sum(weight for _, weight in components)
    integrity_score = round(sum(score * weight for score, weight in components) / total_weight, 3)
    return {
        "integrity_score": integrity_score,
        "integrity_source": track_source or "",
        "playlist_track_count": playlist_track_count,
        "metadata_track_count": metadata_track_count,
        "playlist_total_duration": playlist_total_duration,
        "metadata_total_duration": metadata_total_duration,
        "matched_titles": matched_titles,
        "title_ratio": title_ratio,
        "count_score": round(count_score, 3),
        "duration_score": duration_score,
        "duration_ratio": duration_ratio,
        "tracks_checked": bool(tracks),
        "duration_checked": metadata_total_duration > 0,
    }


def _playlist_candidate_result(match_score: float, integrity: dict, album_like: bool) -> tuple[float, bool]:
    combined_score = round(
        (match_score * 0.7) + (float(integrity.get("integrity_score") or 0.0) * 0.3),
        3,
    ) if integrity else round(match_score, 3)

    minimum_match_score = 0.48 if album_like else 0.54
    minimum_score = PLAYLIST_CONFIDENT_THRESHOLD if album_like else 0.62
    if match_score < minimum_match_score or combined_score < minimum_score:
        return combined_score, False

    if not integrity:
        return combined_score, match_score >= (0.58 if album_like else 0.68)

    if float(integrity.get("integrity_score") or 0.0) < PLAYLIST_INTEGRITY_THRESHOLD:
        return combined_score, False

    if float(integrity.get("count_score") or 0.0) < PLAYLIST_INTEGRITY_COUNT_THRESHOLD:
        return combined_score, False

    if integrity.get("duration_checked") and float(integrity.get("duration_score") or 0.0) < PLAYLIST_INTEGRITY_DURATION_THRESHOLD:
        return combined_score, False

    if integrity.get("tracks_checked"):
        title_ratio = float(integrity.get("title_ratio") or 0.0)
        matched_titles = int(integrity.get("matched_titles") or 0)
        if title_ratio < PLAYLIST_INTEGRITY_TITLE_THRESHOLD and matched_titles < 2:
            return combined_score, False

    return combined_score, True


async def _score_playlist_candidate(candidate: dict, playlist: dict, album_like: bool) -> dict | None:
    title_hint, artist_hint = _playlist_album_hints(playlist)
    match_score = max(
        _album_score(title_hint, artist_hint, candidate),
        _album_score(_base_album_title(title_hint), artist_hint, candidate) * 0.98,
    )
    if match_score < PLAYLIST_MATCH_THRESHOLD:
        return None

    assessed = {**candidate}
    integrity = await _playlist_candidate_integrity(assessed, playlist)
    if integrity.get("metadata_track_count"):
        assessed["track_count"] = integrity["metadata_track_count"]
    assessed.update(integrity)
    score, matched = _playlist_candidate_result(match_score, integrity, album_like)
    return {
        "candidate": assessed,
        "score": score,
        "matched": matched,
        "match_score": round(match_score, 3),
    }


async def _search_album_candidates(query: str, artist_hint: str = "", limit: int = 4) -> list[dict]:
    if not query:
        return []
    search_query = _album_search_query(query, artist_hint)
    results = await asyncio.gather(
        bandcamp_search_albums(search_query, limit=limit),
        itunes_search_album(search_query, limit=limit),
        deezer_search_album(search_query, limit=limit),
        spotify_search_album(search_query, limit=limit),
        return_exceptions=True,
    )

    merged: dict[str, dict] = {}
    for batch in results:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            key = f"{_norm_text(item.get('artist'))}::{_norm_text(item.get('title'))}"
            if not key:
                continue
            current = merged.get(key)
            if not current:
                merged[key] = {
                    **item,
                    "sources": [item.get("source")] if item.get("source") else [],
                }
                continue
            chosen = current
            if _source_priority(item.get("source") or "") > _source_priority(current.get("source") or ""):
                chosen = {**current, **item}
            sources = {*(current.get("sources") or []), item.get("source")}
            chosen["cover_art"] = chosen.get("cover_art") or item.get("cover_art") or current.get("cover_art")
            chosen["release_date"] = chosen.get("release_date") or item.get("release_date") or current.get("release_date")
            chosen["year"] = chosen.get("year") or item.get("year") or current.get("year")
            chosen["track_count"] = chosen.get("track_count") or item.get("track_count") or current.get("track_count")
            chosen["bandcamp_url"] = chosen.get("bandcamp_url") or item.get("bandcamp_url") or current.get("bandcamp_url") or ""
            chosen["bandcamp_embed_url"] = chosen.get("bandcamp_embed_url") or item.get("bandcamp_embed_url") or current.get("bandcamp_embed_url") or ""
            chosen["bandcamp_album_id"] = chosen.get("bandcamp_album_id") or item.get("bandcamp_album_id") or current.get("bandcamp_album_id") or ""
            chosen["bandcamp_tracks"] = chosen.get("bandcamp_tracks") or item.get("bandcamp_tracks") or current.get("bandcamp_tracks") or []
            chosen["sources"] = [s for s in sources if s]
            merged[key] = chosen
    ordered = list(merged.values())
    for item in ordered:
        item["sources"] = sorted(item.get("sources") or [], key=_source_priority, reverse=True)
        item["source_count"] = len(item["sources"])
        if item.get("sources"):
            item["source"] = item["sources"][0]
    return ordered


async def _search_artist_candidates(query: str, limit: int = 5) -> list[dict]:
    if not query:
        return []

    results = await asyncio.gather(
        spotify_search_artist(query, limit=limit),
        deezer_search_artist(query, limit=limit),
        itunes_search_artist(query, limit=limit),
        return_exceptions=True,
    )

    merged: dict[str, dict] = {}
    for batch in results:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            key = _norm_text(item.get("artist"))
            if not key:
                continue
            current = merged.get(key)
            if not current:
                merged[key] = {
                    **item,
                    "sources": [item.get("source")] if item.get("source") else [],
                }
                continue
            chosen = current
            if _source_priority(item.get("source") or "") > _source_priority(current.get("source") or ""):
                chosen = {**current, **item}
            sources = {*(current.get("sources") or []), item.get("source")}
            chosen["image"] = chosen.get("image") or item.get("image") or current.get("image")
            chosen["popularity"] = max(
                int(current.get("popularity") or 0),
                int(item.get("popularity") or 0),
            )
            chosen["spotify_artist_id"] = chosen.get("spotify_artist_id") or item.get("spotify_artist_id") or ""
            chosen["deezer_artist_id"] = chosen.get("deezer_artist_id") or item.get("deezer_artist_id") or ""
            chosen["itunes_artist_id"] = chosen.get("itunes_artist_id") or item.get("itunes_artist_id") or ""
            chosen["sources"] = [source for source in sources if source]
            merged[key] = chosen

    ordered = list(merged.values())
    for item in ordered:
        item["sources"] = sorted(item.get("sources") or [], key=_source_priority, reverse=True)
        item["source_count"] = len(item["sources"])
        item["match_confidence"] = round(_artist_score(query, item.get("artist") or ""), 3)
        if item.get("sources"):
            item["source"] = item["sources"][0]
    ordered.sort(
        key=lambda item: (
            float(item.get("match_confidence") or 0.0),
            _source_priority(item.get("source") or ""),
            int(item.get("popularity") or 0),
        ),
        reverse=True,
    )
    return ordered


def _is_album_release(item: dict) -> bool:
    album_type = (item.get("album_type") or "").lower()
    if album_type and album_type not in {"album", "ep"}:
        return False
    return True


def _artist_album_quality(item: dict, artist_name: str, remainder: str, query_match_score: float) -> float:
    title = item.get("title") or ""
    base_title = _base_album_title(title)
    quality = float(item.get("source_count") or 0.0) * 4.0
    quality += query_match_score * 20.0

    if item.get("source") in {"bandcamp", "deezer", "spotify"}:
        quality += 2.5
    if item.get("source_count", 0) > 1:
        quality += 1.5
    if base_title != title:
        quality -= 2.0
    if COMPILATION_TITLE_RE.search(title):
        quality -= 4.5
    if LIVE_RELEASE_RE.search(title):
        quality -= 3.0
    if not remainder and _norm_text(title) == _norm_text(artist_name):
        quality -= 3.5

    return round(quality, 3)


async def _search_artist_albums(artist_match: dict, query: str, limit: int = 8) -> list[dict]:
    artist_name = artist_match.get("artist") or ""
    if not artist_name:
        return []

    tasks = [
        bandcamp_search_albums(artist_name, limit=max(limit * 2, 12)),
        itunes_search_album(artist_name, limit=max(limit * 2, 12)),
    ]
    if artist_match.get("deezer_artist_id"):
        tasks.append(deezer_get_artist_albums(artist_match.get("deezer_artist_id"), limit=max(limit * 2, 12)))
    if artist_match.get("spotify_artist_id"):
        tasks.append(spotify_get_artist_albums(artist_match.get("spotify_artist_id"), limit=max(limit * 2, 12)))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: dict[str, dict] = {}
    remainder = _artist_query_remainder(query, artist_name)
    for batch in results:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            normalized_item = {**item, "artist": item.get("artist") or artist_name}
            if not _is_album_release(item):
                continue
            if not remainder and _norm_text(normalized_item.get("title")) == _norm_text(artist_name):
                continue
            if _artist_score(artist_name, normalized_item.get("artist") or "") < 0.72:
                continue
            base_title = _base_album_title(normalized_item.get("title"))
            key = f"{_norm_text(normalized_item.get('artist'))}::{_norm_text(base_title or normalized_item.get('title'))}"
            if not key:
                continue
            query_match_score = _album_query_score(remainder, normalized_item.get("title") or "")
            release_quality = _artist_album_quality(normalized_item, artist_name, remainder, query_match_score)
            current = merged.get(key)
            if not current:
                merged[key] = {
                    **normalized_item,
                    "sources": [normalized_item.get("source")] if normalized_item.get("source") else [],
                    "query_match_score": query_match_score,
                    "release_quality": release_quality,
                }
                continue
            chosen = current
            if release_quality > float(current.get("release_quality") or 0.0):
                chosen = {**current, **normalized_item}
            elif release_quality == float(current.get("release_quality") or 0.0) and _source_priority(normalized_item.get("source") or "") > _source_priority(current.get("source") or ""):
                chosen = {**current, **normalized_item}
            sources = {*(current.get("sources") or []), normalized_item.get("source")}
            chosen["cover_art"] = chosen.get("cover_art") or item.get("cover_art") or current.get("cover_art")
            chosen["release_date"] = chosen.get("release_date") or item.get("release_date") or current.get("release_date")
            chosen["year"] = chosen.get("year") or item.get("year") or current.get("year")
            chosen["track_count"] = chosen.get("track_count") or item.get("track_count") or current.get("track_count")
            chosen["spotify_album_id"] = chosen.get("spotify_album_id") or item.get("spotify_album_id") or ""
            chosen["deezer_album_id"] = chosen.get("deezer_album_id") or item.get("deezer_album_id") or ""
            chosen["itunes_album_id"] = chosen.get("itunes_album_id") or item.get("itunes_album_id") or ""
            chosen["bandcamp_url"] = chosen.get("bandcamp_url") or item.get("bandcamp_url") or current.get("bandcamp_url") or ""
            chosen["bandcamp_embed_url"] = chosen.get("bandcamp_embed_url") or item.get("bandcamp_embed_url") or current.get("bandcamp_embed_url") or ""
            chosen["bandcamp_album_id"] = chosen.get("bandcamp_album_id") or item.get("bandcamp_album_id") or current.get("bandcamp_album_id") or ""
            chosen["bandcamp_tracks"] = chosen.get("bandcamp_tracks") or item.get("bandcamp_tracks") or current.get("bandcamp_tracks") or []
            chosen["sources"] = [source for source in sources if source]
            chosen["query_match_score"] = max(
                float(current.get("query_match_score") or 0.0),
                query_match_score,
            )
            chosen["release_quality"] = max(
                float(current.get("release_quality") or 0.0),
                release_quality,
            )
            merged[key] = chosen

    ordered = list(merged.values())
    for item in ordered:
        item["sources"] = sorted(item.get("sources") or [], key=_source_priority, reverse=True)
        item["source_count"] = len(item["sources"])
        if item.get("sources"):
            item["source"] = item["sources"][0]
        item["release_quality"] = _artist_album_quality(
            item,
            artist_name,
            remainder,
            float(item.get("query_match_score") or 0.0),
        )
    ordered.sort(
        key=lambda item: (
            -float(item.get("query_match_score") or 0.0),
            -float(item.get("release_quality") or 0.0),
            _extract_year(item.get("release_date") or item.get("year")) or 9999,
            -float(item.get("source_count") or 0.0),
            item.get("title") or "",
        ),
    )
    return ordered[:limit]


def _library_metrics(title: str, artist: str, album_key: str) -> dict:
    conn = get_db()
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN LOWER(COALESCE(album, '')) = LOWER(?) THEN 1 ELSE 0 END) AS album_matches,
            SUM(CASE WHEN LOWER(COALESCE(artist, '')) = LOWER(?) THEN 1 ELSE 0 END) AS artist_matches
        FROM music_library
        """,
        (title or "", artist or ""),
    ).fetchone()
    conn.close()
    rating = get_album_rating(album_key) if album_key else None
    return {
        "album_matches": int(row["album_matches"] or 0) if row else 0,
        "artist_matches": int(row["artist_matches"] or 0) if row else 0,
        "rating": rating["rating"] if rating else None,
    }


def _build_album_base(
    candidate: dict,
    fallback_title: str,
    fallback_artist: str,
    *,
    cover_art: str = "",
    cover_art_origin: str = "",
    cover_art_note: str = "",
    track_count: int | None = None,
    youtube_popularity: int = 0,
    confidence: float = 1.0,
    matched: bool = True,
    version_hint: str = "",
) -> dict:
    title = candidate.get("title") or fallback_title
    artist = candidate.get("artist") or fallback_artist
    album_key = normalize_album_key(title, artist)
    base_title = _base_album_title(title)
    base_album_key = normalize_album_key(base_title, artist)
    metrics = _library_metrics(title, artist, album_key)
    query_match_score = float(candidate.get("query_match_score") or 0.0)
    popularity_score = round(
        (math.log10(max(youtube_popularity, 0) + 1) * 8.0)
        + (float(candidate.get("source_count") or 0) * 6.0)
        + (confidence * 10.0)
        + (query_match_score * 18.0),
        3,
    )
    library_score = round((metrics["album_matches"] * 2.0) + (metrics["artist_matches"] * 0.5) + ((metrics["rating"] or 0) * 1.5), 3)
    default_score = round((confidence * 100.0) + popularity_score + (library_score * 4.0) + (8.0 if matched else 0.0), 3)
    resolved_cover_art = cover_art or candidate.get("cover_art") or ""
    resolved_cover_origin = cover_art_origin
    resolved_cover_note = cover_art_note
    if not resolved_cover_origin:
        if candidate.get("cover_art") and resolved_cover_art:
            resolved_cover_origin = "metadata"
        elif resolved_cover_art:
            resolved_cover_origin = "youtube_thumbnail"
    if not resolved_cover_note:
        if resolved_cover_origin == "metadata":
            resolved_cover_note = f"Cover matched from {candidate.get('source') or 'metadata'}"
        elif resolved_cover_origin == "youtube_thumbnail":
            resolved_cover_note = "Using the YouTube thumbnail as cover art"
    return {
        "album_key": album_key,
        "base_album_key": base_album_key,
        "base_title": base_title,
        "title": title,
        "artist": artist,
        "bandcamp_url": candidate.get("bandcamp_url") or "",
        "bandcamp_embed_url": candidate.get("bandcamp_embed_url") or "",
        "bandcamp_streamable": _bandcamp_is_streamable(candidate),
        "bandcamp_streamable_track_count": _bandcamp_streamable_track_count(candidate),
        "cover_art": resolved_cover_art,
        "cover_art_origin": resolved_cover_origin,
        "cover_art_note": resolved_cover_note,
        "release_date": candidate.get("release_date") or "",
        "year": candidate.get("year") or "",
        "track_count": track_count or candidate.get("track_count"),
        "source": candidate.get("source") or "youtube",
        "sources": candidate.get("sources") or ([candidate.get("source")] if candidate.get("source") else []),
        "source_count": candidate.get("source_count") or (1 if candidate.get("source") else 0),
        "integrity_score": candidate.get("integrity_score"),
        "integrity_source": candidate.get("integrity_source") or "",
        "integrity_matched_titles": candidate.get("matched_titles"),
        "integrity_track_count": candidate.get("metadata_track_count"),
        "integrity_duration_ratio": candidate.get("duration_ratio"),
        "matched": matched,
        "confidence": round(confidence, 3),
        "rating": metrics["rating"],
        "version_label": _version_label(version_hint or candidate.get("title") or fallback_title),
        "library_album_matches": metrics["album_matches"],
        "library_artist_matches": metrics["artist_matches"],
        "youtube_popularity": youtube_popularity,
        "popularity_score": popularity_score,
        "library_score": library_score,
        "default_score": default_score,
        "total_duration_seconds": _resolve_album_total_duration_seconds(candidate),
    }


def _build_album_payload(candidate: dict, playlist: dict, confidence: float, matched: bool) -> dict:
    playlist_title_hint, playlist_artist_hint = _playlist_album_hints(playlist)
    album = _build_album_base(
        candidate,
        playlist_title_hint or _clean_playlist_title(playlist.get("title")) or playlist.get("title", ""),
        playlist_artist_hint or _playlist_author(playlist),
        cover_art=candidate.get("cover_art") or _pick_playlist_thumb(playlist),
        cover_art_origin="metadata" if candidate.get("cover_art") else "youtube_thumbnail",
        track_count=candidate.get("track_count") or playlist.get("videoCount"),
        youtube_popularity=int(playlist.get("viewCount") or 0),
        confidence=confidence,
        matched=matched,
        version_hint=candidate.get("title") or playlist.get("title") or "",
    )
    album["primary_target_kind"] = _primary_album_target_kind(album, "playlist")
    return album


def _build_metadata_album_group(candidate: dict) -> dict:
    album = _build_album_base(
        candidate,
        candidate.get("title", ""),
        candidate.get("artist", ""),
        cover_art=candidate.get("cover_art") or "",
        cover_art_origin="metadata" if candidate.get("cover_art") else "",
        track_count=candidate.get("track_count"),
        confidence=1.0,
        matched=True,
        version_hint=candidate.get("title") or "",
    )
    return {
        "album_key": album.get("base_album_key") or album.get("album_key") or candidate.get("title"),
        "title": album.get("base_title") or album.get("title") or candidate.get("title"),
        "artist": album.get("artist") or candidate.get("artist"),
        "release_date": album.get("release_date") or "",
        "year": album.get("year") or "",
        "bandcamp_url": album.get("bandcamp_url") or "",
        "bandcamp_embed_url": album.get("bandcamp_embed_url") or "",
        "cover_art": album.get("cover_art"),
        "cover_art_origin": album.get("cover_art_origin") or "metadata",
        "cover_art_note": album.get("cover_art_note"),
        "track_count": album.get("track_count"),
        "source": album.get("source") or "",
        "sources": album.get("sources") or [],
        "source_count": album.get("source_count") or 0,
        "matched": album.get("matched"),
        "confidence": album.get("confidence"),
        "version_label": album.get("version_label") or "",
        "library_album_matches": album.get("library_album_matches") or 0,
        "library_artist_matches": album.get("library_artist_matches") or 0,
        "rating": album.get("rating"),
        "default_score": album.get("default_score") or 0.0,
        "popularity_score": album.get("popularity_score") or 0.0,
        "library_score": album.get("library_score") or 0.0,
        "chronological_key": _chronological_key(album),
        "youtube_popularity": 0,
        "primary_target_kind": _primary_album_target_kind(album, "metadata"),
        "total_duration_seconds": album.get("total_duration_seconds"),
        "variants": [],
    }


def _build_bandcamp_sidebar_recommendation_album_group(row: dict) -> dict | None:
    """Shape a resolved Bandcamp sidebar row into a ``MusicAlbumGroup`` for album-page recommendations."""
    album_title = (row.get("album") or "").strip()
    if not album_title:
        album_title = (row.get("track") or "").strip()
    if not album_title:
        album_title = _album_query_title(row.get("title") or "")
    artist_raw = (row.get("artist") or row.get("author") or "").strip()
    bc_url = (row.get("bandcamp_url") or "").strip()
    if not bc_url and not album_title:
        return None

    thumb = row.get("thumbnail")
    if isinstance(thumb, list) and thumb:
        first = thumb[0]
        thumb = first.get("url") if isinstance(first, dict) else ""
    elif not isinstance(thumb, str):
        thumb = ""

    vid = (row.get("video_id") or "").strip()
    yt_pop = int(row.get("viewCount") or 0) if row.get("viewCount") else 0
    conf = min(float(row.get("graph_score") or row.get("recommendation_score") or 0.85), 1.0)

    candidate = {
        "title": album_title,
        "artist": artist_raw,
        "bandcamp_url": bc_url,
        "source": "bandcamp",
        "sources": ["bandcamp"],
        "source_count": 1,
    }
    album = _build_album_base(
        candidate,
        album_title,
        artist_raw,
        cover_art=thumb,
        cover_art_origin="youtube_thumbnail" if thumb else "",
        track_count=row.get("track_count"),
        youtube_popularity=yt_pop,
        confidence=conf,
        matched=True,
        version_hint=album_title,
    )

    primary_kind = "bandcamp" if bc_url else _primary_album_target_kind(album, "metadata")
    if vid and not bc_url:
        primary_kind = _primary_album_target_kind(album, "video")

    group: dict = {
        "album_key": album.get("base_album_key") or album.get("album_key") or candidate.get("title"),
        "title": album.get("base_title") or album.get("title") or album_title,
        "artist": album.get("artist") or artist_raw,
        "release_date": album.get("release_date") or "",
        "year": album.get("year") or "",
        "bandcamp_url": album.get("bandcamp_url") or "",
        "bandcamp_embed_url": album.get("bandcamp_embed_url") or "",
        "cover_art": album.get("cover_art"),
        "cover_art_origin": album.get("cover_art_origin") or ("youtube_thumbnail" if thumb else "metadata"),
        "cover_art_note": album.get("cover_art_note"),
        "track_count": album.get("track_count"),
        "source": album.get("source") or "bandcamp",
        "sources": album.get("sources") or ["bandcamp"],
        "source_count": album.get("source_count") or 1,
        "matched": album.get("matched"),
        "confidence": album.get("confidence"),
        "version_label": album.get("version_label") or "",
        "library_album_matches": album.get("library_album_matches") or 0,
        "library_artist_matches": album.get("library_artist_matches") or 0,
        "rating": album.get("rating"),
        "default_score": album.get("default_score") or 0.0,
        "popularity_score": album.get("popularity_score") or 0.0,
        "library_score": album.get("library_score") or 0.0,
        "chronological_key": _chronological_key(album),
        "youtube_popularity": yt_pop,
        "primary_target_kind": primary_kind,
        "variants": [],
    }

    td = album.get("total_duration_seconds") or (int(row.get("lengthSeconds") or 0) if vid else None) or None
    if td and td > 0:
        group["total_duration_seconds"] = td

    if vid:
        vt = (row.get("title") or "").strip()
        group["primary_video_id"] = vid
        group["primary_video_title"] = vt
        group["primary_video_author"] = row.get("author")
        group["primary_video_thumbnail"] = thumb or None
        group["primary_video_duration"] = row.get("lengthSeconds")
        group["primary_video_view_count"] = row.get("viewCount")
        group["primary_video_published"] = row.get("published")
        group["primary_video_is_full_album"] = bool(FULL_ALBUM_VIDEO_RE.search(vt or ""))

    return group


def _serialize_artist_match(artist_match: dict, album_count: int | None = None) -> dict:
    payload = {
        "name": artist_match.get("artist", ""),
        "image": artist_match.get("image") or "",
        "source": artist_match.get("source") or "",
        "sources": artist_match.get("sources") or ([artist_match.get("source")] if artist_match.get("source") else []),
        "match_confidence": artist_match.get("match_confidence") or 0.0,
        "popularity": int(artist_match.get("popularity") or 0),
        "spotify_artist_id": artist_match.get("spotify_artist_id") or "",
        "deezer_artist_id": artist_match.get("deezer_artist_id") or "",
        "itunes_artist_id": artist_match.get("itunes_artist_id") or "",
    }
    if album_count is not None:
        payload["album_count"] = album_count
    return payload


def _serialize_album_track(track: dict) -> dict:
    return {
        "position": int(track.get("position") or 0) or 0,
        "title": track.get("title") or "",
        "duration": int(track.get("duration") or 0) or None,
        "artist": track.get("artist") or "",
        "disc_number": int(track.get("disc_number") or 0) or None,
        "source": track.get("source") or "",
    }


def _album_detail_candidate_score(album_key: str, title: str, artist: str, candidate: dict) -> float:
    score = _album_score(title, artist, candidate)
    candidate_album_key = normalize_album_key(candidate.get("title"), candidate.get("artist"))
    candidate_base_key = normalize_album_key(_base_album_title(candidate.get("title")), candidate.get("artist"))
    if album_key and album_key == candidate_album_key:
        score += 0.18
    if album_key and album_key == candidate_base_key:
        score += 0.12
    return round(score, 3)


async def _related_artist_matches(artist_match: dict, limit: int = 6) -> list[dict]:
    deezer_artist_id = artist_match.get("deezer_artist_id")
    if not deezer_artist_id:
        return []

    seeds = await deezer_get_related_artists(deezer_artist_id, limit=max(limit * 2, 8))
    if not seeds:
        return []

    scoped_seeds = [seed for seed in seeds[:max(limit * 2, 8)] if seed.get("artist")]
    batches = await asyncio.gather(
        *[
            _search_artist_candidates(seed.get("artist") or "", limit=3)
            for seed in scoped_seeds
        ],
        return_exceptions=True,
    )

    owner_key = _norm_text(artist_match.get("artist"))
    related: list[dict] = []
    seen: set[str] = set()

    for seed, batch in zip(scoped_seeds, batches):
        best = None
        if isinstance(batch, list) and batch:
            best = max(
                batch,
                key=lambda item: _artist_score(seed.get("artist") or "", item.get("artist") or ""),
            )
        candidate = {**seed, **(best or {})}
        key = _norm_text(candidate.get("artist"))
        if not key or key == owner_key or key in seen:
            continue
        seen.add(key)
        related.append(_serialize_artist_match(candidate))
        if len(related) >= limit:
            break

    return related


def _playlist_match_score(album_candidate: dict, playlist: dict) -> float:
    title_hint, artist_hint = _playlist_album_hints(playlist)
    return max(
        _album_score(title_hint, artist_hint, album_candidate),
        _album_score(_base_album_title(title_hint), artist_hint, album_candidate) * 0.98,
    )


async def _enrich_playlist_for_album(playlist_summary: dict, album_candidate: dict) -> dict | None:
    detail = None
    try:
        detail = await api_get(f"/playlists/{playlist_summary.get('playlistId')}", {"page": 1})
    except Exception:
        detail = None

    merged = {**playlist_summary}
    if isinstance(detail, dict):
        merged.update(detail)
    merged = _fix_thumbs(merged)
    assessed = await _score_playlist_candidate(album_candidate, merged, _is_album_like(merged))
    if not assessed or not assessed.get("matched"):
        return None
    album = _build_album_payload(
        assessed["candidate"],
        merged,
        float(assessed.get("score") or 0.0),
        True,
    )
    return _serialize_playlist(merged, album)


def _video_album_match_score(album_candidate: dict, video: dict) -> float:
    if VIDEO_REJECTION_RE.search(video.get("title", "") or ""):
        return 0.0

    score = _album_score(
        album_candidate.get("title") or "",
        album_candidate.get("artist") or "",
        {
            "title": _album_query_title(video.get("title")),
            "artist": video.get("author"),
        },
    )

    if FULL_ALBUM_VIDEO_RE.search(video.get("title", "") or ""):
        score += 0.15
    if album_candidate.get("artist") and _artist_score(album_candidate.get("artist"), video.get("author") or "") >= 0.9:
        score += 0.08

    duration = int(video.get("lengthSeconds") or 0)
    if duration > 0:
        track_count = int(album_candidate.get("track_count") or 0)
        expected = max(900, track_count * 150) if track_count else 900
        score += min(duration / max(expected, 1), 1.1) * 0.09

    return score


def _serialize_album_video_group(album_candidate: dict, video: dict, score: float) -> dict:
    album = _build_album_base(
        album_candidate,
        album_candidate.get("title", ""),
        album_candidate.get("artist", ""),
        cover_art=album_candidate.get("cover_art") or _pick_video_thumb(video),
        cover_art_origin="metadata" if album_candidate.get("cover_art") else "youtube_thumbnail",
        track_count=album_candidate.get("track_count"),
        youtube_popularity=int(video.get("viewCount") or 0),
        confidence=min(score, 1.0),
        matched=score >= VIDEO_MATCH_THRESHOLD,
        version_hint=video.get("title") or album_candidate.get("title") or "",
    )
    return {
        "album_key": album.get("base_album_key") or album.get("album_key") or video.get("videoId"),
        "title": album.get("base_title") or album.get("title") or video.get("title"),
        "artist": album.get("artist") or video.get("author"),
        "release_date": album.get("release_date") or "",
        "year": album.get("year") or "",
        "bandcamp_url": album.get("bandcamp_url") or "",
        "bandcamp_embed_url": album.get("bandcamp_embed_url") or "",
        "cover_art": album.get("cover_art") or _pick_video_thumb(video),
        "cover_art_origin": album.get("cover_art_origin") or "youtube_thumbnail",
        "cover_art_note": album.get("cover_art_note") or "Using the YouTube thumbnail as cover art",
        "track_count": album.get("track_count"),
        "source": album.get("source") or "",
        "sources": album.get("sources") or [],
        "source_count": album.get("source_count") or 0,
        "matched": album.get("matched"),
        "confidence": album.get("confidence"),
        "version_label": album.get("version_label") or "",
        "library_album_matches": album.get("library_album_matches") or 0,
        "library_artist_matches": album.get("library_artist_matches") or 0,
        "rating": album.get("rating"),
        "default_score": album.get("default_score") or 0.0,
        "popularity_score": album.get("popularity_score") or 0.0,
        "library_score": album.get("library_score") or 0.0,
        "chronological_key": _chronological_key(album),
        "youtube_popularity": int(video.get("viewCount") or 0),
        "primary_target_kind": _primary_album_target_kind(album, "video"),
        "primary_video_id": video.get("videoId"),
        "primary_video_title": video.get("title"),
        "primary_video_author": video.get("author"),
        "primary_video_thumbnail": _pick_video_thumb(video),
        "primary_video_duration": video.get("lengthSeconds"),
        "primary_video_view_count": video.get("viewCount"),
        "primary_video_published": video.get("published"),
        "primary_video_is_full_album": bool(FULL_ALBUM_VIDEO_RE.search(video.get("title", "") or "")),
        "total_duration_seconds": album.get("total_duration_seconds") or (int(video.get("lengthSeconds") or 0) or None),
        "variants": [],
    }


async def _resolve_artist_album_group(album_candidate: dict) -> dict:
    album_candidate = await _enrich_bandcamp_album_candidate(album_candidate)
    artist = (album_candidate.get("artist") or "").strip()
    title = (album_candidate.get("title") or "").strip()
    playlist_query = _album_search_query(title, artist)
    video_query = _album_video_search_query(title, artist)

    playlist_items: list[dict] = []
    if playlist_query:
        try:
            playlists = await api_get("/search", {"q": playlist_query, "page": 1, "type": "playlist"})
        except Exception:
            playlists = []
        playlist_items = _fix_thumbs(playlists[:4]) if isinstance(playlists, list) else []

    playlist_matches = await asyncio.gather(
        *[_enrich_playlist_for_album(item, album_candidate) for item in playlist_items],
        return_exceptions=True,
    ) if playlist_items else []
    matched_playlists = [item for item in playlist_matches if isinstance(item, dict)]
    if matched_playlists:
        return _serialize_album_group(matched_playlists)

    if not video_query:
        return _build_metadata_album_group(album_candidate)

    try:
        videos = await api_get("/search", {"q": video_query, "page": 1, "type": "video"})
    except Exception:
        videos = []
    video_items = _fix_thumbs(videos[:8]) if isinstance(videos, list) else []

    best_video = None
    best_score = 0.0
    for video in video_items:
        score = _video_album_match_score(album_candidate, video)
        if score > best_score:
            best_video = video
            best_score = score

    if best_video and best_score >= VIDEO_MATCH_THRESHOLD:
        return _serialize_album_video_group(album_candidate, best_video, best_score)

    return _build_metadata_album_group(album_candidate)


async def _search_query_full_album_groups(query: str, limit: int = 4) -> list[dict]:
    title_hint, artist_hint = _album_query_hints(query)
    if not title_hint:
        return []

    candidate = {
        "title": title_hint,
        "artist": artist_hint,
        "source": "youtube",
        "sources": ["youtube"],
        "source_count": 0,
    }
    video_query = _album_video_search_query(title_hint, artist_hint)
    if not video_query:
        return []

    try:
        videos = await api_get("/search", {"q": video_query, "page": 1, "type": "video"})
    except Exception:
        videos = []
    video_items = _fix_thumbs(videos[: max(limit * 3, 8)]) if isinstance(videos, list) else []

    ranked: list[tuple[float, dict]] = []
    for video in video_items:
        if not FULL_ALBUM_VIDEO_RE.search(video.get("title", "") or ""):
            continue
        score = _video_album_match_score(candidate, video)
        if score < VIDEO_MATCH_THRESHOLD:
            continue
        ranked.append((score, video))

    ranked.sort(
        key=lambda item: (
            item[0],
            int(item[1].get("viewCount") or 0),
            int(item[1].get("lengthSeconds") or 0),
        ),
        reverse=True,
    )

    results: list[dict] = []
    seen_ids: set[str] = set()
    for score, video in ranked:
        video_id = video.get("videoId")
        if not video_id or video_id in seen_ids:
            continue
        seen_ids.add(video_id)
        results.append(_serialize_album_video_group(candidate, video, score))
        if len(results) >= limit:
            break
    return results


async def _resolve_playlist_album(playlist: dict) -> dict | None:
    raw_title = playlist.get("title", "") or ""
    clean_title, author = _playlist_album_hints(playlist)
    search_title = clean_title
    album_like = _is_album_like(playlist)
    if not clean_title:
        clean_title = raw_title
    if not search_title:
        search_title = clean_title

    candidates = await _search_album_candidates(search_title, author)
    ranked_candidates: list[tuple[float, dict]] = []
    for candidate in candidates:
        score = max(
            _album_score(clean_title, author, candidate),
            _album_score(search_title, author, candidate) * 0.98,
        )
        if score < PLAYLIST_MATCH_THRESHOLD:
            continue
        ranked_candidates.append((score, candidate))

    ranked_candidates.sort(key=lambda item: item[0], reverse=True)
    best = None
    best_score = 0.0
    for _, candidate in ranked_candidates[:3]:
        assessed = await _score_playlist_candidate(candidate, playlist, album_like)
        if not assessed or not assessed.get("matched"):
            continue
        score = float(assessed.get("score") or 0.0)
        if score > best_score:
            best = assessed["candidate"]
            best_score = score

    if best:
        return _build_album_payload(best, playlist, best_score, True)

    if album_like:
        return _build_album_payload(
            {
                "title": clean_title or playlist.get("title", ""),
                "artist": author,
                "cover_art": _pick_playlist_thumb(playlist),
                "track_count": playlist.get("videoCount"),
                "source": "youtube",
                "sources": ["youtube"],
                "source_count": 0,
            },
            playlist,
            0.35,
            False,
        )

    return None


def _chronological_key(album: dict | None) -> int:
    if not album:
        return 999999
    return _extract_year(album.get("release_date") or album.get("year")) or 999999


def _serialize_album_group(playlists: list[dict]) -> dict:
    primary = max(
        playlists,
        key=lambda item: (
            float((item.get("album") or {}).get("default_score") or 0.0),
            float((item.get("album") or {}).get("confidence") or 0.0),
            int(item.get("viewCount") or 0),
        ),
    )
    album = primary.get("album") or {}
    variants = sorted(
        playlists,
        key=lambda item: (
            float((item.get("album") or {}).get("default_score") or 0.0),
            float((item.get("album") or {}).get("confidence") or 0.0),
            int(item.get("viewCount") or 0),
        ),
        reverse=True,
    )
    total_sec = album.get("total_duration_seconds")
    if not total_sec:
        vsum = _sum_video_list_duration_seconds(primary.get("videos"))
        total_sec = vsum if vsum > 0 else None
    return {
        "album_key": album.get("base_album_key") or album.get("album_key") or primary.get("playlistId"),
        "title": album.get("base_title") or album.get("title") or primary.get("title"),
        "artist": album.get("artist") or primary.get("author"),
        "release_date": album.get("release_date") or "",
        "year": album.get("year") or "",
        "bandcamp_url": album.get("bandcamp_url") or "",
        "bandcamp_embed_url": album.get("bandcamp_embed_url") or "",
        "cover_art": album.get("cover_art") or primary.get("playlistThumbnail"),
        "cover_art_origin": album.get("cover_art_origin") or "youtube_thumbnail",
        "cover_art_note": album.get("cover_art_note") or "Using the YouTube thumbnail as cover art",
        "track_count": album.get("track_count") or primary.get("videoCount"),
        "source": album.get("source") or "",
        "sources": album.get("sources") or [],
        "source_count": album.get("source_count") or 0,
        "matched": album.get("matched"),
        "confidence": album.get("confidence"),
        "version_label": album.get("version_label") or "",
        "library_album_matches": album.get("library_album_matches") or 0,
        "library_artist_matches": album.get("library_artist_matches") or 0,
        "rating": album.get("rating"),
        "default_score": album.get("default_score") or 0.0,
        "popularity_score": album.get("popularity_score") or 0.0,
        "library_score": album.get("library_score") or 0.0,
        "chronological_key": _chronological_key(album),
        "youtube_popularity": sum(int(item.get("viewCount") or 0) for item in variants),
        "primary_target_kind": _primary_album_target_kind(album, "playlist"),
        "primary_playlist_id": primary.get("playlistId"),
        "primary_playlist_title": primary.get("title"),
        "total_duration_seconds": total_sec,
        "variants": variants,
    }


def _group_album_results(playlists: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in playlists:
        album = item.get("album")
        if not isinstance(album, dict):
            continue
        group_key = album.get("base_album_key") or album.get("album_key") or item.get("playlistId")
        grouped.setdefault(group_key, []).append(item)
    groups = [_serialize_album_group(items) for items in grouped.values()]
    groups.sort(key=lambda item: (-float(item.get("default_score") or 0.0), item.get("chronological_key") or 999999, item.get("title") or ""))
    return groups


def _serialize_playlist(playlist: dict, album: dict | None) -> dict:
    return {
        "playlistId": playlist.get("playlistId", ""),
        "title": playlist.get("title", ""),
        "author": _playlist_author(playlist),
        "authorId": playlist.get("authorId", ""),
        "playlistThumbnail": _pick_playlist_thumb(playlist),
        "description": playlist.get("description", ""),
        "videoCount": playlist.get("videoCount"),
        "viewCount": playlist.get("viewCount"),
        "updated": playlist.get("updated"),
        "isAlbumLike": bool(album),
        "album": album,
    }


async def _enrich_playlist_result(playlist_summary: dict) -> dict:
    detail = None
    try:
        detail = await api_get(f"/playlists/{playlist_summary.get('playlistId')}", {"page": 1})
    except Exception:
        detail = None

    merged = {**playlist_summary}
    if isinstance(detail, dict):
        merged.update(detail)
    merged = _fix_thumbs(merged)
    album = await _resolve_playlist_album(merged)
    return _serialize_playlist(merged, album)


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/search")
async def music_search(q: str = Query(...), page: int = Query(1)):
    track_candidates = await _search_track_candidates(q, limit=10)

    try:
        videos_task = api_get("/search", {"q": q, "page": page, "type": "video"})
        playlists_task = api_get("/search", {"q": q, "page": page, "type": "playlist"})
        videos, playlists = await asyncio.gather(videos_task, playlists_task, return_exceptions=True)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    if isinstance(videos, Exception) or not isinstance(videos, list):
        videos = []
    if isinstance(playlists, Exception) or not isinstance(playlists, list):
        playlists = []

    videos = _fix_thumbs(videos)
    playlists = _fix_thumbs(playlists)

    annotated = []
    for i, video in enumerate(videos[:20]):
        rec = quick_recognize(video, position=i)
        annotated.append({**video, "music": rec.to_dict()})

    ranked_videos, track_matches = _rank_track_search_results(annotated, q, track_candidates)
    if ranked_videos:
        annotated = ranked_videos

    playlist_results = await asyncio.gather(
        *[_enrich_playlist_result(item) for item in playlists[:8]],
        return_exceptions=True,
    )
    enriched_playlists = [
        item for item in playlist_results if isinstance(item, dict)
    ]
    artist_payload = None
    album_groups: list[dict] = []

    if page == 1:
        search_album_candidates = await _search_album_candidates(q, limit=6)
        if search_album_candidates:
            search_album_results = await asyncio.gather(
                *[_resolve_artist_album_group(item) for item in search_album_candidates],
                return_exceptions=True,
            )
            album_groups = [item for item in search_album_results if isinstance(item, dict)]

        artist_candidates = await _search_artist_candidates(q, limit=4)
        if artist_candidates:
            best_artist = artist_candidates[0]
            if float(best_artist.get("match_confidence") or 0.0) >= ARTIST_MATCH_THRESHOLD:
                artist_albums = await _search_artist_albums(best_artist, q, limit=8)
                artist_payload = _serialize_artist_match(best_artist, len(artist_albums))
                if artist_albums and not album_groups:
                    album_results = await asyncio.gather(
                        *[_resolve_artist_album_group(item) for item in artist_albums],
                        return_exceptions=True,
                    )
                    album_groups = [item for item in album_results if isinstance(item, dict)]

    if not album_groups:
        album_groups = _group_album_results(enriched_playlists)

    if page == 1 and not album_groups:
        album_groups = await _search_query_full_album_groups(q, limit=6)

    if track_matches:
        _enrich_music_search_track_matches(track_matches)

    return {
        "artist": artist_payload,
        "tracks": track_matches,
        "videos": annotated,
        "playlists": enriched_playlists,
        "albums": album_groups,
    }


@router.get("/playlist/{playlist_id}/album")
async def playlist_album(playlist_id: str):
    try:
        detail = await api_get(f"/playlists/{playlist_id}", {"page": 1})
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    detail = _fix_thumbs(detail)
    album = await _resolve_playlist_album(detail)
    return _serialize_playlist(detail, album)


def _evenly_spaced_track_indices(n: int, k: int) -> list[int]:
    """Indices into [0..n-1), spread across the playlist (up to k samples)."""
    if n <= 0 or k <= 0:
        return []
    k = min(k, n)
    if k == n:
        return list(range(n))
    raw: list[int] = []
    seen: set[int] = set()
    for i in range(k):
        idx = int(round(i * (n - 1) / (k - 1))) if k > 1 else 0
        idx = max(0, min(n - 1, idx))
        if idx not in seen:
            seen.add(idx)
            raw.append(idx)
    j = 0
    while len(raw) < k and j < n:
        if j not in seen:
            seen.add(j)
            raw.append(j)
        j += 1
    return raw


_playlist_reco_recognize_sem = asyncio.Semaphore(6)


async def _playlist_video_seed_track(video_id: str) -> tuple[str, str] | None:
    async with _playlist_reco_recognize_sem:
        try:
            data = await api_get(f"/videos/{video_id}")
        except Exception:
            return None
        rec = await recognize(data)
        if not rec.is_music:
            return None
        track = (rec.track or data.get("title", "") or "").strip()
        artist = (rec.artist or data.get("author", "") or "").strip()
        if not track or not artist:
            return None
        return (track, artist)


@router.get("/playlist/{playlist_id}/recommendations")
async def playlist_music_recommendations(
    playlist_id: str,
    max_seed_tracks: int = Query(16, ge=4, le=32),
    limit: int = Query(24, ge=4, le=48),
):
    """
    Aggregate music recommendations for a playlist: recognize a spread of tracks from page 1,
    run the same catalog pipeline as `/music/{video_id}/recommendations` per seed, then merge
    round-robin and dedupe (excluding videos already in the playlist).
    """
    try:
        detail = await api_get(f"/playlists/{playlist_id}", {"page": 1})
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    detail = _fix_thumbs(detail)
    videos = [v for v in (detail.get("videos") or []) if isinstance(v, dict)]
    playable = [v for v in videos if not UNAVAILABLE_VIDEO_RE.search(v.get("title") or "")]
    if not playable:
        playable = videos
    exclude_ids = {v.get("videoId") for v in playable if v.get("videoId")}
    indices = _evenly_spaced_track_indices(len(playable), max_seed_tracks)
    sampled = [playable[i] for i in indices if i < len(playable)]

    seed_tasks = [_playlist_video_seed_track(v.get("videoId", "")) for v in sampled if v.get("videoId")]
    seed_results = await asyncio.gather(*seed_tasks) if seed_tasks else []
    seeds = [s for s in seed_results if s is not None]

    if not seeds:
        return {
            "recommendations": [],
            "seed_tracks_used": 0,
        }

    n_seeds = max(len(seeds), 1)
    per_seed_limit = max(8, min(14, limit // n_seeds + 4))
    recs = await get_playlist_aggregate_recommendations(
        seeds,
        exclude_video_ids=exclude_ids,
        per_seed_limit=per_seed_limit,
        out_limit=limit,
    )
    return {
        "recommendations": recs,
        "seed_tracks_used": len(seeds),
    }


@router.get("/artist/{artist_name}")
async def music_artist_detail(artist_name: str):
    name = artist_name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Artist name is required")

    artist_candidates = await _search_artist_candidates(name, limit=6)
    if not artist_candidates:
        raise HTTPException(status_code=404, detail="Artist not found")

    best_artist = artist_candidates[0]
    if float(best_artist.get("match_confidence") or 0.0) < 0.65:
        raise HTTPException(status_code=404, detail="Artist not found")

    artist_albums = await _search_artist_albums(best_artist, name, limit=12)
    album_results = await asyncio.gather(
        *[_resolve_artist_album_group(item) for item in artist_albums],
        return_exceptions=True,
    ) if artist_albums else []

    albums = [item for item in album_results if isinstance(item, dict)]
    related_artists = await _related_artist_matches(best_artist, limit=6)

    return {
        "artist": _serialize_artist_match(best_artist, len(artist_albums)),
        "albums": albums,
        "related_artists": related_artists,
    }


@router.get("/album/{album_key}")
async def music_album_detail(
    album_key: str,
    title: str = Query(""),
    artist: str = Query(""),
):
    resolved_title = title.strip()
    resolved_artist = artist.strip()

    if not resolved_title and not resolved_artist and not album_key.strip():
        raise HTTPException(status_code=422, detail="Album metadata is required")

    search_title = resolved_title or album_key.replace("::", " ").strip()
    search_candidates = await _search_album_candidates(search_title, resolved_artist, limit=8)
    if not search_candidates:
        raise HTTPException(status_code=404, detail="Album not found")

    ranked_candidates = sorted(
        search_candidates,
        key=lambda item: (
            _album_detail_candidate_score(album_key, search_title, resolved_artist, item),
            _source_priority(item.get("source") or ""),
            int(item.get("track_count") or 0),
        ),
        reverse=True,
    )
    best_candidate = ranked_candidates[0]
    if _album_detail_candidate_score(album_key, search_title, resolved_artist, best_candidate) < 0.5:
        raise HTTPException(status_code=404, detail="Album not found")

    album_group = await _resolve_artist_album_group(best_candidate)
    tracks, tracks_source = await _fetch_candidate_tracks(best_candidate)
    serialized_tracks = [_serialize_album_track(track) for track in tracks]
    track_sum = sum(int(track.get("duration") or 0) for track in tracks if isinstance(track, dict))
    if track_sum > 0 and not album_group.get("total_duration_seconds"):
        album_group["total_duration_seconds"] = track_sum

    more_from_artist: list[dict] = []
    if best_candidate.get("artist"):
        artist_candidates = await _search_artist_candidates(best_candidate.get("artist"), limit=4)
        if artist_candidates:
            sibling_candidates = await _search_artist_albums(artist_candidates[0], best_candidate.get("artist"), limit=10)
            sibling_candidates = [
                item for item in sibling_candidates
                if normalize_album_key(_base_album_title(item.get("title")), item.get("artist")) != album_key
                and normalize_album_key(item.get("title"), item.get("artist")) != album_key
            ][:6]
            sibling_results = await asyncio.gather(
                *[_resolve_artist_album_group(item) for item in sibling_candidates],
                return_exceptions=True,
            ) if sibling_candidates else []
            more_from_artist = [item for item in sibling_results if isinstance(item, dict)]

    return {
        "album": album_group,
        "tracks": serialized_tracks,
        "tracks_source": tracks_source or "",
        "more_from_artist": more_from_artist,
    }


class ArtistFollowRequest(BaseModel):
    name: str | None = None
    image: str | None = None
    source: str | None = None
    spotify_artist_id: str | None = None
    deezer_artist_id: str | None = None
    itunes_artist_id: str | None = None


@router.get("/follows/artists")
async def music_followed_artists(
    limit: int = Query(24, ge=1, le=200),
    release_limit: int = Query(24, ge=1, le=100),
):
    return {
        "artists": list_artist_follows(limit),
        "releases": list_artist_release_events(release_limit),
    }


@router.post("/follows/artists/check")
async def check_followed_artists():
    results = await check_followed_artists_once()
    return {
        "ok": True,
        "checked": len(results),
    }


@router.post("/follows/artists/sync-from-ratings")
async def sync_follows_from_album_ratings_endpoint(
    min_rating: int = Query(8, ge=1, le=10),
):
    """Backfill artist_follows from album_ratings so highly rated library artists appear under Following."""
    stats = sync_artist_follows_from_album_ratings(min_rating=min_rating)
    return {"ok": True, **stats}


@router.get("/artist/{artist_name}/follow")
async def artist_follow_state(artist_name: str):
    follow = get_artist_follow(artist_name)
    return {
        "followed": bool(follow),
        "artist": follow,
    }


@router.post("/artist/{artist_name}/follow")
async def follow_artist(artist_name: str, body: ArtistFollowRequest):
    resolved_name = (body.name or artist_name).strip()
    if not resolved_name:
        raise HTTPException(status_code=422, detail="Artist name is required")

    save_artist_follow(
        resolved_name,
        image=body.image,
        source=body.source,
        spotify_artist_id=body.spotify_artist_id,
        deezer_artist_id=body.deezer_artist_id,
        itunes_artist_id=body.itunes_artist_id,
    )
    result = await check_followed_artist(resolved_name)
    follow = (result or {}).get("artist") or get_artist_follow(resolved_name)
    return {
        "followed": True,
        "artist": follow,
        "latest_release": (result or {}).get("latest_release"),
    }


@router.delete("/artist/{artist_name}/follow")
async def unfollow_artist(artist_name: str):
    delete_artist_follow(artist_name)
    return {"ok": True}


# ── Recognition & recommendations ────────────────────────────────────────────

@router.get("/stats")
async def music_stats(limit: int = Query(6, ge=1, le=12)):
    conn = get_db()
    try:
        music_filter = """
            (
                ml.track IS NOT NULL
                OR at.album_key IS NOT NULL
                OR LOWER(COALESCE(vm.genre, '')) = 'music'
            )
        """
        base_from = f"""
            FROM watch_history h
            LEFT JOIN music_library ml ON ml.video_id = h.video_id
            LEFT JOIN album_tracks at ON at.video_id = h.video_id
            LEFT JOIN video_metadata vm ON vm.video_id = h.video_id
            WHERE {music_filter}
        """
        top_tracks = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT
                    h.video_id,
                    COALESCE(NULLIF(ml.track, ''), h.title) AS track,
                    COALESCE(NULLIF(ml.artist, ''), NULLIF(at.album_artist, ''), h.author) AS artist,
                    COALESCE(NULLIF(ml.album, ''), NULLIF(at.album_title, '')) AS album,
                    COALESCE(ml.thumbnail, h.thumbnail) AS thumbnail,
                    COALESCE(h.listen_count, 1) AS listens,
                    h.watched_at AS last_played
                {base_from}
                ORDER BY listens DESC, h.watched_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        top_artists = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(ml.artist, ''), NULLIF(at.album_artist, ''), h.author) AS artist,
                    SUM(COALESCE(h.listen_count, 1)) AS listens,
                    COUNT(DISTINCT h.video_id) AS track_count,
                    MAX(h.watched_at) AS last_played
                {base_from}
                GROUP BY artist
                HAVING artist IS NOT NULL AND TRIM(artist) != ''
                ORDER BY listens DESC, last_played DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        top_albums = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT
                    LOWER(TRIM(COALESCE(NULLIF(at.album_artist, ''), NULLIF(ml.artist, ''), h.author, '')))
                      || '::' ||
                    LOWER(TRIM(COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, ''), ''))) AS album_key,
                    COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, '')) AS title,
                    COALESCE(NULLIF(at.album_artist, ''), NULLIF(ml.artist, ''), h.author) AS artist,
                    COALESCE(ar.cover_art, MAX(ml.thumbnail), MAX(h.thumbnail)) AS thumbnail,
                    SUM(COALESCE(h.listen_count, 1)) AS listens,
                    COUNT(DISTINCT h.video_id) AS track_count,
                    MAX(h.watched_at) AS last_played
                FROM watch_history h
                LEFT JOIN music_library ml ON ml.video_id = h.video_id
                LEFT JOIN album_tracks at ON at.video_id = h.video_id
                LEFT JOIN video_metadata vm ON vm.video_id = h.video_id
                LEFT JOIN album_ratings ar ON ar.album_key = at.album_key
                WHERE {music_filter}
                GROUP BY 1, 2, 3
                HAVING COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, '')) IS NOT NULL
                   AND TRIM(COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, ''))) != ''
                ORDER BY listens DESC, last_played DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        recent_seed = conn.execute(
            f"""
            SELECT
                h.video_id,
                COALESCE(NULLIF(ml.track, ''), h.title) AS track,
                COALESCE(NULLIF(ml.artist, ''), NULLIF(at.album_artist, ''), h.author) AS artist,
                COALESCE(NULLIF(ml.album, ''), NULLIF(at.album_title, '')) AS album,
                COALESCE(ml.thumbnail, h.thumbnail) AS thumbnail,
                COALESCE(h.listen_count, 1) AS listens,
                h.watched_at AS last_played
            {base_from}
            ORDER BY h.watched_at DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    recent_seed_payload = dict(recent_seed) if recent_seed else None
    similar_tracks = []
    if recent_seed_payload and recent_seed_payload.get("track") and recent_seed_payload.get("artist"):
        similar_tracks = await get_recommendations(
            recent_seed_payload["track"],
            recent_seed_payload["artist"],
            limit=max(6, limit),
        )

    return {
        "recent_seed": recent_seed_payload,
        "top_tracks": top_tracks,
        "top_artists": top_artists,
        "top_albums": top_albums,
        "similar_tracks": similar_tracks,
    }


@router.get("/fallback")
async def music_fallback(
    video_id: str = Query(""),
    title: str = Query(""),
    artist: str = Query(""),
):
    track = ""
    album = ""
    author = ""
    resolved_title = title.strip()
    resolved_artist = artist.strip()

    if video_id:
        track, db_artist, album, db_title, author = _lookup_music_identity(video_id)
        if not track and not db_artist and not db_title:
            track, db_artist, album, db_title, author = await _lookup_music_identity_from_api(video_id)
        if not resolved_title:
            resolved_title = db_title
        if not resolved_artist:
            resolved_artist = db_artist or author
        if not track:
            track = _clean_music_hint(db_title or resolved_title)
        if not resolved_artist:
            resolved_artist = db_artist or author

    query = _fallback_query(track, resolved_artist, resolved_title, author)
    if not query:
        raise HTTPException(status_code=404, detail="Not enough metadata for fallback search")

    best = await bandcamp_lookup(
        query,
        track=track,
        artist=resolved_artist,
        title=resolved_title,
        author=author,
        limit=3,
    )
    if not best:
        raise HTTPException(status_code=404, detail="No Bandcamp fallback found")

    return {
        "query": query,
        "track": best.get("track") or track,
        "artist": best.get("artist") or resolved_artist,
        "album": best.get("album") or album,
        "bandcamp_url": best.get("url", ""),
        "bandcamp_audio_url": best.get("audio_url", ""),
        "bandcamp_embed_url": best.get("embed_url", ""),
        "bandcamp_track_id": best.get("track_id", ""),
        "source": "bandcamp",
    }


@router.get("/recommendations/bandcamp")
async def music_bandcamp_sidebar_album_recommendations(
    url: str = Query(
        ...,
        min_length=1,
        description="Bandcamp album or track page URL (sidebar “you may also like”).",
    ),
):
    """Scrape Bandcamp sidebar recommendations and return ``MusicAlbumGroup`` rows for the album page."""
    rows = await _bandcamp_sidebar_recommendations(url.strip())
    albums: list[dict] = []
    for row in rows:
        group = _build_bandcamp_sidebar_recommendation_album_group(row)
        if group:
            albums.append(group)
    return {"albums": albums}


@router.get("/{video_id}/recognize")
async def music_recognize(video_id: str):
    try:
        data = await api_get(f"/videos/{video_id}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    rec = await recognize(data)
    return rec.to_dict()


def _bandcamp_seed_url_from_description(text: str) -> str:
    """First Bandcamp release URL in a video description (prefers ``/album/`` links)."""
    if not (text or "").strip():
        return ""
    preferred: list[str] = []
    fallback: list[str] = []
    for m in re.finditer(r"https?://[^\s)\]}>\"']+", text):
        url = m.group(0).rstrip(".,);")
        if "bandcamp.com" not in url.lower():
            continue
        u = url.split("?", 1)[0].rstrip("/")
        if "/album/" in u:
            preferred.append(u)
        else:
            fallback.append(u)
    if preferred:
        return preferred[0]
    return fallback[0] if fallback else ""


async def _bandcamp_sidebar_recommendations(seed_url: str) -> list[dict]:
    """Bandcamp album/track page sidebar → catalog rows, with YouTube attach when possible."""
    if not (seed_url or "").strip():
        return []
    try:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(
            None, get_shared_bandcamp_recommender().get_recommendations, seed_url.strip()
        )
        shaped = bandcamp_sidebar_to_music_recommendation_rows(raw)
        return list(await asyncio.gather(*[resolve_bandcamp_recommendation_row(r) for r in shaped]))
    except Exception:
        logger.debug("bandcamp sidebar recommendations failed", exc_info=True)
        return []


@router.get("/{video_id}/recommendations")
async def music_recommendations(
    video_id: str,
    bandcamp_url: str | None = Query(
        default=None,
        description="Optional Bandcamp seed URL when the upstream video payload omits the description.",
    ),
):
    try:
        data = await api_get(f"/videos/{video_id}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    rec = await recognize(data)
    desc = data.get("description") or ""
    seed_from_desc = ((bandcamp_url or "").strip() or _bandcamp_seed_url_from_description(desc)).strip()

    if not rec.is_music:
        if not seed_from_desc:
            raise HTTPException(status_code=422, detail="Video not identified as music")
        rows = await _bandcamp_sidebar_recommendations(seed_from_desc)
        return dedupe_music_recommendation_rows(rows)

    track = rec.track or data.get("title", "")
    artist = rec.artist or data.get("author", "")
    if not track or not artist:
        if seed_from_desc:
            rows = await _bandcamp_sidebar_recommendations(seed_from_desc)
            return dedupe_music_recommendation_rows(rows)
        raise HTTPException(status_code=422, detail="Could not determine track/artist")
    album = (rec.album or data.get("album") or "").strip()

    same_artist, catalog = await asyncio.gather(
        get_same_artist_catalog_tracks(artist, album, track, limit=7),
        get_recommendations(track, artist, limit=12),
    )

    bandcamp_rows: list[dict] = []
    try:
        # Prefer explicit description / query seed, then catalog search for an album URL.
        seed_url = ""
        if seed_from_desc:
            seed_url = seed_from_desc.split("?", 1)[0].rstrip("/")
        if not seed_url:
            query = " ".join(x for x in (artist, album) if x).strip() or f"{artist} {track}".strip()
            albums = await bandcamp_search_albums(query, limit=6)
            for row in albums:
                u = (row.get("bandcamp_url") or "").strip()
                if "/album/" in u:
                    seed_url = u.split("?", 1)[0]
                    break
        if seed_url:
            bandcamp_rows = await _bandcamp_sidebar_recommendations(seed_url)
    except Exception:
        logger.debug("bandcamp sidebar recommendations skipped", exc_info=True)

    seen_bc: set[str] = {r.get("bandcamp_url", "") for r in catalog if r.get("bandcamp_url")}
    merged_bc: list[dict] = []
    for row in bandcamp_rows:
        u = row.get("bandcamp_url") or ""
        if u and u in seen_bc:
            continue
        if u:
            seen_bc.add(u)
        merged_bc.append(row)

    combined = list(same_artist) + list(merged_bc) + list(catalog)
    return dedupe_music_recommendation_rows(combined)


@router.get("/{video_id}/lyrics")
async def music_lyrics(
    video_id: str,
    track: str | None = None,
    artist: str | None = None,
    album: str | None = None,
):
    """Optional query params override identity (e.g. current virtual track inside a full-album upload)."""
    if track or artist:
        query_track = (track or "").strip()
        query_artist = (artist or "").strip()
        query_album = (album or "").strip()
    else:
        q_track, q_artist, q_album, title, author = _lookup_music_identity(video_id)
        if not q_track and not q_artist:
            q_track, q_artist, q_album, title, author = await _lookup_music_identity_from_api(video_id)

        query_track = q_track or _clean_music_hint(title)
        query_artist = q_artist or _clean_music_hint(author)
        query_album = (q_album or "").strip()

    lyrics = await _fetch_lrclib_lyrics(query_track, query_artist, query_album)
    if not lyrics:
        raise HTTPException(status_code=404, detail="Lyrics not found")

    return lyrics


# ── Jobs ──────────────────────────────────────────────────────────────────────

class JobCreate(BaseModel):
    playlist_id: int


class MusicTagMove(BaseModel):
    parent_id: int | None = None
    position: int = 0
    group_id: int | None = None
    kind: str | None = None


class MusicTagCreate(BaseModel):
    name: str
    parent_id: int | None = None
    group_id: int | None = None
    kind: str = "new"


class MusicTagMerge(BaseModel):
    target_id: int
    preserve_source: bool = True


class MusicTagGroupCreate(BaseModel):
    name: str


class MusicTagGroupUpdate(BaseModel):
    name: str


class MusicLibraryManualBody(BaseModel):
    video_id: str
    tag_id: int | None = None
    tag_ids: list[int] | None = None
    # Optional metadata from the frontend — when provided the backend skips the Invidious fetch
    title: str | None = None
    thumbnail: str | None = None
    duration: int | None = None
    author: str | None = None
    author_id: str | None = None


@router.post("/jobs")
async def create_job(body: JobCreate):
    conn = get_db()
    pl = conn.execute("SELECT id FROM playlists WHERE id=?", (body.playlist_id,)).fetchone()
    conn.close()
    if not pl:
        raise HTTPException(status_code=404, detail="Playlist not found")
    job_id = await music_worker.submit_job(body.playlist_id)
    return {"id": job_id, "status": "pending"}


@router.get("/jobs")
async def list_jobs():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM music_jobs ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get("/jobs/{job_id}")
async def get_job(job_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM music_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return dict(row)


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: int):
    conn = get_db()
    conn.execute("DELETE FROM music_jobs WHERE id=?", (job_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ── Library ───────────────────────────────────────────────────────────────────

@router.get("/tags")
async def tags():
    return {
        "groups": list_music_tag_groups(),
        "tags": list_music_tags(),
    }


@router.post("/tag-groups")
async def create_tag_group(body: MusicTagGroupCreate):
    group_id = create_music_tag_group(body.name)
    return {"id": group_id}


@router.put("/tag-groups/{group_id}")
async def update_tag_group(group_id: int, body: MusicTagGroupUpdate):
    update_music_tag_group(group_id, body.name)
    return {"ok": True}


@router.post("/tags")
async def create_tag(body: MusicTagCreate):
    tag_id = create_music_tag(body.name, body.parent_id, body.group_id, body.kind)
    return {"id": tag_id}


@router.post("/tags/{tag_id}/move")
async def move_tag(tag_id: int, body: MusicTagMove):
    move_music_tag(tag_id, body.parent_id, body.position, body.group_id, body.kind)
    return {"ok": True}


@router.post("/tags/{tag_id}/merge")
async def merge_tag(tag_id: int, body: MusicTagMerge):
    merge_music_tag(tag_id, body.target_id, body.preserve_source)
    return {"ok": True}


class MusicTagRename(BaseModel):
    name: str


@router.put("/tags/{tag_id}")
async def rename_tag(tag_id: int, body: MusicTagRename):
    rename_music_tag(tag_id, body.name)
    return {"ok": True}


@router.delete("/tags/{tag_id}")
async def delete_tag(tag_id: int):
    delete_music_tag(tag_id)
    return {"ok": True}


@router.get("/genres")
async def list_genres():
    conn = get_db()
    rows = conn.execute(
        "SELECT genre, COUNT(*) as cnt FROM music_library WHERE genre IS NOT NULL GROUP BY genre ORDER BY cnt DESC"
    ).fetchall()
    conn.close()
    return [r["genre"] for r in rows]


class MusicLibraryGenreBody(BaseModel):
    genre: str | None = None


@router.put("/library/{video_id}/genre")
async def put_music_library_genre(video_id: str, body: MusicLibraryGenreBody):
    """Persist a user genre label on `music_library` (creates a row if needed)."""
    vid = (video_id or "").strip()
    if not vid:
        raise HTTPException(status_code=422, detail="video_id is required")
    set_music_library_genre(vid, body.genre)
    return {"ok": True}


@router.post("/library/manual")
async def music_library_manual_assign(body: MusicLibraryManualBody):
    """Upsert a music_library row and attach one or more meta-tags (genres/moods)."""
    raw_ids: list[int] = []
    if body.tag_ids:
        raw_ids.extend(body.tag_ids)
    if body.tag_id is not None:
        raw_ids.append(body.tag_id)
    seen: set[int] = set()
    tag_ids: list[int] = []
    for tid in raw_ids:
        if tid not in seen:
            seen.add(tid)
            tag_ids.append(tid)
    if not tag_ids:
        raise HTTPException(status_code=422, detail="Provide tag_id or tag_ids")

    video_id = (body.video_id or "").strip()
    if not video_id:
        raise HTTPException(status_code=422, detail="video_id is required")

    if body.title and body.title.strip() and body.title.strip() != video_id:
        meta = {
            "title": body.title.strip(),
            "thumbnail": body.thumbnail,
            "duration": body.duration,
            "author": (body.author or "").strip(),
            "author_id": (body.author_id or "").strip(),
        }
    else:
        meta = await _fetch_music_library_meta(video_id, retries=3)
        if not meta:
            raise HTTPException(status_code=404, detail="Could not fetch video metadata from Invidious")

    conn = get_db()
    try:
        manual_upsert_music_library_row(
            conn,
            video_id=video_id,
            title=meta["title"],
            thumbnail=meta.get("thumbnail"),
            duration=meta.get("duration"),
            author=meta.get("author"),
            author_id=meta.get("author_id"),
        )
        manual_assign_music_tags(conn, video_id, tag_ids)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"ok": True, "video_id": video_id, "tag_ids": tag_ids}


def _music_library_tag_exists_sql(tag_ids: list[int]) -> tuple[str, list]:
    placeholders = ",".join("?" for _ in tag_ids)
    sql = f"""
        EXISTS (
            SELECT 1
            FROM music_tag_assignments mta
            WHERE (mta.video_id = ml.video_id OR mta.video_id = ml.source_video_id)
              AND mta.tag_id IN ({placeholders})
        )
    """
    return sql, tag_ids


@router.get("/library/artists")
async def music_library_artists(
    q: str = Query(""),
    tag_id: int = Query(None),
    include_descendants: bool = Query(True),
    sort: str = Query("tracks"),
    limit: int = Query(200, ge=1, le=500),
):
    """Artists aggregated from saved library tracks (not only followed artists)."""
    sort_norm = sort.strip().lower()
    if sort_norm not in ("name", "tracks", "plays", "recent"):
        raise HTTPException(status_code=422, detail="sort must be 'name', 'tracks', 'plays', or 'recent'")

    conn = get_db()
    try:
        filters: list[str] = []
        params: list = []

        if tag_id is not None:
            sync_tag_playlist_content(conn, tag_id)
            conn.commit()
            tag_ids = get_music_tag_descendant_ids(conn, tag_id) if include_descendants else [tag_id]
            if not tag_ids:
                return {"artists": []}
            frag, extra = _music_library_tag_exists_sql(tag_ids)
            filters.append(frag)
            params.extend(extra)

        q_clean = q.strip()
        if q_clean:
            pat = f"%{q_clean}%"
            filters.append(
                "("
                "COALESCE(NULLIF(ml.artist, ''), NULLIF(ml.author, '')) LIKE ?"
                " OR COALESCE(ml.track, '') LIKE ?"
                " OR COALESCE(ml.title, '') LIKE ?"
                ")"
            )
            params.extend([pat, pat, pat])

        filters.append(
            "COALESCE(NULLIF(ml.artist, ''), NULLIF(ml.author, '')) IS NOT NULL "
            "AND TRIM(COALESCE(NULLIF(ml.artist, ''), NULLIF(ml.author, ''))) != ''"
        )

        where = "WHERE " + " AND ".join(filters) if filters else ""

        if sort_norm == "name":
            order_by = "artist COLLATE NOCASE ASC"
        elif sort_norm == "plays":
            order_by = "listens DESC, artist COLLATE NOCASE ASC"
        elif sort_norm == "recent":
            order_by = "last_played DESC, artist COLLATE NOCASE ASC"
        else:
            order_by = "track_count DESC, listens DESC, artist COLLATE NOCASE ASC"

        rows = conn.execute(
            f"""
            SELECT
                LOWER(TRIM(COALESCE(NULLIF(ml.artist, ''), NULLIF(ml.author, '')))) AS artist_key,
                COALESCE(NULLIF(ml.artist, ''), NULLIF(ml.author, '')) AS artist,
                MAX(ml.thumbnail) AS thumbnail,
                COUNT(*) AS track_count,
                COALESCE(SUM(COALESCE(h.listen_count, 0)), 0) AS listens,
                MAX(COALESCE(h.watched_at, ml.added_at)) AS last_played
            FROM music_library ml
            LEFT JOIN music_jobs mj ON mj.id = ml.source_job_id
            LEFT JOIN watch_history h ON h.video_id = ml.video_id
            {where}
            GROUP BY artist_key, artist
            ORDER BY {order_by}
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
    finally:
        conn.close()

    return {"artists": [dict(r) for r in rows]}


@router.get("/library/albums")
async def music_library_albums(
    q: str = Query(""),
    tag_id: int = Query(None),
    include_descendants: bool = Query(True),
    sort: str = Query("tracks"),
    limit: int = Query(200, ge=1, le=500),
):
    """Albums aggregated from saved library tracks (not only watch-history stats)."""
    sort_norm = sort.strip().lower()
    if sort_norm not in ("name", "tracks", "plays", "recent"):
        raise HTTPException(status_code=422, detail="sort must be 'name', 'tracks', 'plays', or 'recent'")

    conn = get_db()
    try:
        filters: list[str] = []
        params: list = []

        if tag_id is not None:
            sync_tag_playlist_content(conn, tag_id)
            conn.commit()
            tag_ids = get_music_tag_descendant_ids(conn, tag_id) if include_descendants else [tag_id]
            if not tag_ids:
                return {"albums": []}
            frag, extra = _music_library_tag_exists_sql(tag_ids)
            filters.append(frag)
            params.extend(extra)

        q_clean = q.strip()
        if q_clean:
            pat = f"%{q_clean}%"
            filters.append(
                "("
                "COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, '')) LIKE ?"
                " OR COALESCE(NULLIF(at.album_artist, ''), NULLIF(ml.artist, ''), ml.author) LIKE ?"
                " OR COALESCE(ml.track, '') LIKE ?"
                ")"
            )
            params.extend([pat, pat, pat])

        filters.append(
            "COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, '')) IS NOT NULL "
            "AND TRIM(COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, ''))) != ''"
        )

        where = "WHERE " + " AND ".join(filters)

        _title_expr = "COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, ''))"
        if sort_norm == "name":
            order_by = f"{_title_expr} COLLATE NOCASE ASC, artist COLLATE NOCASE ASC"
        elif sort_norm == "plays":
            order_by = f"listens DESC, {_title_expr} COLLATE NOCASE ASC"
        elif sort_norm == "recent":
            order_by = f"last_played DESC, {_title_expr} COLLATE NOCASE ASC"
        else:
            order_by = f"track_count DESC, listens DESC, {_title_expr} COLLATE NOCASE ASC"

        rows = conn.execute(
            f"""
            SELECT
                LOWER(TRIM(COALESCE(NULLIF(at.album_artist, ''), NULLIF(ml.artist, ''), ml.author, '')))
                  || '::' ||
                LOWER(TRIM(COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, ''), ''))) AS album_key,
                COALESCE(NULLIF(at.album_title, ''), NULLIF(ml.album, '')) AS title,
                COALESCE(NULLIF(at.album_artist, ''), NULLIF(ml.artist, ''), ml.author) AS artist,
                COALESCE(MAX(ar.cover_art), MAX(ml.thumbnail)) AS thumbnail,
                COALESCE(SUM(COALESCE(h.listen_count, 0)), 0) AS listens,
                COUNT(DISTINCT ml.video_id) AS track_count,
                MAX(COALESCE(h.watched_at, ml.added_at)) AS last_played
            FROM music_library ml
            LEFT JOIN album_tracks at ON at.video_id = ml.video_id
            LEFT JOIN watch_history h ON h.video_id = ml.video_id
            LEFT JOIN album_ratings ar ON ar.album_key = at.album_key
            LEFT JOIN music_jobs mj ON mj.id = ml.source_job_id
            {where}
            GROUP BY 1, 2, 3
            ORDER BY {order_by}
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()
    finally:
        conn.close()

    return {"albums": [dict(r) for r in rows]}


@router.get("/library")
async def music_library(
    genre: str = Query(None),
    job_id: int = Query(None),
    playlist_id: int = Query(None),
    tag_id: int = Query(None),
    include_descendants: bool = Query(True),
    kind: str = Query(None),
    source_video_id: str = Query(None),
    page: int = Query(1),
    per_page: int = Query(40),
    q: str = Query(None),
    sort: str = Query(None),
):
    if kind not in (None, "seed", "recommended"):
        raise HTTPException(status_code=422, detail="kind must be 'seed' or 'recommended'")

    offset = (page - 1) * per_page
    conn = get_db()
    filters = []
    params: list = []
    if genre:
        filters.append("ml.genre=?")
        params.append(genre)
    if job_id is not None:
        filters.append("ml.source_job_id=?")
        params.append(job_id)
    if playlist_id is not None:
        filters.append("mj.playlist_id=?")
        params.append(playlist_id)
    if tag_id is not None:
        sync_tag_playlist_content(conn, tag_id)
        conn.commit()
        tag_ids = get_music_tag_descendant_ids(conn, tag_id) if include_descendants else [tag_id]
        if not tag_ids:
            conn.close()
            return {"items": [], "total": 0, "page": page, "per_page": per_page}
        placeholders = ",".join("?" for _ in tag_ids)
        filters.append(
            f"""
            EXISTS (
                SELECT 1
                FROM music_tag_assignments mta
                WHERE (mta.video_id = ml.video_id OR mta.video_id = ml.source_video_id)
                  AND mta.tag_id IN ({placeholders})
            )
            """
        )
        params.extend(tag_ids)
    if source_video_id:
        filters.append("ml.source_video_id=?")
        params.append(source_video_id)
    if kind == "seed":
        filters.append("ml.source_video_id IS NOT NULL")
        filters.append("ml.video_id = ml.source_video_id")
    elif kind == "recommended":
        filters.append("ml.source_video_id IS NOT NULL")
        filters.append("ml.video_id != ml.source_video_id")
        # Don't re-suggest tracks the user has already rated.
        filters.append(
            "NOT EXISTS (SELECT 1 FROM video_ratings vr_existing WHERE vr_existing.video_id = ml.video_id)"
        )

    q_clean = (q or "").strip()
    if q_clean:
        pat = f"%{q_clean}%"
        filters.append(
            "("
            "COALESCE(ml.track,'') LIKE ? OR COALESCE(ml.title,'') LIKE ? OR "
            "COALESCE(ml.artist,'') LIKE ? OR COALESCE(ml.album,'') LIKE ? OR COALESCE(ml.author,'') LIKE ?"
            ")"
        )
        params.extend([pat, pat, pat, pat, pat])

    sort_norm = (sort or "rating").strip().lower()
    if sort_norm not in ("rating", "added", "artist", "album", "plays"):
        conn.close()
        raise HTTPException(status_code=422, detail="sort must be 'rating', 'added', 'artist', 'album', or 'plays'")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    stable_shuffle = """
        (
            COALESCE(unicode(substr(ml.video_id, 1, 1)), 0) * 97 +
            COALESCE(unicode(substr(ml.video_id, 2, 1)), 0) * 193 +
            COALESCE(unicode(substr(ml.video_id, 3, 1)), 0) * 389 +
            COALESCE(unicode(substr(ml.video_id, 4, 1)), 0) * 769 +
            COALESCE(unicode(substr(COALESCE(ml.source_video_id, ml.video_id), 1, 1)), 0) * 1543
        )
    """
    if sort_norm == "added":
        order_clause = "ml.added_at DESC, ml.id DESC"
    elif sort_norm == "artist":
        order_clause = (
            "LOWER(TRIM(COALESCE(NULLIF(ml.artist, ''), ml.author, ''))) COLLATE NOCASE ASC, ml.added_at DESC"
        )
    elif sort_norm == "album":
        order_clause = "LOWER(TRIM(COALESCE(ml.album, ''))) COLLATE NOCASE ASC, ml.added_at DESC"
    elif sort_norm == "plays":
        order_clause = (
            "COALESCE((SELECT h.listen_count FROM watch_history h WHERE h.video_id = ml.video_id), 0) DESC, "
            "ml.added_at DESC"
        )
    else:
        order_clause = f"""
            CASE WHEN COALESCE(vr.rating, ar.rating, cr.rating) IS NULL THEN 1 ELSE 0 END,
            CAST(COALESCE(vr.rating, ar.rating, cr.rating) AS INTEGER) DESC,
            {stable_shuffle},
            ml.added_at DESC
        """
    rows = conn.execute(
        f"""
        SELECT
            ml.*,
            CASE
                WHEN ml.source_video_id IS NOT NULL AND ml.video_id != ml.source_video_id THEN 'recommended'
                ELSE 'seed'
            END AS relation_kind,
            src.track AS source_track,
            src.artist AS source_artist,
            src.title AS source_title,
            mj.playlist_id AS source_playlist_id,
            mj.playlist_title AS source_playlist_title,
            CAST(vr.rating AS INTEGER) AS video_rating,
            CAST(ar.rating AS INTEGER) AS album_rating,
            CAST(cr.rating AS INTEGER) AS channel_rating,
            CAST(COALESCE(vr.rating, ar.rating, cr.rating) AS INTEGER) AS effective_rating,
            COALESCE((SELECT h.listen_count FROM watch_history h WHERE h.video_id = ml.video_id), 0) AS listen_count
        FROM music_library ml
        LEFT JOIN music_library src ON src.video_id = ml.source_video_id
        LEFT JOIN music_jobs mj ON mj.id = ml.source_job_id
        LEFT JOIN video_ratings vr ON vr.video_id = ml.video_id
        LEFT JOIN album_tracks at ON at.video_id = ml.video_id
        LEFT JOIN album_ratings ar ON ar.album_key = at.album_key
        LEFT JOIN channel_ratings cr ON cr.channel_id = ml.author_id
        {where}
        ORDER BY {order_clause}
        LIMIT ? OFFSET ?
        """,
        params + [per_page, offset]
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM music_library ml LEFT JOIN music_jobs mj ON mj.id = ml.source_job_id {where}",
        params,
    ).fetchone()[0]
    conn.close()
    items = await _enrich_music_library_rows(rows)
    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.delete("/library/{video_id}")
async def delete_music_library_video(video_id: str, playlist_id: int = Query(None)):
    result = delete_music_library_item(video_id, playlist_id)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="Music library item not found")
    return {"ok": True, **result}
