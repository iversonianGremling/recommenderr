import json
import logging
import time
from typing import Any

from backend.db import get_db, get_subscriptions, get_ratings_for_video_ids, get_music_labeled_channel_ids

RSS_CACHE_TTL_SECONDS = 300
DEFAULT_CHANNEL_RATING = 5
DEFAULT_INTERVAL_DAYS = 7.0
MAX_FEED_AGE_DAYS = 45.0
MIN_INTERVAL_DAYS = 0.5

logger = logging.getLogger(__name__)
_cache: dict[str, Any] = {
    "signature": "",
    "fetched_at": 0.0,
    "videos": [],
    "represented_channels": 0,
}
_cache_music: dict[str, Any] = {
    "signature": "",
    "fetched_at": 0.0,
    "videos": [],
    "represented_channels": 0,
}


def invalidate_subscription_feed_cache() -> None:
    _cache["signature"] = ""
    _cache["fetched_at"] = 0.0
    _cache["videos"] = []
    _cache["represented_channels"] = 0
    _cache_music["signature"] = ""
    _cache_music["fetched_at"] = 0.0
    _cache_music["videos"] = []
    _cache_music["represented_channels"] = 0


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_channel_meta() -> dict[str, dict[str, Any]]:
    conn = get_db()
    rows = conn.execute("""
        SELECT s.channel_id,
               s.channel_name,
               COALESCE(CAST(cr.rating AS INTEGER), ?) AS rating,
               cs.avg_interval_days,
               cs.last_upload_at,
               cs.fetched_at,
               cs.recent_videos
        FROM subscriptions s
        LEFT JOIN channel_ratings cr ON cr.channel_id = s.channel_id
        LEFT JOIN channel_stats cs ON cs.channel_id = s.channel_id
    """, (DEFAULT_CHANNEL_RATING,)).fetchall()
    conn.close()

    meta: dict[str, dict[str, Any]] = {}
    for row in rows:
        recent_videos: list[dict[str, Any]] = []
        raw_recent = row["recent_videos"]
        if raw_recent:
            try:
                parsed = json.loads(raw_recent)
                if isinstance(parsed, list):
                    recent_videos = [item for item in parsed if isinstance(item, dict)]
            except json.JSONDecodeError:
                pass

        meta[row["channel_id"]] = {
            "channel_name": row["channel_name"],
            "rating": _as_int(row["rating"], DEFAULT_CHANNEL_RATING),
            "avg_interval_days": _as_float(row["avg_interval_days"], DEFAULT_INTERVAL_DAYS),
            "last_upload_at": _as_int(row["last_upload_at"], 0),
            "fetched_at": _as_int(row["fetched_at"], 0),
            "recent_videos": recent_videos,
        }

    return meta


def _uploads_per_week(avg_interval_days: float | None) -> float:
    if avg_interval_days is None:
        return 1.0
    return 7.0 / max(avg_interval_days, MIN_INTERVAL_DAYS)


def _channel_video_cap(rating: int, avg_interval_days: float | None) -> int:
    uploads_per_week = _uploads_per_week(avg_interval_days)

    if rating >= 9:
        return 20
    if rating == 8:
        return 8 if uploads_per_week <= 7.0 else 6
    if rating == 7:
        return 4 if uploads_per_week <= 4.0 else 3
    if rating == 6:
        return 2 if uploads_per_week <= 3.0 else 1
    return 1


def _rating_priority(rating: int) -> float:
    if rating <= 1:
        return 0.0
    if rating <= 4:
        return max(0.2, rating / 5.0)
    return float((rating - 4) ** 3)


def _video_rating_multiplier(video_rating: int | None) -> float:
    """Blend the user's per-video rating (when present) with channel-based scoring."""
    if video_rating is None:
        return 1.0
    r = int(video_rating)
    if r <= 1:
        return 0.35
    if r <= 5:
        return 0.55 + r * 0.09
    return 1.0 + (r - 5) * 0.07


def _score_video(
    item: dict[str, Any],
    meta: dict[str, Any],
    channel_slot: int,
    now: float,
    video_rating: int | None = None,
) -> float:
    rating = _as_int(meta.get("rating"), DEFAULT_CHANNEL_RATING)
    uploads_per_week = _uploads_per_week(_as_float(meta.get("avg_interval_days"), DEFAULT_INTERVAL_DAYS))
    published = _as_int(item.get("published"), 0)
    age_days = max(0.0, (now - published) / 86400.0) if published else MAX_FEED_AGE_DAYS

    # High-rated channels dominate, while frequent low-rated channels are damped.
    freshness = 2 ** (-age_days / 4.0)
    cadence_penalty = max(1.0, uploads_per_week) ** max(0.0, (9 - rating) / 4.0)
    slot_penalty = 1.0 + channel_slot * max(0.35, min(2.5, uploads_per_week / max(3.0, rating - 1.0)))
    base = _rating_priority(rating) * freshness / cadence_penalty / slot_penalty
    return base * _video_rating_multiplier(video_rating)


