"""Genre / mood / decade classification for rated music.

Most yamtrack-rated albums are NOT present as tracks in `music_library`, so
classification is stored at the *album* level (keyed by the canonical
`album_key`) in `music_album_classification`. When an album's tracks DO exist in
the library, applying a classification also assigns the matching genre/mood/
decade meta-tags (reusing the user's existing "Genres" / "Moods" / "Year/Decade"
tag groups) and sets the primary `music_library.genre`, so the work shows up in
the existing tag organizer.

Suggestions are hybrid: candidate genre/mood tags come from external APIs
(Last.fm album + artist tags — these are direct calls, not routed through the
saturated YouTube egress), and a local ollama model normalizes them against the
*existing* vocabulary, only proposing a genuinely new label when nothing fits.
Decade is derived in code from the release year (never the LLM).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from difflib import SequenceMatcher

import httpx

from backend.db import get_db, normalize_album_key
from backend.services.music_client import (
    deezer_search_album,
    itunes_search_album,
    lastfm_album_tags,
    lastfm_artist_tags,
)

logger = logging.getLogger("music_classifier")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.1.176:11434").rstrip("/")
# llama3.2:3b classifies well when guided by candidate tags and is ~10x faster
# on CPU than the 8b/12b models (~15s vs ~150s/album). Override with
# CLASSIFY_MODEL=dolphin3:8b for richer mood inference at the cost of latency.
CLASSIFY_MODEL = os.environ.get("CLASSIFY_MODEL", "llama3.2:3b")
# Keep the model resident between per-album calls during a classify session.
CLASSIFY_KEEP_ALIVE = os.environ.get("CLASSIFY_KEEP_ALIVE", "10m")
YTMUSIC_DB_PATH = os.getenv("YTMUSIC_DB_PATH", "/opt/ytmusic/data/ytmusic.db")

SUGGESTION_TTL = float(os.environ.get("CLASSIFY_SUGGEST_TTL", str(60 * 60 * 24 * 30)))

# Tags that carry no useful genre/mood signal.
_TAG_NOISE = frozenset({
    "", "music", "albums i own", "favorites", "favourite", "favourites",
    "seen live", "spotify", "vinyl", "owned", "love", "loved", "good",
    "awesome", "favorite albums", "all", "best",
})


# ── schema ──────────────────────────────────────────────────────────────────

def init_classification_db() -> None:
    conn = get_db()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS music_album_classification (
                   album_key   TEXT PRIMARY KEY,
                   artist      TEXT,
                   album       TEXT,
                   genres      TEXT NOT NULL DEFAULT '[]',
                   moods       TEXT NOT NULL DEFAULT '[]',
                   decade      TEXT,
                   year        INTEGER,
                   cover_art   TEXT,
                   updated_at  REAL NOT NULL
               )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS music_classification_suggestions (
                   album_key  TEXT PRIMARY KEY,
                   payload    TEXT NOT NULL,
                   model      TEXT,
                   created_at REAL NOT NULL
               )"""
        )
        conn.commit()
    finally:
        conn.close()


# ── vocabulary ──────────────────────────────────────────────────────────────

def _norm(value: str | None) -> str:
    if not value:
        return ""
    v = value.lower()
    v = re.sub(r"[^a-z0-9]+", " ", v)
    return " ".join(v.split())


def _resolve_group_id(conn, *needles: str) -> int | None:
    rows = conn.execute(
        "SELECT id, name FROM music_tag_groups ORDER BY position ASC, id ASC"
    ).fetchall()
    for row in rows:
        name = (row["name"] or "").strip().lower()
        for needle in needles:
            if needle in name:
                return int(row["id"])
    return None


def _group_tags(conn, group_id: int | None) -> list[str]:
    if group_id is None:
        return []
    rows = conn.execute(
        "SELECT name FROM music_tags WHERE group_id=? ORDER BY name COLLATE NOCASE",
        (group_id,),
    ).fetchall()
    return [r["name"] for r in rows if (r["name"] or "").strip()]


def get_vocabulary() -> dict:
    """Existing genre + mood labels the user already organizes by."""
    conn = get_db()
    try:
        genre_gid = _resolve_group_id(conn, "genre")
        mood_gid = _resolve_group_id(conn, "mood")
        genres = _group_tags(conn, genre_gid)
        # Distinct primary genres already applied to library rows count too.
        for r in conn.execute(
            "SELECT DISTINCT genre FROM music_library WHERE genre IS NOT NULL AND genre != ''"
        ):
            if r["genre"] not in genres:
                genres.append(r["genre"])
        return {"genres": genres, "moods": _group_tags(conn, mood_gid)}
    finally:
        conn.close()


