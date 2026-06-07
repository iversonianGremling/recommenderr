"""In-library radio.

Resolve a set of (track / artist / video_id) seeds against the user's own music
library, then rank the rest of the library by similarity — same artist/album,
shared music tags, same genre, an optional semantic embedding boost (where
catalog embeddings exist) plus a gentle rating/familiarity bias — into an
endless, diversified queue.

This is the in-library replacement for the old external `radio_service` path:
it never leaves the box (no Invidious / yt-dlp / Last.fm resolution), so it
works even while YouTube egress is blocked. The `/v1/radio` endpoint calls
`build_radio` and returns the result on the existing RadioTrack contract.
"""
from __future__ import annotations

import random

from backend.db import get_db
from backend.services import embedding_engine as emb

# --- similarity weights -----------------------------------------------------
# Tuned so a same-artist neighbour outranks a shared-tag one, but tag/semantic
# overlap can still lift a *different* artist into the rotation.
W_SAME_ARTIST = 5.0
W_SAME_ALBUM = 3.0
W_SHARED_TAG = 1.6          # per shared tag, capped at SHARED_TAG_CAP
SHARED_TAG_CAP = 3
W_SAME_GENRE = 1.2
W_EMBED = 4.0               # * max(0, cosine(seed centroid, candidate))
W_RATING = 0.35            # * effective_rating (1..10)
W_LISTEN = 0.04            # * min(listen_count, 25) -- familiarity nudge
JITTER = 0.9               # random 0..JITTER for run-to-run variety

# A candidate only qualifies for radio if it shares *some* similarity signal
# (artist/album/tag/genre/embedding); rating+listen+jitter alone would just be a
# random library shuffle.
MIN_AFFINITY = 0.01
MAX_PER_ARTIST_RUN = 2     # don't play more than N in a row by one artist
SEED_PROFILE_CAP = 60      # cap seed tracks used to build the taste profile


def _lc(s) -> str:
    return (s or "").strip().lower()


def _album_key(artist, album) -> str:
    a, b = _lc(artist), _lc(album)
    return f"{a}::{b}" if a and b else ""


def _load_library(conn) -> dict[str, dict]:
    """All music-library tracks with rating + listen metadata, keyed by video_id."""
    rows = conn.execute(
        """
        SELECT
            ml.video_id, ml.title, ml.thumbnail, ml.duration, ml.author, ml.author_id,
            ml.track, ml.artist, ml.album, ml.genre,
            CAST(COALESCE(
                vr.rating,
                (SELECT ar.rating FROM album_tracks at
                   JOIN album_ratings ar ON ar.album_key = at.album_key
                  WHERE at.video_id = ml.video_id LIMIT 1),
                cr.rating
            ) AS INTEGER) AS effective_rating,
            COALESCE((SELECT h.listen_count FROM watch_history h
                       WHERE h.video_id = ml.video_id), 0) AS listen_count
        FROM music_library ml
        LEFT JOIN video_ratings vr ON vr.video_id = ml.video_id
        LEFT JOIN channel_ratings cr ON cr.channel_id = ml.author_id
        """
    ).fetchall()
    lib: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        d["artist_lc"] = _lc(d.get("artist") or d.get("author"))
        d["album_lc"] = _album_key(d.get("artist") or d.get("author"), d.get("album"))
        lib[d["video_id"]] = d
    return lib


def _load_tags(conn) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    for r in conn.execute("SELECT video_id, tag_id FROM music_tag_assignments"):
        out.setdefault(r["video_id"], set()).add(r["tag_id"])
    return out


def _resolve_seed_vids(seeds: list[dict], lib: dict[str, dict]) -> tuple[set[str], set[str]]:
    """Map incoming seeds to library video_ids + a set of free-text seed artists.

    Returns (seed_vids, seed_artist_strings). A seed may resolve to a specific
    track (video_id or track+artist match) or, failing that, to all library
    tracks by that artist."""
    seed_vids: set[str] = set()
    seed_artists: set[str] = set()
    # index by artist for quick lookup
    by_artist: dict[str, list[str]] = {}
    for vid, d in lib.items():
        if d["artist_lc"]:
            by_artist.setdefault(d["artist_lc"], []).append(vid)

    for s in seeds:
        vid = (s.get("video_id") or "").strip()
        artist = _lc(s.get("artist"))
        track = _lc(s.get("track"))
        if artist:
            seed_artists.add(artist)
        if vid and vid in lib:
            seed_vids.add(vid)
            continue
        if artist:
            cand = by_artist.get(artist, [])
            if track:
                exact = [
                    v for v in cand
                    if _lc(lib[v].get("track")) == track or _lc(lib[v].get("title")) == track
                ]
                if exact:
                    seed_vids.update(exact)
                    continue
            # No exact track — seed from (a sample of) the artist's catalogue.
            seed_vids.update(cand[:SEED_PROFILE_CAP])
    return seed_vids, seed_artists


