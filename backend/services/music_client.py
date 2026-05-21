import asyncio
import html
import json
import os
import re
import time
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime

import httpx
from backend.services.source_registry import get_credential, with_source
from urllib.parse import quote

# ── MusicBrainz ────────────────────────────────────────────────────────────────
_mb_lock = asyncio.Lock()

# Deezer genre_id → name (filled on first deezer_search)
_deezer_genre_map: dict[int, str] | None = None


async def _deezer_genre_map() -> dict[int, str]:
    global _deezer_genre_map
    if _deezer_genre_map is not None:
        return _deezer_genre_map
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://api.deezer.com/genre")
            r.raise_for_status()
            data = r.json()
        m: dict[int, str] = {}
        for row in data.get("data", []):
            try:
                gid = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            name = (row.get("name") or "").strip()
            if gid and name:
                m[gid] = name
        _deezer_genre_map = m
    except Exception:
        _deezer_genre_map = {}
    return _deezer_genre_map


def _itunes_artwork(url: str | None) -> str:
    if not url:
        return ""
    return url.replace("100x100bb", "600x600bb")


def _spotify_best_image(images: list[dict] | None) -> str:
    if not images:
        return ""
    return images[0].get("url", "") or ""


def _bandcamp_clean_url(url: str | None) -> str:
    if not url:
        return ""
    return html.unescape(url).strip().split("?", 1)[0]


def _bandcamp_norm(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value).lower()
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _bandcamp_match_score(hint: str | None, candidate: str | None) -> float:
    clean_hint = _bandcamp_norm(hint)
    clean_candidate = _bandcamp_norm(candidate)
    if not clean_hint or not clean_candidate:
        return 0.0
    score = SequenceMatcher(None, clean_hint, clean_candidate).ratio()
    if clean_hint == clean_candidate:
        return 1.0
    if clean_hint in clean_candidate or clean_candidate in clean_hint:
        score = max(score, 0.93)
    return score


def _bandcamp_json_attr(page_html: str, attr_name: str) -> dict:
    match = re.search(rf'{re.escape(attr_name)}="([^"]+)"', page_html)
    if not match:
        return {}
    try:
        return json.loads(html.unescape(match.group(1)))
    except Exception:
        return {}


def _bandcamp_embed_url(page_html: str, track_id: str | int | None) -> str:
    match = re.search(r"EmbeddedPlayer/[^\"' <]+", page_html)
    if match:
        path = html.unescape(match.group(0)).strip().lstrip("/")
        return f"https://bandcamp.com/{path}"
    if track_id:
        return f"https://bandcamp.com/EmbeddedPlayer/v=2/track={track_id}/size=large/tracklist=false/artwork=small/"
    return ""


def _bandcamp_meta_content(page_html: str, property_name: str) -> str:
    match = re.search(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(property_name)}["\'][^>]+content=["\']([^"\']+)["\']',
        page_html,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return html.unescape(match.group(1)).strip()


def _bandcamp_release_date(value: str | None) -> str:
    raw = html.unescape(value or "").strip()
    if not raw:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except Exception:
        return ""

async def musicbrainz_search_recording(query: str, limit: int = 5) -> list[dict]:
    async with _mb_lock:
        await asyncio.sleep(1)
        try:
            q = quote(query)
            url = f"https://musicbrainz.org/ws/2/recording?query={q}&limit={limit}&fmt=json"
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers={"User-Agent": "YTFrontend/1.0"})
                r.raise_for_status()
                data = r.json()
            results = []
            for rec in data.get("recordings", []):
                artist = ""
                if rec.get("artist-credit"):
                    ac = rec["artist-credit"][0]
                    artist = ac.get("name") or ac.get("artist", {}).get("name", "")
                release = rec.get("releases", [{}])[0] if rec.get("releases") else {}
                isrc_list = rec.get("isrcs", [])
                results.append({
                    "title": rec.get("title", ""),
                    "artist": artist,
                    "album": release.get("title", ""),
                    "isrc": isrc_list[0] if isrc_list else "",
                    "mb_id": rec.get("id", ""),
                })
            return results
        except Exception:
            return []