def _snap(label: str, vocab: list[str]) -> str | None:
    """Collapse a label onto an existing vocabulary entry (e.g. 'Hip-Hop' →
    'Hip Hop') so the AI cannot silently create near-duplicate categories."""
    n = _norm(label)
    if not n:
        return None
    by_norm = {_norm(v): v for v in vocab}
    if n in by_norm:
        return by_norm[n]
    best, best_ratio = None, 0.0
    for vn, original in by_norm.items():
        ratio = SequenceMatcher(None, n, vn).ratio()
        if ratio > best_ratio:
            best, best_ratio = original, ratio
    return best if best_ratio >= 0.86 else None


def _decade_from_year(year: int | None) -> str | None:
    if not year or year < 1900 or year > 2100:
        return None
    return f"{(year // 10) * 10}s"


# ── external candidates ─────────────────────────────────────────────────────

def _pick_album_match(results: list[dict], artist: str, album: str) -> dict | None:
    want_t, want_a = _norm(album), _norm(artist)
    best, best_score = None, 0.0
    for row in results or []:
        if not isinstance(row, dict):
            continue
        score = SequenceMatcher(None, want_t, _norm(row.get("title"))).ratio() * 0.6
        score += SequenceMatcher(None, want_a, _norm(row.get("artist"))).ratio() * 0.4
        if score > best_score:
            best, best_score = row, score
    return best if best_score >= 0.5 else None


async def gather_album_meta(artist: str, album: str) -> dict:
    """Collect candidate genre/mood tags + release year + a cover, from direct
    metadata APIs. Tolerant of any single source failing."""
    q = f"{artist} {album}".strip()
    album_tags, artist_tags, dz, it = await asyncio.gather(
        lastfm_album_tags(artist, album),
        lastfm_artist_tags(artist, 8),
        deezer_search_album(q, 5),
        itunes_search_album(q, 5),
        return_exceptions=True,
    )
    album_tags = album_tags if isinstance(album_tags, list) else []
    artist_tags = artist_tags if isinstance(artist_tags, list) else []
    dz = dz if isinstance(dz, list) else []
    it = it if isinstance(it, list) else []

    year, cover = None, None
    for results in (dz, it):
        match = _pick_album_match(results, artist, album)
        if match:
            ys = str(match.get("year") or "")[:4]
            if ys.isdigit() and year is None:
                year = int(ys)
            if not cover and match.get("cover_art"):
                cover = match.get("cover_art")

    # De-dupe candidate tags, drop noise, cap length.
    seen, candidates = set(), []
    for tag in [*album_tags, *artist_tags]:
        key = _norm(tag)
        if not key or key in _TAG_NOISE or key in seen:
            continue
        seen.add(key)
        candidates.append(tag.strip())
    return {"candidate_tags": candidates[:20], "year": year, "cover_art": cover}


# ── ollama ──────────────────────────────────────────────────────────────────

def _classify_prompt(artist, album, candidates, genres_vocab, moods_vocab) -> str:
    return (
        "You are a music cataloguer. Classify an album into genres and moods.\n\n"
        f"ALBUM: {album}\nARTIST: {artist}\n"
        f"CANDIDATE_TAGS (from music databases): {', '.join(candidates) or '(none)'}\n\n"
        f"EXISTING_GENRES: {', '.join(genres_vocab) or '(none)'}\n"
        f"EXISTING_MOODS: {', '.join(moods_vocab) or '(none)'}\n\n"
        "Rules:\n"
        "- Pick 1-3 genres and 1-3 moods.\n"
        "- STRONGLY prefer labels from EXISTING_GENRES / EXISTING_MOODS; reuse "
        "their exact spelling. Only invent a new label when nothing existing fits.\n"
        "- Never invent near-duplicates of existing labels.\n"
        "- Genres describe style (e.g. Jazz, Post-punk); moods describe feel "
        "(e.g. Melancholic, Energetic, Chill).\n"
        'Respond ONLY with JSON: {"genres": ["..."], "moods": ["..."]}'
    )