def _build_profile(seed_vids: set[str], seed_artists: set[str], lib: dict, tags: dict, conn):
    profile = {
        "artists": set(seed_artists),
        "albums": set(),
        "genres": set(),
        "tags": set(),
        "centroid": None,
    }
    sample = list(seed_vids)[:SEED_PROFILE_CAP]
    for vid in sample:
        d = lib.get(vid)
        if not d:
            continue
        if d["artist_lc"]:
            profile["artists"].add(d["artist_lc"])
        if d["album_lc"]:
            profile["albums"].add(d["album_lc"])
        if d.get("genre"):
            profile["genres"].add(_lc(d["genre"]))
        profile["tags"] |= tags.get(vid, set())
    # Optional semantic centroid from whatever seed tracks have embeddings.
    if sample:
        vecs = emb._load_vecs(conn, sample)
        if vecs:
            dim = len(next(iter(vecs.values())))
            profile["centroid"] = emb._weighted_centroid(
                vecs, {v: 1.0 for v in vecs}, dim
            )
    return profile


def _diversify(ranked: list[dict], limit: int) -> list[dict]:
    """Greedy interleave that caps consecutive tracks by the same artist."""
    out: list[dict] = []
    deferred: list[dict] = []
    run_artist = None
    run_len = 0
    # Walk ranked order, deferring tracks that would over-run one artist, then
    # append the deferred tail.
    for d in ranked:
        if len(out) >= limit:
            break
        a = d["artist_lc"]
        if a and a == run_artist and run_len >= MAX_PER_ARTIST_RUN:
            deferred.append(d)
            continue
        out.append(d)
        if a == run_artist:
            run_len += 1
        else:
            run_artist, run_len = a, 1
    for d in deferred:
        if len(out) >= limit:
            break
        out.append(d)
    return out[:limit]


def _to_radio_track(d: dict, score: float) -> dict:
    artist = d.get("artist") or d.get("author") or ""
    track = d.get("track") or ""
    title = d.get("title") or (f"{artist} – {track}".strip(" –") if (artist or track) else d["video_id"])
    return {
        "video_id": d["video_id"],
        "title": title,
        "thumbnail": d.get("thumbnail"),
        "duration": d.get("duration"),
        "author": d.get("author"),
        "author_id": d.get("author_id"),
        "track": track or None,
        "artist": artist or None,
        "sources": ["library"],
        "score": round(score, 4),
        "is_music_confirmed": True,
    }


def build_radio(
    seeds: list[dict],
    limit: int = 30,
    exclude_video_ids: set[str] | None = None,
) -> list[dict]:
    """Return up to `limit` in-library RadioTrack dicts similar to the seeds."""
    exclude = set(exclude_video_ids or set())
    conn = get_db()
    try:
        lib = _load_library(conn)
        if not lib:
            return []
        tags = _load_tags(conn)
        seed_vids, seed_artists = _resolve_seed_vids(seeds, lib)
        profile = _build_profile(seed_vids, seed_artists, lib, tags, conn)

        skip = exclude | seed_vids
        centroid = profile["centroid"]
        cand_ids = [v for v in lib if v not in skip]
        vecs = emb._load_vecs(conn, cand_ids) if centroid else {}
        # Whether the seed has a defined stylistic "shape". When it does, we refuse
        # to backfill the queue with tracks that share none of it (a rap seed must
        # not bleed into pagan folk). Only a shapeless seed (no tags/genre/embeds)
        # falls back to a rating-only pool so radio never dead-ends.
        profile_shape = bool(profile["tags"] or profile["genres"] or centroid is not None)

        scored: list[tuple[float, dict]] = []
        fallback: list[tuple[float, dict]] = []
        for vid in cand_ids:
            d = lib[vid]
            if d.get("effective_rating") == 1:  # hard-blocked / disliked
                continue
            affinity = 0.0
            if d["artist_lc"] and d["artist_lc"] in profile["artists"]:
                affinity += W_SAME_ARTIST
            if d["album_lc"] and d["album_lc"] in profile["albums"]:
                affinity += W_SAME_ALBUM
            if profile["tags"]:
                shared = len(tags.get(vid, set()) & profile["tags"])
                if shared:
                    affinity += W_SHARED_TAG * min(shared, SHARED_TAG_CAP)
            if d.get("genre") and _lc(d["genre"]) in profile["genres"]:
                affinity += W_SAME_GENRE
            if centroid is not None:
                v = vecs.get(vid)
                if v:
                    cs = emb._cosine(centroid, v)
                    if cs > 0:
                        affinity += W_EMBED * cs

            bias = (
                W_RATING * (d.get("effective_rating") or 0)
                + W_LISTEN * min(d.get("listen_count") or 0, 25)
                + random.uniform(0, JITTER)
            )
            if affinity >= MIN_AFFINITY:
                scored.append((affinity + bias, d))
            elif not profile_shape:
                # Shapeless seed (no tag/genre/embedding signal at all) — keep a
                # rating-only backfill so an obscure seed still yields a queue.
                fallback.append((bias, d))
            # else: the seed HAS a stylistic shape but this candidate shares none
            # of it → drop it. Coherence over queue length, per user preference.

        scored.sort(key=lambda x: x[0], reverse=True)
        ranked = [d for _, d in scored]
        if len(ranked) < limit:
            fallback.sort(key=lambda x: x[0], reverse=True)
            have = {d["video_id"] for d in ranked}
            for _, d in fallback:
                if d["video_id"] not in have:
                    ranked.append(d)
                if len(ranked) >= limit * 2:
                    break

        chosen = _diversify(ranked, limit)
        # carry each track's own total score for the response
        score_by_vid = {d["video_id"]: s for s, d in scored}
        return [_to_radio_track(d, score_by_vid.get(d["video_id"], 0.0)) for d in chosen]
    finally:
        conn.close()
