import re
from dataclasses import dataclass, field
from backend.services.music_client import itunes_search, deezer_search


@dataclass
class MusicRecognition:
    is_music: bool
    confidence: float
    track: str | None = None
    artist: str | None = None
    album: str | None = None
    isrc: str | None = None
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "is_music": self.is_music,
            "confidence": round(self.confidence, 3),
            "track": self.track,
            "artist": self.artist,
            "album": self.album,
            "isrc": self.isrc,
            "sources": self.sources,
        }


# YouTube / Invidious often use Unicode dashes in titles; normalize before "Artist - Title" parsing.
_TITLE_DASH_NORMALIZE = str.maketrans(
    {
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2212": "-",  # minus sign
        "\uff0d": "-",  # fullwidth hyphen-minus
    }
)


def _normalize_title_dashes(title: str) -> str:
    if not title:
        return title
    return title.translate(_TITLE_DASH_NORMALIZE)


# Skip misleading splits for common non-music upload patterns.
_TITLE_PARSE_SKIP = re.compile(
    r"(?i)(?:^|\b)(how\s+to\b|tutorial\b|walkthrough\b|gameplay\b|lets\s+play\b|podcast\b|react(?:ion)?\b|#\s*shorts\b)"
)


def _parsed_title_allowed(title_norm: str, parsed: dict) -> bool:
    if _TITLE_PARSE_SKIP.search(title_norm):
        return False
    left = parsed.get("artist") or ""
    if re.match(r"^(?:s\d{1,2}\s*)?(?:e\d{1,3}|ep\.?\s*\d+)\b", left, re.IGNORECASE):
        return False
    return True


def quick_recognize(video: dict, position: int = 0) -> MusicRecognition:
    title = video.get("title", "") or ""
    title_norm = _normalize_title_dashes(title)
    channel = video.get("author", "") or ""
    confidence = 0.0
    sources = []

    # Title pattern boosts
    music_patterns = [
        "(Official Video)",
        "(Official Music Video)",
        "(Official Audio)",
        "(Lyric Video)",
        "(Lyrics)",
        "[Official]",
    ]
    for pat in music_patterns:
        if pat.lower() in title_norm.lower():
            confidence += 0.4
            sources.append("title_pattern")
            break

    # "X - Y" artist dash song pattern (ASCII or normalized Unicode dash)
    if re.search(r".+\s+-\s+.+", title_norm):
        confidence += 0.2
        if "title_dash" not in sources:
            sources.append("title_dash")

    # VEVO channel
    if channel.upper().endswith("VEVO"):
        confidence += 0.5
        sources.append("vevo")

    # Music-related channel keywords
    music_channel_keywords = ["Records", "Music", " Entertainment"]
    for kw in music_channel_keywords:
        if kw.lower() in channel.lower():
            confidence += 0.2
            sources.append("music_channel")
            break

    # Position boost (top results get small boost)
    pos_boost = max(0.0, 0.15 - position * 0.015)
    confidence += pos_boost

    # Cap at 0.85
    confidence = min(0.85, confidence)

    is_music = confidence >= 0.2
    track: str | None = None
    artist: str | None = None
    if is_music:
        parsed = _parse_title(title_norm)
        if parsed and _parsed_title_allowed(title_norm, parsed):
            track = parsed["track"]
            artist = parsed["artist"]
            sources.append("title_parse")

    return MusicRecognition(
        is_music=is_music,
        confidence=confidence,
        track=track,
        artist=artist,
        sources=list(set(sources)),
    )


def _parse_title(title: str) -> dict | None:
    """Extract artist/track from common title patterns."""
    title = _normalize_title_dashes((title or "").strip())
    if not title:
        return None
    # "Artist - Track (suffix)" or "Artist - Track [suffix]"
    m = re.match(r"^(.+?)\s+-\s+(.+?)(?:\s*[\(\[].+)?$", title)
    if m:
        artist = m.group(1).strip()
        track = m.group(2).strip()
        # Remove trailing (suffix) / [suffix] from track
        track = re.sub(r"\s*[\(\[].*$", "", track).strip()
        # Strip common video-quality / reupload tags left inside the track segment
        track = re.sub(
            r"\s*[\(\[]\s*(?:hd|hq|4k|1080p|720p|official\s+video|audio)\s*[\)\]]\s*$",
            "",
            track,
            flags=re.IGNORECASE,
        ).strip()
        if artist and track:
            return {"artist": artist, "track": track}

    # "Track by Artist"
    m2 = re.match(r"^(.+?)\s+by\s+(.+?)$", title, re.IGNORECASE)
    if m2:
        track = m2.group(1).strip()
        artist = m2.group(2).strip()
        if artist and track:
            return {"artist": artist, "track": track}

    return None