async def _ollama_classify(prompt: str) -> dict | None:
    body = {
        "model": CLASSIFY_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "keep_alive": CLASSIFY_KEEP_ALIVE,
        "options": {"temperature": 0.1},
    }
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/generate", json=body)
            r.raise_for_status()
            text = r.json().get("response", "")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception as exc:  # noqa: BLE001
        logger.warning("music_classifier: ollama classify failed: %s", exc)
    return None


def _label_entries(labels, vocab: list[str]) -> list[dict]:
    out, seen = [], set()
    for raw in labels or []:
        label = " ".join(str(raw or "").split())
        if not label or _norm(label) in _TAG_NOISE:
            continue
        snapped = _snap(label, vocab)
        name = snapped or label.title()
        if _norm(name) in seen:
            continue
        seen.add(_norm(name))
        out.append({
            "name": name,
            "existing": snapped is not None,
            "confidence": 0.9 if snapped is not None else 0.6,
        })
    return out


async def build_suggestion(artist: str, album: str) -> dict:
    vocab = get_vocabulary()
    meta = await gather_album_meta(artist, album)
    prompt = _classify_prompt(
        artist, album, meta["candidate_tags"], vocab["genres"], vocab["moods"]
    )
    llm = await _ollama_classify(prompt)
    if llm:
        genres = _label_entries(llm.get("genres"), vocab["genres"])
        moods = _label_entries(llm.get("moods"), vocab["moods"])
        engine = CLASSIFY_MODEL
    else:
        # Graceful fallback: snap raw candidate tags to existing vocabulary so
        # suggestions still work when ollama is unreachable.
        genres = [e for e in _label_entries(meta["candidate_tags"], vocab["genres"]) if e["existing"]][:3]
        moods = []
        engine = "candidates"
    return {
        "genres": genres,
        "moods": moods,
        "decade": _decade_from_year(meta["year"]),
        "year": meta["year"],
        "cover_art": meta["cover_art"],
        "engine": engine,
    }


# ── suggestion cache ────────────────────────────────────────────────────────

def get_cached_suggestion(album_key: str) -> dict | None:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT payload, created_at FROM music_classification_suggestions WHERE album_key=?",
            (album_key,),
        ).fetchone()
        if not row:
            return None
        if time.time() - float(row["created_at"]) > SUGGESTION_TTL:
            return None
        try:
            return json.loads(row["payload"])
        except Exception:  # noqa: BLE001
            return None
    finally:
        conn.close()


