"""Bandcamp album-page 'you may also like' scraping (HTML).

Bandcamp does not publish a public REST API for search or recommendations; this
module fetches public album pages only. Rate limits apply to this client's
HTTPS session (default 5 requests/minute via ``requests_ratelimiter``).

Search and ``data-tralbum`` parsing for catalog metadata live in
``services.music_client`` (``bandcamp_search*``, ``bandcamp_album_details``).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any
from urllib.parse import urljoin

import bs4
import requests
from requests.adapters import HTTPAdapter
from requests_ratelimiter import LimiterAdapter
from urllib3.util import create_urllib3_context

logger = logging.getLogger("bandcamp-recommender")

_BY_GLUE_RE = re.compile(r"(?i)([\w\)\]\"\'»])(by)\s+")

_shared: "BandcampRecommender | None" = None


def get_shared_bandcamp_recommender() -> "BandcampRecommender":
    """Process-wide recommender so per-minute caps apply across requests."""
    global _shared
    if _shared is None:
        rpm = int(os.getenv("BANDCAMP_RECOMMEND_RPM", "5"))
        _shared = BandcampRecommender(limit_req_per_minute=rpm)
    return _shared


class SSLAdapter(HTTPAdapter):
    def __init__(self, ssl_context=None, **kwargs):
        self.ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)


class BandcampRecommender:
    def __init__(self, limit_req_per_minute: int = 5):
        """Initialize with optional HTTPS rate limiting (0 disables)."""
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36"
            )
        }
        self.logger = logger
        self.session = requests.Session()

        ctx = create_urllib3_context()
        ctx.load_default_certs()
        default_ciphers = ":".join(
            [
                "ECDHE+AESGCM",
                "ECDHE+CHACHA20",
                "DHE+AESGCM",
                "DHE+CHACHA20",
                "ECDH+AESGCM",
                "DH+AESGCM",
                "ECDH+AES",
                "DH+AES",
                "RSA+AESGCM",
                "RSA+AES",
                "!aNULL",
                "!eNULL",
                "!MD5",
                "!DSS",
            ]
        )
        ctx.set_ciphers(default_ciphers)
        ssl_adapter = SSLAdapter(ssl_context=ctx)
        self.session.mount("https://", ssl_adapter)

        if limit_req_per_minute > 0:
            rate_adapter = LimiterAdapter(per_minute=limit_req_per_minute)
            self.session.mount("https://", rate_adapter)

    @staticmethod
    def _normalize_glued_by(text: str) -> str:
        """Insert a missing space before ``by`` when Bandcamp concatenates title+artist."""
        if not text:
            return ""
        return _BY_GLUE_RE.sub(r"\1 \2 ", text).strip()

    @staticmethod
    def _split_album_artist(combined: str) -> tuple[str, str | None]:
        combined = BandcampRecommender._normalize_glued_by(combined)
        m = re.search(r"(?i)\s+by\s+", combined)
        if m:
            album = combined[: m.start()].strip()
            artist = combined[m.end() :].strip()
            return album, artist or None
        return combined.strip(), None

    def get_recommendations(self, url: str) -> list[dict[str, Any]]:
        """Extract album/track recommendations from a Bandcamp album page sidebar."""
        try:
            response = self.session.get(url, headers=self.headers, timeout=10)
        except requests.exceptions.RequestException as exc:
            self.logger.error("Could not fetch page %s: %s", url, exc)
            return []

        if not response.ok:
            self.logger.debug("Status code for %s: %s", url, response.status_code)
            return []

        try:
            soup = bs4.BeautifulSoup(response.text, "lxml")
        except bs4.FeatureNotFound:
            soup = bs4.BeautifulSoup(response.text, "html.parser")

        rec_container = soup.find("div", {"class": "recommendations-container"})
        if not rec_container:
            self.logger.debug("No recommendations container found for %s", url)
            return []

        recommendations: list[dict[str, Any]] = []
        for item in rec_container.find_all("li"):
            link = item.find("a")
            if not link or not link.get("href"):
                continue

            rec_url = urljoin(url, link["href"])
            raw_title = (link.get("title") or "").strip() or link.get_text(
                separator=" ", strip=True
            )
            raw_title = self._normalize_glued_by(raw_title)
            artist_elem = item.find("span", {"class": "artist"})
            span_artist = artist_elem.text.strip() if artist_elem else None
            album_part, by_artist = self._split_album_artist(raw_title)
            artist = span_artist or by_artist
            title = album_part or raw_title
            recommendations.append({"title": title, "url": rec_url, "artist": artist})

        self.logger.info("Found %d recommendations for %s", len(recommendations), url)
        return recommendations

    def get_bulk_recommendations(
        self, urls: list[str], delay_seconds: float = 6
    ) -> dict[str, list[dict[str, Any]]]:
        results: dict[str, list[dict[str, Any]]] = {}
        for idx, url in enumerate(urls):
            self.logger.info("Processing %d/%d: %s", idx + 1, len(urls), url)
            results[url] = self.get_recommendations(url)
            if idx < len(urls) - 1:
                time.sleep(delay_seconds)
        return results


def bandcamp_sidebar_to_music_recommendation_rows(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Shape sidebar dicts into the same keys as ``get_recommendations`` catalog rows."""
    out: list[dict[str, Any]] = []
    for i, it in enumerate(items):
        title_guess = (it.get("title") or "").strip()
        artist_guess = (it.get("artist") or "").strip()
        url = (it.get("url") or "").strip()
        clean_url = url.split("?", 1)[0] if url else ""
        album_title, split_art = BandcampRecommender._split_album_artist(title_guess)
        artist = (artist_guess or split_art or "").strip()
        track = album_title or title_guess
        display_bits = [b for b in (artist, track) if b]
        display = " — ".join(display_bits) if display_bits else title_guess
        out.append(
            {
                "track": track,
                "artist": artist,
                "album": "",
                "source": "bandcamp",
                "video_id": "",
                "title": display,
                "author": artist,
                "thumbnail": None,
                "lengthSeconds": None,
                "graph_score": round(0.9 - i * 0.03, 3),
                "bandcamp_url": clean_url,
            }
        )
    return out