async def _confirm_via_apis(track: str, artist: str) -> dict | None:
    """Query iTunes then Deezer to enrich metadata."""
    try:
        query = f"{artist} {track}"
        results = await itunes_search(query, limit=3)
        if results:
            r = results[0]
            return {
                "track": r.get("track") or track,
                "artist": r.get("artist") or artist,
                "album": r.get("album"),
                "isrc": None,
                "source": "itunes",
            }
    except Exception:
        pass

    try:
        query = f"{artist} {track}"
        results = await deezer_search(query, limit=3)
        if results:
            r = results[0]
            return {
                "track": r.get("track") or track,
                "artist": r.get("artist") or artist,
                "album": r.get("album"),
                "isrc": None,
                "source": "deezer",
            }
    except Exception:
        pass

    return None


async def recognize(video_info: dict) -> MusicRecognition:
    """Full recognition using Invidious video info dict."""
    title = (video_info.get("title", "") or "").strip()
    author = video_info.get("author", "") or ""

    # Tier 1: musicVideoType field non-null/non-empty
    mvt = video_info.get("musicVideoType")
    if mvt:
        return MusicRecognition(
            is_music=True,
            confidence=1.0,
            track=video_info.get("track") or title,
            artist=video_info.get("artist") or author,
            album=video_info.get("album"),
            isrc=video_info.get("isrc"),
            sources=["musicVideoType"],
        )

    genre = video_info.get("genre", "") or ""
    vi_track = video_info.get("track") or video_info.get("song")
    vi_artist = video_info.get("artist") or video_info.get("author")

    # Tier 2: genre == "Music" AND track+artist present
    if genre.lower() == "music" and vi_track and vi_artist:
        enriched = await _confirm_via_apis(str(vi_track), str(vi_artist))
        if enriched:
            return MusicRecognition(
                is_music=True,
                confidence=0.9,
                track=enriched.get("track") or str(vi_track),
                artist=enriched.get("artist") or str(vi_artist),
                album=enriched.get("album"),
                isrc=enriched.get("isrc"),
                sources=["genre_music", enriched.get("source", "")],
            )
        return MusicRecognition(
            is_music=True,
            confidence=0.9,
            track=str(vi_track),
            artist=str(vi_artist),
            album=video_info.get("album"),
            sources=["genre_music"],
        )

    # Tier 3: genre == "Music" but no Invidious track/artist fields
    if genre.lower() == "music":
        parsed = _parse_title(title)
        base = quick_recognize(video_info, 0)
        confidence = max(base.confidence, 0.5)
        tr = (parsed["track"] if parsed else None) or base.track
        ar = (parsed["artist"] if parsed else None) or base.artist
        return MusicRecognition(
            is_music=True,
            confidence=confidence,
            track=tr,
            artist=ar,
            sources=list(set(["genre_music"] + base.sources)),
        )

    # Tier 4: heuristic fallback; enrich from iTunes/Deezer when title parsing yields identity
    base = quick_recognize(video_info, 0)
    if not base.is_music or not base.track or not base.artist:
        return base
    enriched = await _confirm_via_apis(base.track, base.artist)
    if not enriched:
        return base
    extra_sources = [s for s in base.sources if s]
    src = enriched.get("source", "")
    if src:
        extra_sources.append(src)
    return MusicRecognition(
        is_music=True,
        confidence=max(base.confidence, 0.55),
        track=enriched.get("track") or base.track,
        artist=enriched.get("artist") or base.artist,
        album=enriched.get("album"),
        isrc=None,
        sources=list(set(extra_sources)),
    )