def _store_suggestion(album_key: str, payload: dict, model: str) -> None:
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO music_classification_suggestions (album_key, payload, model, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(album_key) DO UPDATE SET payload=excluded.payload,
                   model=excluded.model, created_at=excluded.created_at""",
            (album_key, json.dumps(payload), model, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


async def suggest_for_album(album_key: str, artist: str, album: str, refresh: bool = False) -> dict:
    if not refresh:
        cached = get_cached_suggestion(album_key)
        if cached is not None:
            return {**cached, "cached": True}
    payload = await build_suggestion(artist, album)
    _store_suggestion(album_key, payload, payload.get("engine") or "")
    return {**payload, "cached": False}


# ── apply ───────────────────────────────────────────────────────────────────

def _matching_library_videos(conn, album_key: str) -> list[str]:
    out = []
    for r in conn.execute(
        "SELECT video_id, album, artist FROM music_library WHERE album IS NOT NULL AND album != ''"
    ):
        if normalize_album_key(r["album"], r["artist"]) == album_key:
            out.append(r["video_id"])
    return out


def _ensure_tag(conn, group_id: int | None, name: str) -> int | None:
    """Find an existing tag by name in the group, else create it. Returns id."""
    if group_id is None or not name.strip():
        return None
    row = conn.execute(
        "SELECT id FROM music_tags WHERE group_id=? AND lower(trim(name))=lower(trim(?))",
        (group_id, name),
    ).fetchone()
    if row:
        return int(row["id"])
    now = time.time()
    pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM music_tags WHERE group_id=? AND parent_id IS NULL",
        (group_id,),
    ).fetchone()[0]
    tag_id = conn.execute(
        """INSERT INTO music_tags (name, kind, group_id, parent_id, position, created_at, updated_at)
           VALUES (?, 'new', ?, NULL, ?, ?, ?)""",
        (" ".join(name.split()), group_id, pos, now, now),
    ).lastrowid
    return int(tag_id)


def apply_classification(
    album_key: str,
    artist: str,
    album: str,
    genres: list[str],
    moods: list[str],
    decade: str | None,
    year: int | None = None,
    cover_art: str | None = None,
) -> dict:
    genres = [" ".join(g.split()) for g in (genres or []) if (g or "").strip()]
    moods = [" ".join(m.split()) for m in (moods or []) if (m or "").strip()]
    conn = get_db()
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        now = time.time()
        # 1. Album-level record (authoritative; covers albums not in the library).
        conn.execute(
            """INSERT INTO music_album_classification
                   (album_key, artist, album, genres, moods, decade, year, cover_art, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(album_key) DO UPDATE SET
                   artist=excluded.artist, album=excluded.album, genres=excluded.genres,
                   moods=excluded.moods, decade=excluded.decade, year=excluded.year,
                   cover_art=COALESCE(excluded.cover_art, music_album_classification.cover_art),
                   updated_at=excluded.updated_at""",
            (album_key, artist, album, json.dumps(genres), json.dumps(moods),
             decade, year, cover_art, now),
        )

        # 2. Resolve the user's existing meta-tag groups.
        genre_gid = _resolve_group_id(conn, "genre")
        mood_gid = _resolve_group_id(conn, "mood")
        decade_gid = _resolve_group_id(conn, "decade", "year")

        tag_ids: list[int] = []
        for g in genres:
            tid = _ensure_tag(conn, genre_gid, g)
            if tid:
                tag_ids.append(tid)
        for m in moods:
            tid = _ensure_tag(conn, mood_gid, m)
            if tid:
                tag_ids.append(tid)
        if decade:
            tid = _ensure_tag(conn, decade_gid, decade)
            if tid:
                tag_ids.append(tid)

        # 3. Apply to any library tracks of this album (tracks inherit).
        videos = _matching_library_videos(conn, album_key)
        primary_genre = genres[0] if genres else None
        for vid in videos:
            if primary_genre:
                conn.execute(
                    "UPDATE music_library SET genre=? WHERE video_id=?",
                    (primary_genre, vid),
                )
            for tid in tag_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO music_tag_assignments (tag_id, video_id, created_at) VALUES (?, ?, ?)",
                    (tid, vid, now),
                )
        conn.commit()
        return {"ok": True, "tagged_tracks": len(videos), "tag_ids": tag_ids}
    finally:
        conn.close()


# ── queue ───────────────────────────────────────────────────────────────────

def _ytmusic_album_ratings() -> dict[str, dict]:
    """album_key → {rating, cover_art, title, artist} from the ytmusic DB."""
    out: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(f"file:{YTMUSIC_DB_PATH}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute(
                "SELECT album_key, album_title, album_artist, cover_art, rating FROM album_ratings"
            ):
                out[r["album_key"]] = {
                    "rating": r["rating"],
                    "cover_art": r["cover_art"] or "",
                    "title": r["album_title"],
                    "artist": r["album_artist"] or "",
                }
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("music_classifier: ytmusic ratings read failed: %s", exc)
    return out


def list_classification_queue(scope: str = "rated", page: int = 1, per_page: int = 40) -> dict:
    """Albums to classify. `rated` = yamtrack-rated (external_music_seeds) ∪
    native ytmusic album ratings; `all` additionally includes every distinct
    album present in the library."""
    conn = get_db()
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        albums: dict[str, dict] = {}

        # Rated seeds from yamtrack (artist/album/score).
        for r in conn.execute(
            "SELECT artist, album, score FROM external_music_seeds WHERE kind='album'"
        ):
            artist, album = r["artist"] or "", r["album"] or ""
            if not album:
                continue
            key = normalize_album_key(album, artist)
            albums.setdefault(key, {
                "album_key": key, "artist": artist, "album": album,
                "rating": None, "cover_art": "", "source": "yamtrack",
            })
            if r["score"] is not None:
                albums[key]["rating"] = round(float(r["score"]))

        # Enrich / add native ytmusic ratings (cover art + rating).
        for key, info in _ytmusic_album_ratings().items():
            entry = albums.get(key)
            if entry is None:
                entry = {
                    "album_key": key, "artist": info["artist"], "album": info["title"],
                    "rating": info["rating"], "cover_art": info["cover_art"], "source": "ytmusic",
                }
                albums[key] = entry
            else:
                if entry.get("rating") is None:
                    entry["rating"] = info["rating"]
                if not entry.get("cover_art"):
                    entry["cover_art"] = info["cover_art"]

        if scope == "all":
            for r in conn.execute(
                """SELECT artist, album, MIN(thumbnail) AS thumb
                   FROM music_library WHERE album IS NOT NULL AND album != ''
                   GROUP BY lower(artist), lower(album)"""
            ):
                key = normalize_album_key(r["album"], r["artist"])
                if key not in albums:
                    albums[key] = {
                        "album_key": key, "artist": r["artist"] or "", "album": r["album"],
                        "rating": None, "cover_art": r["thumb"] or "", "source": "library",
                    }

        # Attach existing classification + cached suggestion availability.
        classified = {
            r["album_key"]: r for r in conn.execute(
                "SELECT album_key, genres, moods, decade, year, cover_art FROM music_album_classification"
            )
        }
        suggested = {
            r["album_key"]: r["payload"] for r in conn.execute(
                "SELECT album_key, payload FROM music_classification_suggestions"
            )
        }
        items = []
        for key, info in albums.items():
            cls = classified.get(key)
            current = None
            if cls is not None:
                current = {
                    "genres": json.loads(cls["genres"] or "[]"),
                    "moods": json.loads(cls["moods"] or "[]"),
                    "decade": cls["decade"],
                    "year": cls["year"],
                }
                if not info.get("cover_art") and cls["cover_art"]:
                    info["cover_art"] = cls["cover_art"]
            suggestion = None
            if key in suggested:
                try:
                    suggestion = json.loads(suggested[key])
                except Exception:  # noqa: BLE001
                    suggestion = None
                if suggestion and not info.get("cover_art") and suggestion.get("cover_art"):
                    info["cover_art"] = suggestion["cover_art"]
            items.append({
                **info,
                "classification": current,
                "classified": cls is not None,
                "has_suggestion": key in suggested,
                "suggestion": suggestion,
            })

        # Unclassified first, then by rating desc, then title.
        items.sort(key=lambda it: (
            it["classified"],
            -(it["rating"] or 0),
            _norm(it["album"]),
        ))
        total = len(items)
        start = max(0, (page - 1) * per_page)
        return {
            "items": items[start:start + per_page],
            "total": total,
            "page": page,
            "per_page": per_page,
        }
    finally:
        conn.close()


# ── bulk pre-warm worker ────────────────────────────────────────────────────
# Server-side so a "suggest everything" run survives the browser tab closing and
# is resumable (already-cached albums are skipped). One album at a time — ollama
# is a single CPU instance, so there is nothing to gain from concurrency.

_bulk: dict = {
    "running": False, "scope": "rated", "total": 0, "done": 0,
    "current": "", "stop": False, "started_at": 0.0, "errors": 0,
}


def bulk_status() -> dict:
    return {k: _bulk[k] for k in
            ("running", "scope", "total", "done", "current", "started_at", "errors")}


async def _bulk_worker(scope: str) -> None:
    try:
        queue = list_classification_queue(scope, 1, 1_000_000)
        targets = [
            it for it in queue["items"]
            if not it["classified"] and not it["has_suggestion"]
        ]
        _bulk["total"] = len(targets)
        _bulk["done"] = 0
        _bulk["errors"] = 0
        for it in targets:
            if _bulk["stop"]:
                break
            _bulk["current"] = it.get("album") or ""
            try:
                await suggest_for_album(it["album_key"], it["artist"], it["album"])
            except Exception as exc:  # noqa: BLE001
                _bulk["errors"] += 1
                logger.warning("music_classifier: bulk suggest failed for %s: %s",
                               it.get("album_key"), exc)
            _bulk["done"] += 1
    finally:
        _bulk["running"] = False
        _bulk["current"] = ""


def start_bulk(scope: str) -> dict:
    if _bulk["running"]:
        return {"started": False, **bulk_status()}
    _bulk.update(running=True, scope=scope, total=0, done=0,
                 current="", stop=False, started_at=time.time(), errors=0)
    asyncio.create_task(_bulk_worker(scope))
    return {"started": True, **bulk_status()}


def stop_bulk() -> dict:
    if _bulk["running"]:
        _bulk["stop"] = True
    return bulk_status()
