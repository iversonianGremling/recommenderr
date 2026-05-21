"""Fetch YouTube channel uploads via the official Atom feed (RSS). Used when Invidious is unavailable."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from xml.etree import ElementTree as ET

import httpx

from backend.services.invidious_client import INVIDIOUS_URL

logger = logging.getLogger(__name__)

YOUTUBE_RSS_URL = "https://www.youtube.com/feeds/videos.xml"
RSS_TIMEOUT_SECONDS = 15.0

_RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=RSS_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "ytfrontend/1.0 (subscription refresh)"},
        )
    return _client


def _parse_timestamp(value: str | None) -> int:
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


def _parse_feed_xml(
    xml_text: str, channel_id: str, default_channel_name: str
) -> tuple[str, list[dict[str, Any]]]:
    root = ET.fromstring(xml_text)
    feed_title = (
        root.findtext("atom:title", default=default_channel_name, namespaces=_RSS_NS)
        or default_channel_name
    )
    videos: list[dict[str, Any]] = []

    for entry in root.findall("atom:entry", _RSS_NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=_RSS_NS)
        if not video_id:
            continue

        thumb_el = entry.find("media:group/media:thumbnail", _RSS_NS)
        thumb_url = thumb_el.attrib.get("url") if thumb_el is not None else ""
        if not thumb_url:
            thumb_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        published = _parse_timestamp(
            entry.findtext("atom:published", default="", namespaces=_RSS_NS)
            or entry.findtext("atom:updated", default="", namespaces=_RSS_NS)
        )

        videos.append(
            {
                "videoId": video_id,
                "title": entry.findtext("atom:title", default=video_id, namespaces=_RSS_NS),
                "published": published,
                "videoThumbnails": [{"url": thumb_url}],
                "viewCount": None,
                "lengthSeconds": None,
            }
        )

    return feed_title, videos


async def _fetch_rss_xml(
    channel_id: str, default_channel_name: str, url: str, *, params: dict | None = None
) -> tuple[dict[str, Any] | None, BaseException | None]:
    try:
        resp = await _get_client().get(url, params=params or {})
        resp.raise_for_status()
    except Exception as exc:
        return None, exc

    try:
        channel_title, videos = _parse_feed_xml(resp.text, channel_id, default_channel_name)
    except ET.ParseError as exc:
        return None, exc

    thumb: str | None = None
    if videos:
        vt = videos[0].get("videoThumbnails") or []
        if vt:
            thumb = vt[0].get("url")

    return (
        {
            "channel_name": channel_title,
            "thumbnail": thumb,
            "videos": videos,
        },
        None,
    )


async def fetch_channel_videos_rss(
    channel_id: str, default_channel_name: str
) -> dict[str, Any] | None:
    """
    Fetch channel uploads via Atom/RSS. Tries YouTube's feed and Invidious's proxied feed in parallel;
    Invidious may succeed when the container cannot reach YouTube directly.

    Returns Invidious-shaped video list plus resolved channel title and a thumbnail URL, or None if both fail.
    """
    yt_task = _fetch_rss_xml(
        channel_id,
        default_channel_name,
        YOUTUBE_RSS_URL,
        params={"channel_id": channel_id},
    )
    inv_url = f"{INVIDIOUS_URL.rstrip('/')}/feed/channel/{channel_id}"
    iv_task = _fetch_rss_xml(channel_id, default_channel_name, inv_url)

    yt_result, iv_result = await asyncio.gather(yt_task, iv_task)

    yt_data, yt_err = yt_result
    iv_data, iv_err = iv_result

    if yt_err is not None:
        logger.info("YouTube RSS fetch failed for %s: %s", channel_id, yt_err)
    if iv_err is not None:
        logger.info("Invidious RSS fetch failed for %s: %s", channel_id, iv_err)

    def n_v(d: dict[str, Any] | None) -> int:
        return len((d or {}).get("videos") or [])

    # Prefer direct YouTube when it has entries; otherwise use Invidious-hosted feed.
    if yt_data and n_v(yt_data) > 0:
        return yt_data
    if iv_data and n_v(iv_data) > 0:
        return iv_data
    if yt_data:
        return yt_data
    if iv_data:
        return iv_data
    return None
