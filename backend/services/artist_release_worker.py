import asyncio
import logging
import os
import re
import time
from difflib import SequenceMatcher

from backend.db import (
    get_artist_follow,
    list_artist_follows,
    normalize_album_key,
    record_artist_release_event,
    save_artist_follow,
    save_artist_release_snapshot,
)
from backend.services.music_client import (
    deezer_get_artist_albums,
    deezer_search_artist,
    itunes_search_album,
    itunes_search_artist,
    spotify_get_artist_albums,
    spotify_search_artist,
)

logger = logging.getLogger("artist_release_worker")

CHECK_INTERVAL_SECONDS = max(900, int(os.getenv("ARTIST_RELEASE_CHECK_INTERVAL_SECONDS", "21600")))
STARTUP_DELAY_SECONDS = max(5, int(os.getenv("ARTIST_RELEASE_CHECK_STARTUP_DELAY_SECONDS", "20")))

_CHECK_LOCK = asyncio.Lock()
_YEAR_RE = re.compile(r"^(?P<year>\d{4})(?:-(?P<month>\d{2}))?(?:-(?P<day>\d{2}))?$")


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _artist_match_score(query: str, candidate: str | None) -> float:
    clean_query = _clean_text(query)
    clean_candidate = _clean_text(candidate)
    if not clean_query or not clean_candidate:
        return 0.0
    score = SequenceMatcher(None, clean_query, clean_candidate).ratio()
    if clean_query == clean_candidate:
        return 1.0
    if clean_query in clean_candidate or clean_candidate in clean_query:
        score = max(score, 0.94)
    return score


def _release_sort_key(item: dict) -> tuple[int, int, int]:
    raw = (item.get("release_date") or item.get("year") or "").strip()
    match = _YEAR_RE.match(raw)
    if not match:
        return (0, 0, 0)
    year = int(match.group("year") or 0)
    month = int(match.group("month") or 0)
    day = int(match.group("day") or 0)
    return (year, month, day)


def _source_priority(source: str | None) -> int:
    value = (source or "").strip().lower()
    if value == "spotify":
        return 3
    if value == "deezer":
        return 2
    if value == "itunes":
        return 1
    return 0


def _release_key(artist_name: str, release: dict) -> str:
    return normalize_album_key(release.get("title"), release.get("artist") or artist_name)


def _pick_best_search_hit(artist_name: str, items: list[dict], field: str) -> dict | None:
    ranked = sorted(
        items,
        key=lambda item: (
            _artist_match_score(artist_name, item.get(field)),
            _source_priority(item.get("source")),
        ),
        reverse=True,
    )
    if not ranked:
        return None
    best = ranked[0]
    if _artist_match_score(artist_name, best.get(field)) < 0.72:
        return None
    return best


async def _resolve_follow_identifiers(follow: dict) -> dict:
    artist_name = (follow.get("artist_name") or "").strip()
    if not artist_name:
        return follow

    spotify_items, deezer_items, itunes_items = await asyncio.gather(
        spotify_search_artist(artist_name, limit=5),
        deezer_search_artist(artist_name, limit=5),
        itunes_search_artist(artist_name, limit=5),
    )

    spotify_hit = _pick_best_search_hit(artist_name, spotify_items, "artist")
    deezer_hit = _pick_best_search_hit(artist_name, deezer_items, "artist")
    itunes_hit = _pick_best_search_hit(artist_name, itunes_items, "artist")

    return {
        **follow,
        "image": follow.get("image") or (spotify_hit or {}).get("image") or (deezer_hit or {}).get("image") or "",
        "source": follow.get("source") or (spotify_hit or {}).get("source") or (deezer_hit or {}).get("source") or (itunes_hit or {}).get("source") or "",
        "spotify_artist_id": follow.get("spotify_artist_id") or (spotify_hit or {}).get("spotify_artist_id") or "",
        "deezer_artist_id": follow.get("deezer_artist_id") or (deezer_hit or {}).get("deezer_artist_id") or "",
        "itunes_artist_id": follow.get("itunes_artist_id") or (itunes_hit or {}).get("itunes_artist_id") or "",
    }


def _filter_release_candidates(artist_name: str, items: list[dict]) -> list[dict]:
    filtered: list[dict] = []
    for item in items:
        if not item.get("title"):
            continue
        score = _artist_match_score(artist_name, item.get("artist"))
        if score < 0.72:
            continue
        filtered.append(item)
    return filtered


