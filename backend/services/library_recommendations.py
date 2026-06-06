"""Library recommender: multi-seed weighted PPR over the user's external music
library (synced into external_music_seeds, e.g. from yamtrack).

For each highly-rated seed we expand similar content via the music APIs
(Deezer related-artists → their albums; Last.fm/Deezer/etc. for songs), build a
catalog graph, and run one Personalized PageRank weighted by the seeds' ratings
(reusing ppr_engine.compute_ppr / rating_mult). Albums and artists are ranked by
PPR; songs are derived from the lead tracks of the top recommended albums (and
from song seeds directly when present). Owned items are excluded.

Hierarchical fallback: a song seed the APIs don't know contributes via its
artist; an album seed with no related albums still contributes related artists.

Computed lists are persisted to library_rec_results; the HTTP layer reads those.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict

from backend.db import get_db
from backend.services.ppr_engine import compute_ppr, rating_mult
from backend.services.source_registry import get_weight
from backend.services.music_client import (
    deezer_search_artist,
    deezer_get_related_artists,
    deezer_get_artist_albums,
    deezer_get_album_tracks,
)
from backend.services import music_recommendations as mrec

logger = logging.getLogger("library_recommendations")

# Bounds (keep API usage sane; recompute runs in the background).
ALBUM_SEED_CAP = 50      # distinct seed artists to expand from album seeds
SONG_SEED_CAP = 25
ARTIST_SEED_CAP = 25
RELATED_PER_ARTIST = 6
ALBUMS_PER_ARTIST = 4
SONG_RECS_FROM_ALBUMS = 25
_CONCURRENCY = 8

_ALBUM_TYPE_BONUS = {"album": 1.0, "ep": 0.6, "single": 0.4, "compile": 0.5, "": 0.8}

_state = {"running": False, "last_computed": 0.0, "last_error": None}


# ---------------------------------------------------------------------------
# Catalog PPR config — this is its own independent engine (separate from the
# per-graph PPR), so it has its own key/value store: catalog_ppr_config.
# Defaults below mirror the historical hardcoded constants.
# ---------------------------------------------------------------------------

CATALOG_CONFIG_DEFAULTS: dict[str, float] = {
    "alpha": 0.15,                  # restart probability of the catalog PPR
    "album_seed_cap": float(ALBUM_SEED_CAP),
    "song_seed_cap": float(SONG_SEED_CAP),
    "related_per_artist": float(RELATED_PER_ARTIST),
    "albums_per_artist": float(ALBUMS_PER_ARTIST),
    "song_recs_from_albums": float(SONG_RECS_FROM_ALBUMS),
}


def get_catalog_config() -> dict[str, float]:
    conn = get_db()
    try:
        rows = conn.execute("SELECT key, value FROM catalog_ppr_config").fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    cfg = dict(CATALOG_CONFIG_DEFAULTS)
    for r in rows:
        if r["key"] in cfg:
            try:
                cfg[r["key"]] = float(r["value"])
            except (TypeError, ValueError):
                pass
    return cfg


def set_catalog_config(updates: dict[str, float]) -> None:
    conn = get_db()
    now = time.time()
    try:
        for k, v in updates.items():
            if k in CATALOG_CONFIG_DEFAULTS:
                conn.execute(
                    "INSERT OR REPLACE INTO catalog_ppr_config (key, value, updated_at) VALUES (?, ?, ?)",
                    (k, str(float(v)), now),
                )
        conn.commit()
    finally:
        conn.close()


def _norm(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())


def _album_key(artist: str, album: str) -> str:
    return f"{_norm(artist)}\x1f{_norm(album)}"


def _artist_key(artist: str) -> str:
    return _norm(artist)


def _seed_weight(score) -> float:
    if score is None:
        return 1.0
    try:
        w = rating_mult(round(float(score)))
    except (TypeError, ValueError):
        return 1.0
    return w if w > 0 else 0.1


# ---- bounded async helpers with per-run caches -----------------------------

class _Expander:
    def __init__(self, related_per_artist: int = RELATED_PER_ARTIST, albums_per_artist: int = ALBUMS_PER_ARTIST):
        self._sem = asyncio.Semaphore(_CONCURRENCY)
        self._id: dict[str, tuple[str | None, str]] = {}     # name -> (artist_id, image)
        self._related: dict[str, list[dict]] = {}            # artist_id -> related
        self._albums: dict[str, list[dict]] = {}             # artist_id -> albums
        self._related_per_artist = related_per_artist
        self._albums_per_artist = albums_per_artist

    async def artist_id(self, name: str) -> tuple[str | None, str]:
        if name in self._id:
            return self._id[name]
        async with self._sem:
            hits = await deezer_search_artist(name, limit=1)
        res = (str(hits[0]["deezer_artist_id"]), hits[0].get("image", "")) if hits else (None, "")
        self._id[name] = res
        return res

    async def related(self, artist_id: str) -> list[dict]:
        if artist_id in self._related:
            return self._related[artist_id]
        async with self._sem:
            rel = await deezer_get_related_artists(artist_id, limit=self._related_per_artist)
        self._related[artist_id] = rel
        return rel

    async def albums(self, artist_id: str) -> list[dict]:
        if artist_id in self._albums:
            return self._albums[artist_id]
        async with self._sem:
            alb = await deezer_get_artist_albums(artist_id, limit=self._albums_per_artist)
        self._albums[artist_id] = alb
        return alb


def _load_seeds():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT kind, artist, album, track, score FROM external_music_seeds"
        ).fetchall()
    finally:
        conn.close()
    albums, songs, artists = [], [], []
    for r in rows:
        d = {"artist": r["artist"], "album": r["album"], "track": r["track"], "score": r["score"]}
        if r["kind"] == "album":
            albums.append(d)
        elif r["kind"] == "song":
            songs.append(d)
        elif r["kind"] == "artist":
            artists.append(d)
    return albums, songs, artists


async def _compute() -> dict:
    cfg = get_catalog_config()
    alpha = float(cfg["alpha"])
    album_seed_cap = int(cfg["album_seed_cap"])
    song_seed_cap = int(cfg["song_seed_cap"])
    song_recs_from_albums = int(cfg["song_recs_from_albums"])

    albums, songs, artists = _load_seeds()

    owned_albums = {_album_key(a["artist"], a["album"]) for a in albums}
    owned_artists = {_artist_key(a["artist"]) for a in albums}
    owned_artists |= {_artist_key(a["artist"]) for a in artists}

    # Seed artists = album-seed artists (weighted by best album) + artist seeds.
    artist_weight: dict[str, float] = defaultdict(float)
    for a in albums:
        k = a["artist"].strip()
        if k:
            artist_weight[k] = max(artist_weight[k], _seed_weight(a["score"]))
    for a in artists:
        k = a["artist"].strip()
        if k:
            artist_weight[k] = max(artist_weight[k], _seed_weight(a["score"]))

    top_artists = sorted(artist_weight.items(), key=lambda x: -x[1])[:album_seed_cap]

    exp = _Expander(
        related_per_artist=int(cfg["related_per_artist"]),
        albums_per_artist=int(cfg["albums_per_artist"]),
    )
    dz = get_weight("deezer") or 0.9

    # Resolve seed artist IDs, then their related artists (bounded, cached).
    await asyncio.gather(*[exp.artist_id(name) for name, _ in top_artists])
    seed_ids = {name: exp._id.get(name, (None, ""))[0] for name, _ in top_artists}
    await asyncio.gather(*[exp.related(aid) for aid in seed_ids.values() if aid])

    # Unique related artists across all seeds → fetch their albums once each.
    related_ids: set[str] = set()
    for aid in seed_ids.values():
        if aid:
            for rel in exp._related.get(aid, []):
                if rel.get("deezer_artist_id"):
                    related_ids.add(str(rel["deezer_artist_id"]))
    await asyncio.gather(*[exp.albums(rid) for rid in related_ids])

    album_graph: dict[str, list[tuple[str, float]]] = defaultdict(list)
    artist_graph: dict[str, list[tuple[str, float]]] = defaultdict(list)
    album_meta: dict[str, dict] = {}
    artist_meta: dict[str, dict] = {}
    album_seed_vec: dict[str, float] = {}

    for name, w in top_artists:
        sk = _artist_key(name)
        album_seed_vec[sk] = max(album_seed_vec.get(sk, 0.0), w)
        aid = seed_ids.get(name)
        if not aid:
            continue
        for rel in exp._related.get(aid, []):
            rname = rel.get("artist") or ""
            rid = str(rel.get("deezer_artist_id") or "")
            rk = _artist_key(rname)
            if rk and rk not in owned_artists:
                artist_graph[sk].append((rk, dz * w))
            for alb in exp._albums.get(rid, []):
                title = alb.get("title") or ""
                if not title:
                    continue
                # Deezer's /artist/{id}/albums omits the artist sub-object, so
                # fall back to the related artist's name for display + dedup key.
                a_name = alb.get("artist") or rname
                ak = _album_key(a_name, title)
                bonus = _ALBUM_TYPE_BONUS.get((alb.get("album_type") or "").lower(), 0.8)
                album_graph[sk].append((ak, dz * w * bonus))
                album_meta.setdefault(ak, {**alb, "artist": a_name})
                if rk and rk not in artist_meta:
                    artist_meta[rk] = {"artist": rname, "image": alb.get("cover_art", "")}

    # Weighted PPR over each catalog graph.
    album_scores = compute_ppr(dict(album_graph), album_seed_vec, alpha=alpha, max_iter=80, tol=1e-5) if album_graph else {}
    artist_scores = compute_ppr(dict(artist_graph), album_seed_vec, alpha=alpha, max_iter=80, tol=1e-5) if artist_graph else {}

    album_recs = [
        {"artist": album_meta[k].get("artist", ""), "album": album_meta[k].get("title", ""),
         "cover_art": album_meta[k].get("cover_art", ""), "score": sc,
         "deezer_album_id": album_meta[k].get("deezer_album_id", ""), "sources": "deezer"}
        for k, sc in sorted(album_scores.items(), key=lambda x: -x[1])
        if k in album_meta and k not in owned_albums and sc > 0
    ]

    artist_recs = [
        {"artist": artist_meta[k].get("artist", ""), "cover_art": artist_meta[k].get("image", ""),
         "score": sc, "sources": "deezer"}
        for k, sc in sorted(artist_scores.items(), key=lambda x: -x[1])
        if k in artist_meta and k not in owned_artists and sc > 0
    ]

    # Songs: direct from song seeds (catalog PPR), else lead tracks of top albums.
    song_recs: list[dict] = []
    seen_songs: set[str] = set()
    for s in sorted(songs, key=lambda x: -_seed_weight(x["score"]))[:song_seed_cap]:
        try:
            recs = await mrec.get_recommendations(s["track"], s["artist"], limit=8)
        except Exception as exc:  # noqa: BLE001
            logger.debug("song seed recs error: %s", exc)
            recs = []
        for r in recs:
            key = mrec.track_identity_key(r.get("artist", ""), r.get("track", ""))
            if key in seen_songs:
                continue
            seen_songs.add(key)
            song_recs.append({
                "artist": r.get("artist", ""), "track": r.get("track", ""),
                "cover_art": r.get("thumbnail") or r.get("cover_art") or "",
                "video_id": r.get("video_id"), "score": float(r.get("graph_score") or 0.0),
                "sources": ",".join(r.get("sources", []) if isinstance(r.get("sources"), list) else []) or "lastfm",
            })

    # Derive songs from the top recommended albums' lead track.
    for alb in album_recs[:song_recs_from_albums]:
        if not alb.get("deezer_album_id"):
            continue
        try:
            async with exp._sem:
                tracks = await deezer_get_album_tracks(alb["deezer_album_id"], limit=1)
        except Exception:  # noqa: BLE001
            tracks = []
        if not tracks:
            continue
        t = tracks[0]
        key = mrec.track_identity_key(t.get("artist") or alb["artist"], t.get("title") or "")
        if key in seen_songs or not t.get("title"):
            continue
        seen_songs.add(key)
        song_recs.append({
            "artist": t.get("artist") or alb["artist"], "track": t.get("title"),
            "cover_art": alb.get("cover_art", ""), "video_id": None,
            "score": float(alb["score"]), "sources": "deezer",
        })

    _persist(album_recs, artist_recs, song_recs)
    return {
        "albums": len(album_recs), "artists": len(artist_recs), "songs": len(song_recs),
        "seed_artists": len(top_artists),
    }


def _persist(album_recs, artist_recs, song_recs) -> None:
    now = time.time()
    conn = get_db()
    try:
        conn.execute("DELETE FROM library_rec_results")
        rows = []
        for r in album_recs:
            rows.append(("album", r["artist"], r["album"], "", r["score"], r["cover_art"], None, r["sources"], now))
        for r in artist_recs:
            rows.append(("artist", r["artist"], "", "", r["score"], r["cover_art"], None, r["sources"], now))
        for r in song_recs:
            rows.append(("song", r["artist"], "", r["track"], r["score"], r["cover_art"], r.get("video_id"), r["sources"], now))
        conn.executemany(
            """INSERT OR REPLACE INTO library_rec_results
               (kind, artist, album, track, score, cover_art, video_id, sources, computed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


async def recompute() -> dict:
    """Run a full recompute (idempotent; guarded against concurrent runs)."""
    if _state["running"]:
        return {"status": "already_running"}
    _state["running"] = True
    _state["last_error"] = None
    try:
        result = await _compute()
        _state["last_computed"] = time.time()
        return {"status": "ok", **result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("library recompute failed")
        _state["last_error"] = str(exc)
        return {"status": "error", "error": str(exc)}
    finally:
        _state["running"] = False


def read_results(limit: int = 50) -> dict:
    conn = get_db()
    try:
        out = {}
        for kind in ("album", "artist", "song"):
            rows = conn.execute(
                "SELECT artist, album, track, score, cover_art, video_id, sources, computed_at "
                "FROM library_rec_results WHERE kind=? ORDER BY score DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
            out[kind + "s"] = [dict(r) for r in rows]
        computed_at = conn.execute("SELECT MAX(computed_at) FROM library_rec_results").fetchone()[0]
    finally:
        conn.close()
    out["computed_at"] = computed_at
    out["state"] = {"running": _state["running"], "last_error": _state["last_error"]}
    return out