def _build_candidates(
    subscriptions: list[dict[str, Any]],
    channel_meta: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    now = time.time()
    max_age_seconds = int(MAX_FEED_AGE_DAYS * 86400)
    deduped: dict[str, dict[str, Any]] = {}
    represented_channels: set[str] = set()

    for sub in subscriptions:
        channel_id = sub["channel_id"]
        meta = channel_meta.get(channel_id, {})
        recent_videos = list(meta.get("recent_videos") or [])
        if not recent_videos:
            continue

        rating = _as_int(meta.get("rating"), DEFAULT_CHANNEL_RATING)
        avg_interval_days = _as_float(meta.get("avg_interval_days"), DEFAULT_INTERVAL_DAYS)
        recent_videos.sort(key=lambda item: _as_int(item.get("published"), 0), reverse=True)

        taken = 0
        for raw in recent_videos:
            if taken >= _channel_video_cap(rating, avg_interval_days):
                break

            video_id = str(raw.get("video_id") or raw.get("videoId") or "").strip()
            if not video_id:
                continue

            published = _as_int(raw.get("published"), _as_int(meta.get("last_upload_at"), 0))
            if published and (now - published) > max_age_seconds:
                continue

            thumb = raw.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
            candidate = {
                "videoId": video_id,
                "title": raw.get("title") or video_id,
                "author": meta.get("channel_name") or sub["channel_name"],
                "authorId": channel_id,
                "lengthSeconds": _as_int(raw.get("length_seconds") or raw.get("lengthSeconds"), 0) or None,
                "viewCount": _as_int(raw.get("view_count") or raw.get("viewCount"), 0) or None,
                "published": published,
                "videoThumbnails": [{"url": thumb}],
            }
            candidate["_channel_slot"] = taken
            existing = deduped.get(video_id)
            if (existing is None) or (_as_int(existing.get("published"), 0) < published):
                deduped[video_id] = candidate
            represented_channels.add(channel_id)
            taken += 1

    video_ids = list(deduped.keys())
    video_ratings = get_ratings_for_video_ids(video_ids) if video_ids else {}

    scored: list[dict[str, Any]] = []
    for item in deduped.values():
        meta = channel_meta.get(item["authorId"], {})
        scored_item = dict(item)
        vid = str(item.get("videoId") or "")
        vr = video_ratings.get(vid)
        scored_item["_score"] = _score_video(
            item,
            meta,
            _as_int(item.get("_channel_slot"), 0),
            now,
            video_rating=vr,
        )
        scored.append(scored_item)

    scored.sort(
        key=lambda item: (
            item.get("_score", 0.0),
            _as_int(item.get("published"), 0),
        ),
        reverse=True,
    )

    ordered = []
    for item in scored:
        ordered.append({key: value for key, value in item.items() if not key.startswith("_")})

    return ordered, len(represented_channels)


def _build_response(
    videos: list[dict[str, Any]],
    limit: int,
    offset: int,
    represented_channels: int,
    total_channels: int,
) -> dict[str, Any]:
    safe_limit = max(1, limit)
    total = len(videos)
    return {
        "videos": videos[offset:offset + safe_limit],
        "total": total,
        "limit": safe_limit,
        "offset": offset,
        "has_more": offset + safe_limit < total,
        "fetched_channels": represented_channels,
        # Legacy fields kept so older callers do not break during deployment.
        "total_channels": total_channels,
        "page": (offset // safe_limit) + 1,
        "per_page": safe_limit,
        "channels_loaded": represented_channels,
    }


async def get_subscription_feed(limit: int = 12, offset: int = 0, music_labeled_only: bool = False) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 60))
    safe_offset = max(0, offset)
    subscriptions = get_subscriptions()

    if not subscriptions:
        return _build_response([], safe_limit, safe_offset, 0, 0)

    signature = "|".join(sorted(sub["channel_id"] for sub in subscriptions))
    now = time.time()

    if music_labeled_only:
        music_ids = get_music_labeled_channel_ids()
        cache_sig = f"{signature}|m|{','.join(sorted(music_ids))}"
        if (
            _cache_music["signature"] == cache_sig
            and now - float(_cache_music["fetched_at"]) < RSS_CACHE_TTL_SECONDS
        ):
            filtered_count = len([s for s in subscriptions if s["channel_id"] in music_ids])
            return _build_response(
                list(_cache_music["videos"]),
                safe_limit,
                safe_offset,
                _as_int(_cache_music.get("represented_channels"), 0),
                filtered_count,
            )

        filtered = [s for s in subscriptions if s["channel_id"] in music_ids]
        if not filtered:
            return _build_response([], safe_limit, safe_offset, 0, 0)

        channel_meta = _load_channel_meta()
        merged, represented_channels = _build_candidates(filtered, channel_meta)

        if not merged:
            logger.warning(
                "music-labeled subscription feed empty: %s labeled channels, no recent cached videos",
                len(filtered),
            )

        _cache_music["signature"] = cache_sig
        _cache_music["fetched_at"] = now
        _cache_music["videos"] = merged
        _cache_music["represented_channels"] = represented_channels

        return _build_response(merged, safe_limit, safe_offset, represented_channels, len(filtered))

    if (
        _cache["signature"] == signature
        and now - float(_cache["fetched_at"]) < RSS_CACHE_TTL_SECONDS
    ):
        return _build_response(
            list(_cache["videos"]),
            safe_limit,
            safe_offset,
            _as_int(_cache.get("represented_channels"), 0),
            len(subscriptions),
        )

    channel_meta = _load_channel_meta()
    merged, represented_channels = _build_candidates(subscriptions, channel_meta)

    if not merged:
        logger.warning(
            "subscription feed is empty: no recent cached videos across %s subscriptions",
            len(subscriptions),
        )

    _cache["signature"] = signature
    _cache["fetched_at"] = now
    _cache["videos"] = merged
    _cache["represented_channels"] = represented_channels

    return _build_response(merged, safe_limit, safe_offset, represented_channels, len(subscriptions))