# ── iTunes ─────────────────────────────────────────────────────────────────────
async def itunes_search(term: str, limit: int = 5) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://itunes.apple.com/search",
                params={"term": term, "entity": "song", "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("results", []):
            art = _itunes_artwork(
                item.get("artworkUrl100")
                or item.get("artworkUrl60")
                or item.get("artworkUrl30")
                or ""
            )
            genre = (item.get("primaryGenreName") or "").strip()
            results.append({
                "track": item.get("trackName", ""),
                "artist": item.get("artistName", ""),
                "album": item.get("collectionName", ""),
                "cover_art": art,
                "duration": int((item.get("trackTimeMillis") or 0) / 1000) if item.get("trackTimeMillis") else None,
                "release_date": item.get("releaseDate", ""),
                "itunes_id": item.get("trackId", ""),
                "genre": genre,
                "source": "itunes",
            })
        return results
    except Exception:
        return []


async def itunes_search_artist(term: str, limit: int = 5) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://itunes.apple.com/search",
                params={"term": term, "entity": "allArtist", "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("results", []):
            results.append({
                "artist": item.get("artistName", ""),
                "itunes_artist_id": item.get("artistId", ""),
                "source": "itunes",
            })
        return results
    except Exception:
        return []


async def itunes_search_album(term: str, limit: int = 5) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://itunes.apple.com/search",
                params={"term": term, "entity": "album", "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("results", []):
            art = _itunes_artwork(
                item.get("artworkUrl100")
                or item.get("artworkUrl60")
                or item.get("artworkUrl30")
                or ""
            )
            results.append({
                "title": item.get("collectionName", ""),
                "artist": item.get("artistName", ""),
                "cover_art": art,
                "release_date": item.get("releaseDate", ""),
                "year": (item.get("releaseDate", "") or "")[:4],
                "track_count": item.get("trackCount"),
                "itunes_album_id": item.get("collectionId", ""),
                "itunes_artist_id": item.get("artistId", ""),
                "source": "itunes",
            })
        return results
    except Exception:
        return []


# ── Deezer ─────────────────────────────────────────────────────────────────────
async def deezer_search(q: str, limit: int = 5) -> list[dict]:
    try:
        genre_map = await _deezer_genre_map()
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.deezer.com/search",
                params={"q": q, "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("data", []):
            cover_art = (
                item.get("album", {}).get("cover_xl")
                or item.get("album", {}).get("cover_big")
                or item.get("album", {}).get("cover_medium")
                or item.get("album", {}).get("cover")
                or ""
            )
            gid = item.get("genre_id")
            genre_name = ""
            if gid is not None:
                try:
                    genre_name = genre_map.get(int(gid), "") or ""
                except (TypeError, ValueError):
                    genre_name = ""
            row = {
                "track": item.get("title", ""),
                "artist": item.get("artist", {}).get("name", ""),
                "album": item.get("album", {}).get("title", ""),
                "cover_art": cover_art,
                "duration": item.get("duration"),
                "popularity": item.get("rank"),
                "deezer_track_id": item.get("id", ""),
                "deezer_artist_id": item.get("artist", {}).get("id", ""),
                "source": "deezer",
            }
            if genre_name:
                row["genre"] = genre_name
            results.append(row)
        return results
    except Exception:
        return []


_CATALOG_GENRE_NOISE = frozenset({"", "music", "music video"})


def _norm_track_artist_match(value: str | None) -> str:
    if not value:
        return ""
    v = value.lower()
    v = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", v)
    v = re.sub(r"[^a-z0-9]+", " ", v)
    return " ".join(v.split())


async def infer_catalog_genre_hint(
    track: str | None,
    song: str | None,
    artist: str | None,
    title: str | None,
    author: str | None,
) -> str | None:
    """Genre label from iTunes + Deezer when YouTube/Invidious `genre` is only e.g. *Music*."""
    t_clean = (track or song or "").strip() or (title or "").strip()
    a_clean = (artist or "").strip() or (author or "").strip()
    if len(t_clean) < 2 or len(a_clean) < 1:
        return None
    query = f"{a_clean} {t_clean}".strip()
    if len(query) < 4:
        return None
    try:
        it, dz = await asyncio.gather(itunes_search(query, 10), deezer_search(query, 10))
    except Exception:
        return None
    want_t = _norm_track_artist_match(t_clean)
    want_a = _norm_track_artist_match(a_clean)
    if not want_t or not want_a:
        return None
    best_g = ""
    best = 0.0
    for row in it + dz:
        g = (row.get("genre") or "").strip()
        if not g or g.lower() in _CATALOG_GENRE_NOISE:
            continue
        rt = _norm_track_artist_match(str(row.get("track") or ""))
        ra = _norm_track_artist_match(str(row.get("artist") or ""))
        if not rt:
            continue
        score = SequenceMatcher(None, want_t, rt).ratio() * 0.55
        score += SequenceMatcher(None, want_a, ra).ratio() * 0.45 if ra else 0.0
        if score > best:
            best = score
            best_g = g
    if best >= 0.48 and best_g:
        return best_g
    return None


async def deezer_search_album(q: str, limit: int = 5) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.deezer.com/search/album",
                params={"q": q, "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("data", []):
            cover_art = (
                item.get("cover_xl")
                or item.get("cover_big")
                or item.get("cover_medium")
                or item.get("cover")
                or ""
            )
            results.append({
                "title": item.get("title", ""),
                "artist": item.get("artist", {}).get("name", ""),
                "cover_art": cover_art,
                "release_date": item.get("release_date", ""),
                "year": str(item.get("release_date", "") or "")[:4],
                "track_count": item.get("nb_tracks"),
                "deezer_album_id": item.get("id", ""),
                "deezer_artist_id": item.get("artist", {}).get("id", ""),
                "album_type": item.get("record_type", ""),
                "source": "deezer",
            })
        return results
    except Exception:
        return []


async def deezer_search_artist(q: str, limit: int = 5) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.deezer.com/search/artist",
                params={"q": q, "limit": limit},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("data", []):
            results.append({
                "artist": item.get("name", ""),
                "deezer_artist_id": item.get("id", ""),
                "image": (
                    item.get("picture_xl")
                    or item.get("picture_big")
                    or item.get("picture_medium")
                    or item.get("picture")
                    or ""
                ),
                "popularity": item.get("nb_fan"),
                "source": "deezer",
            })
        return results
    except Exception:
        return []


async def deezer_get_artist_albums(artist_id, limit: int = 20) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.deezer.com/artist/{artist_id}/albums",
                params={"limit": limit},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("data", [])[:limit]:
            cover_art = (
                item.get("cover_xl")
                or item.get("cover_big")
                or item.get("cover_medium")
                or item.get("cover")
                or ""
            )
            results.append({
                "title": item.get("title", ""),
                "artist": item.get("artist", {}).get("name", ""),
                "cover_art": cover_art,
                "release_date": item.get("release_date", ""),
                "year": str(item.get("release_date", "") or "")[:4],
                "track_count": item.get("nb_tracks"),
                "deezer_album_id": item.get("id", ""),
                "deezer_artist_id": item.get("artist", {}).get("id", "") or artist_id,
                "album_type": item.get("record_type", ""),
                "source": "deezer",
            })
        return results
    except Exception:
        return []


async def deezer_get_album_tracks(album_id, limit: int = 100) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.deezer.com/album/{album_id}/tracks",
                params={"limit": limit},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for index, item in enumerate(data.get("data", [])[:limit]):
            results.append({
                "position": item.get("track_position") or (index + 1),
                "title": item.get("title", ""),
                "duration": item.get("duration"),
                "artist": item.get("artist", {}).get("name", ""),
                "disc_number": item.get("disk_number"),
                "source": "deezer",
            })
        return results
    except Exception:
        return []


async def deezer_get_related_artists(artist_id, limit: int = 10) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"https://api.deezer.com/artist/{artist_id}/related")
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("data", [])[:limit]:
            results.append({
                "artist": item.get("name", ""),
                "deezer_artist_id": item.get("id", ""),
            })
        return results
    except Exception:
        return []


# ── Last.fm ────────────────────────────────────────────────────────────────────

@with_source("lastfm")
async def lastfm_search_track(track: str, artist: str = "", limit: int = 5) -> list[dict]:
    key = get_credential("lastfm", "LASTFM_KEY")
    if not key:
        return []
    try:
        params: dict = {
            "method": "track.search",
            "track": track,
            "limit": limit,
            "api_key": key,
            "format": "json",
        }
        if artist:
            params["artist"] = artist
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://ws.audioscrobbler.com/2.0/", params=params)
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("results", {}).get("trackmatches", {}).get("track", []):
            results.append({
                "track": item.get("name", ""),
                "artist": item.get("artist", ""),
            })
        return results
    except Exception:
        return []


@with_source("lastfm")
async def lastfm_get_similar_tracks(track: str, artist: str, limit: int = 10) -> list[dict]:
    key = get_credential("lastfm", "LASTFM_KEY")
    if not key:
        return []
    try:
        params = {
            "method": "track.getSimilar",
            "track": track,
            "artist": artist,
            "limit": limit,
            "api_key": key,
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://ws.audioscrobbler.com/2.0/", params=params)
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("similartracks", {}).get("track", []):
            results.append({
                "track": item.get("name", ""),
                "artist": item.get("artist", {}).get("name", ""),
                "match": float(item.get("match", 0.0)),
            })
        return results
    except Exception:
        return []


# ── Spotify ────────────────────────────────────────────────────────────────────
_spotify_token: dict = {"token": None, "expires": 0.0}


async def _spotify_get_token() -> str | None:
    client_id = get_credential("spotify", "SPOTIFY_CLIENT_ID")
    client_secret = get_credential("spotify", "SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    if _spotify_token["token"] and time.time() < _spotify_token["expires"] - 60:
        return _spotify_token["token"]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
            )
            r.raise_for_status()
            data = r.json()
        _spotify_token["token"] = data["access_token"]
        _spotify_token["expires"] = time.time() + data.get("expires_in", 3600)
        return _spotify_token["token"]
    except Exception:
        return None


async def spotify_search(q: str, limit: int = 5) -> list[dict]:
    token = await _spotify_get_token()
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.spotify.com/v1/search",
                params={"q": q, "type": "track", "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("tracks", {}).get("items", []):
            album = item.get("album") or {}
            album_genres = [g for g in (album.get("genres") or []) if isinstance(g, str) and g.strip()]
            row = {
                "track": item.get("name", ""),
                "artist": item.get("artists", [{}])[0].get("name", ""),
                "album": album.get("name", ""),
                "cover_art": _spotify_best_image(album.get("images", [])),
                "duration": int((item.get("duration_ms") or 0) / 1000) if item.get("duration_ms") else None,
                "release_date": album.get("release_date", ""),
                "popularity": item.get("popularity"),
                "spotify_track_id": item.get("id", ""),
                "spotify_artist_id": item.get("artists", [{}])[0].get("id", ""),
                "source": "spotify",
            }
            if album_genres:
                row["genres"] = album_genres
            results.append(row)
        return results
    except Exception:
        return []


async def spotify_search_artist(q: str, limit: int = 5) -> list[dict]:
    token = await _spotify_get_token()
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.spotify.com/v1/search",
                params={"q": q, "type": "artist", "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("artists", {}).get("items", []):
            results.append({
                "artist": item.get("name", ""),
                "spotify_artist_id": item.get("id", ""),
                "image": _spotify_best_image(item.get("images", [])),
                "popularity": item.get("popularity"),
                "source": "spotify",
            })
        return results
    except Exception:
        return []


async def spotify_search_album(q: str, limit: int = 5) -> list[dict]:
    token = await _spotify_get_token()
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.spotify.com/v1/search",
                params={"q": q, "type": "album", "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("albums", {}).get("items", []):
            cover_art = _spotify_best_image(item.get("images", []))
            results.append({
                "title": item.get("name", ""),
                "artist": item.get("artists", [{}])[0].get("name", ""),
                "cover_art": cover_art,
                "release_date": item.get("release_date", ""),
                "year": str(item.get("release_date", "") or "")[:4],
                "track_count": item.get("total_tracks"),
                "spotify_album_id": item.get("id", ""),
                "spotify_artist_id": item.get("artists", [{}])[0].get("id", ""),
                "album_type": item.get("album_type", ""),
                "source": "spotify",
            })
        return results
    except Exception:
        return []


async def spotify_get_artist_albums(artist_id: str, limit: int = 20) -> list[dict]:
    token = await _spotify_get_token()
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.spotify.com/v1/artists/{artist_id}/albums",
                params={"include_groups": "album", "limit": limit, "market": "US"},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("items", [])[:limit]:
            results.append({
                "title": item.get("name", ""),
                "artist": item.get("artists", [{}])[0].get("name", ""),
                "cover_art": _spotify_best_image(item.get("images", [])),
                "release_date": item.get("release_date", ""),
                "year": str(item.get("release_date", "") or "")[:4],
                "track_count": item.get("total_tracks"),
                "spotify_album_id": item.get("id", ""),
                "spotify_artist_id": item.get("artists", [{}])[0].get("id", "") or artist_id,
                "album_type": item.get("album_type", ""),
                "source": "spotify",
            })
        return results
    except Exception:
        return []


async def spotify_get_album_tracks(album_id: str, limit: int = 50) -> list[dict]:
    token = await _spotify_get_token()
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.spotify.com/v1/albums/{album_id}/tracks",
                params={"limit": limit, "market": "US"},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for index, item in enumerate(data.get("items", [])[:limit]):
            results.append({
                "position": item.get("track_number") or (index + 1),
                "title": item.get("name", ""),
                "duration": round((item.get("duration_ms") or 0) / 1000) or None,
                "artist": item.get("artists", [{}])[0].get("name", ""),
                "disc_number": item.get("disc_number"),
                "source": "spotify",
            })
        return results
    except Exception:
        return []


async def spotify_get_recommendations(seed_track_ids: list[str], limit: int = 10) -> list[dict]:
    token = await _spotify_get_token()
    if not token:
        return []
    try:
        seeds = ",".join(seed_track_ids[:5])
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.spotify.com/v1/recommendations",
                params={"seed_tracks": seeds, "limit": limit},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("tracks", []):
            results.append({
                "track": item.get("name", ""),
                "artist": item.get("artists", [{}])[0].get("name", ""),
                "album": item.get("album", {}).get("name", ""),
                "spotify_track_id": item.get("id", ""),
            })
        return results
    except Exception:
        return []


# ── Discogs ────────────────────────────────────────────────────────────────────

@with_source("discogs")
async def discogs_search(q: str, limit: int = 5) -> list[dict]:
    token = get_credential("discogs", "DISCOGS_TOKEN")
    if not token:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.discogs.com/database/search",
                params={"q": q, "type": "release", "token": token, "per_page": limit},
                headers={"User-Agent": "YTFrontend/1.0"},
            )
            r.raise_for_status()
            data = r.json()
        results = []
        for item in data.get("results", []):
            title = item.get("title", "")
            artist = ""
            if " - " in title:
                parts = title.split(" - ", 1)
                artist = parts[0]
                title = parts[1]
            results.append({
                "title": title,
                "artist": artist,
                "year": str(item.get("year", "")),
                "discogs_id": item.get("id", ""),
            })
        return results
    except Exception:
        return []


# ── Bandcamp ───────────────────────────────────────────────────────────────────
# Bandcamp has no documented public API for third-party search or catalog data.
# The helpers below use the same HTML/JSON surfaces as a browser (search pages and
# public album/track pages, including ``data-tralbum``).
async def bandcamp_search(q: str, limit: int = 5) -> list[dict]:
    try:
        url = f"https://bandcamp.com/search?q={quote(q)}&item_type=t"
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            page_html = r.text
        results = []
        blocks = re.split(r'<li class="searchresult', page_html)
        for block in blocks[1:]:
            if len(results) >= limit:
                break
            track_m = re.search(r'class="heading"[^>]*>\s*<a[^>]*>([^<]+)</a>', block)
            track = html.unescape(track_m.group(1)).strip() if track_m else ""
            by_m = re.search(r'\bby\s+([^<\n]+)', block)
            artist = html.unescape(by_m.group(1)).strip() if by_m else ""
            album_m = re.search(r'class="subhead"[^>]*>([^<]+)<', block)
            album = html.unescape(album_m.group(1)).strip() if album_m else ""
            url_m = re.search(r'href="(https://[^"]*bandcamp\.com[^"]*)"', block)
            item_url = _bandcamp_clean_url(url_m.group(1) if url_m else "")
            if track:
                results.append({"track": track, "artist": artist, "album": album, "url": item_url})
        return results
    except Exception:
        return []


async def bandcamp_search_albums(q: str, limit: int = 5) -> list[dict]:
    try:
        url = f"https://bandcamp.com/search?q={quote(q)}&item_type=a"
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            page_html = r.text
        results = []
        blocks = re.split(r'<li class="searchresult', page_html)
        for block in blocks[1:]:
            if len(results) >= limit:
                break
            if re.search(r'class="itemtype"[^>]*>\s*track\s*<', block, flags=re.IGNORECASE):
                continue
            title_m = re.search(r'class="heading"[^>]*>\s*<a[^>]*>([^<]+)</a>', block)
            title = html.unescape(title_m.group(1)).strip() if title_m else ""
            if not title:
                continue
            artist_m = re.search(r'\bby\s+([^<\n]+)', block)
            artist = html.unescape(artist_m.group(1)).strip() if artist_m else ""
            url_m = re.search(r'href="(https://[^"]*bandcamp\.com[^"]*)"', block)
            item_url = _bandcamp_clean_url(url_m.group(1) if url_m else "")
            cover_m = re.search(r'<img[^>]+src="([^"]+)"', block)
            cover_art = _bandcamp_clean_url(cover_m.group(1) if cover_m else "")
            results.append({
                "title": title,
                "artist": artist,
                "cover_art": cover_art,
                "bandcamp_url": item_url,
                "source": "bandcamp",
            })
        return results
    except Exception:
        return []


async def bandcamp_track_details(url: str) -> dict | None:
    clean_url = _bandcamp_clean_url(url)
    if not clean_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(clean_url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            page_html = r.text
    except Exception:
        return None

    tralbum = _bandcamp_json_attr(page_html, "data-tralbum")
    embed = _bandcamp_json_attr(page_html, "data-embed")
    trackinfo = tralbum.get("trackinfo") or []
    first_track = trackinfo[0] if trackinfo else {}
    file_info = first_track.get("file") or {}
    audio_url = html.unescape(file_info.get("mp3-128") or "").strip()
    track_id = (
        first_track.get("track_id")
        or first_track.get("id")
        or tralbum.get("id")
        or (embed.get("tralbum_param") or {}).get("value")
    )

    return {
        "track": (first_track.get("title") or embed.get("title") or "").strip(),
        "artist": (tralbum.get("artist") or embed.get("artist") or "").strip(),
        "album": (tralbum.get("album_title") or "").strip(),
        "url": clean_url,
        "audio_url": audio_url,
        "embed_url": _bandcamp_embed_url(page_html, track_id),
        "track_id": str(track_id) if track_id else "",
    }


async def bandcamp_album_details(url: str) -> dict | None:
    clean_url = _bandcamp_clean_url(url)
    if not clean_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            r = await client.get(clean_url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            page_html = r.text
    except Exception:
        return None

    tralbum = _bandcamp_json_attr(page_html, "data-tralbum")
    current = tralbum.get("current") or {}
    trackinfo = tralbum.get("trackinfo") or []
    artist = (tralbum.get("artist") or current.get("artist") or "").strip()
    title = (current.get("title") or tralbum.get("album_title") or "").strip()
    release_date = _bandcamp_release_date(
        current.get("release_date")
        or current.get("publish_date")
        or current.get("new_date")
    )
    cover_art = _bandcamp_meta_content(page_html, "og:image")
    release_id = current.get("id") or tralbum.get("id")

    tracks = []
    for index, item in enumerate(trackinfo):
        if not isinstance(item, dict):
            continue
        track_title = (item.get("title") or "").strip()
        if not track_title:
            continue
        duration = item.get("duration")
        try:
            duration_value = round(float(duration)) if duration else None
        except Exception:
            duration_value = None
        file_info = item.get("file") or {}
        tracks.append({
            "position": item.get("track_num") or item.get("track_number") or (index + 1),
            "title": track_title,
            "duration": duration_value,
            "artist": artist,
            "disc_number": item.get("disc") or item.get("disc_number"),
            "source": "bandcamp",
            "audio_url": html.unescape(file_info.get("mp3-128") or "").strip(),
        })

    return {
        "title": title,
        "artist": artist,
        "cover_art": cover_art,
        "release_date": release_date,
        "year": release_date[:4] if release_date else "",
        "track_count": len(tracks) or None,
        "bandcamp_url": clean_url,
        "bandcamp_embed_url": _bandcamp_embed_url(page_html, release_id),
        "bandcamp_album_id": str(release_id) if release_id else "",
        "bandcamp_tracks": tracks,
        "source": "bandcamp",
    }


async def bandcamp_lookup(
    q: str,
    *,
    track: str = "",
    artist: str = "",
    title: str = "",
    author: str = "",
    limit: int = 5,
) -> dict | None:
    matches = await bandcamp_search(q, limit=limit)
    if not matches:
        return None

    best = matches[0]
    best_score = -1.0
    for candidate in matches:
        track_score = max(
            _bandcamp_match_score(track, candidate.get("track")),
            _bandcamp_match_score(title, candidate.get("track")),
        )
        artist_score = max(
            _bandcamp_match_score(artist, candidate.get("artist")),
            _bandcamp_match_score(author, candidate.get("artist")),
        )
        score = (track_score * 0.75) + (artist_score * 0.25)
        if score > best_score:
            best = candidate
            best_score = score

    details = await bandcamp_track_details(best.get("url", ""))
    if not details:
        return {
            "track": best.get("track", ""),
            "artist": best.get("artist", ""),
            "album": best.get("album", ""),
            "url": best.get("url", ""),
            "audio_url": "",
            "embed_url": "",
            "track_id": "",
        }

    details["track"] = details.get("track") or best.get("track", "")
    details["artist"] = details.get("artist") or best.get("artist", "")
    details["album"] = details.get("album") or best.get("album", "")
    details["url"] = details.get("url") or best.get("url", "")
    return details