async def _fetch_artist_releases(follow: dict) -> list[dict]:
    artist_name = (follow.get("artist_name") or "").strip()
    if not artist_name:
        return []

    spotify_task = spotify_get_artist_albums(follow.get("spotify_artist_id"), limit=20) if follow.get("spotify_artist_id") else asyncio.sleep(0, result=[])
    deezer_task = deezer_get_artist_albums(follow.get("deezer_artist_id"), limit=20) if follow.get("deezer_artist_id") else asyncio.sleep(0, result=[])
    itunes_task = itunes_search_album(artist_name, limit=20)

    spotify_items, deezer_items, itunes_items = await asyncio.gather(
        spotify_task,
        deezer_task,
        itunes_task,
    )

    merged: dict[str, dict] = {}
    for batch in (
        _filter_release_candidates(artist_name, spotify_items),
        _filter_release_candidates(artist_name, deezer_items),
        _filter_release_candidates(artist_name, itunes_items),
    ):
        for item in batch:
            key = _release_key(artist_name, item)
            if not key:
                continue
            current = merged.get(key)
            if not current:
                merged[key] = {**item, "release_key": key}
                continue
            current_sort = _release_sort_key(current)
            next_sort = _release_sort_key(item)
            if next_sort > current_sort or (
                next_sort == current_sort
                and _source_priority(item.get("source")) > _source_priority(current.get("source"))
            ):
                merged[key] = {**current, **item, "release_key": key}
                continue
            if not current.get("cover_art") and item.get("cover_art"):
                current["cover_art"] = item.get("cover_art")
            if not current.get("release_date") and item.get("release_date"):
                current["release_date"] = item.get("release_date")

    return sorted(
        merged.values(),
        key=lambda item: (
            _release_sort_key(item),
            _source_priority(item.get("source")),
            item.get("title", "").lower(),
        ),
        reverse=True,
    )


async def check_followed_artist(follow_or_name: dict | str):
    follow = follow_or_name if isinstance(follow_or_name, dict) else get_artist_follow(follow_or_name)
    if not follow:
        return None

    artist_name = (follow.get("artist_name") or "").strip()
    if not artist_name:
        return None

    follow = await _resolve_follow_identifiers(follow)
    save_artist_follow(
        artist_name,
        image=follow.get("image"),
        source=follow.get("source"),
        spotify_artist_id=follow.get("spotify_artist_id"),
        deezer_artist_id=follow.get("deezer_artist_id"),
        itunes_artist_id=follow.get("itunes_artist_id"),
    )

    releases = await _fetch_artist_releases(follow)
    latest = releases[0] if releases else None
    created_event = False

    previous_release_key = (follow.get("last_release_key") or "").strip()
    if latest:
        latest_key = latest.get("release_key") or _release_key(artist_name, latest)
        if previous_release_key and latest_key and latest_key != previous_release_key:
            created_event = record_artist_release_event(
                artist_name,
                release_key=latest_key,
                title=latest.get("title") or "",
                release_date=latest.get("release_date") or latest.get("year") or "",
                cover_art=latest.get("cover_art") or "",
                source=latest.get("source") or "",
            )
        save_artist_release_snapshot(
            artist_name,
            release_key=latest_key,
            title=latest.get("title") or "",
            release_date=latest.get("release_date") or latest.get("year") or "",
            cover_art=latest.get("cover_art") or "",
            source=latest.get("source") or "",
        )
    else:
        save_artist_release_snapshot(
            artist_name,
            release_key=previous_release_key or None,
            title=follow.get("last_release_title"),
            release_date=follow.get("last_release_date"),
            cover_art=follow.get("last_release_cover_art"),
            source=follow.get("last_release_source"),
        )

    updated_follow = get_artist_follow(artist_name)
    return {
        "artist": updated_follow,
        "latest_release": latest,
        "created_event": created_event,
    }


async def check_followed_artists_once(limit: int | None = None):
    async with _CHECK_LOCK:
        follows = list_artist_follows(limit or 500)
        results = []
        for follow in follows:
            try:
                result = await check_followed_artist(follow)
                if result:
                    results.append(result)
            except Exception as exc:
                logger.warning("[artist_release_worker] failed to check %s: %s", follow.get("artist_name"), exc)
        return results


async def artist_release_worker():
    logger.info("Artist release worker started")
    await asyncio.sleep(STARTUP_DELAY_SECONDS)
    while True:
        started_at = time.time()
        try:
            await check_followed_artists_once()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("[artist_release_worker] unhandled error: %s", exc)

        elapsed = time.time() - started_at
        sleep_for = max(60, CHECK_INTERVAL_SECONDS - int(elapsed))
        try:
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            break
