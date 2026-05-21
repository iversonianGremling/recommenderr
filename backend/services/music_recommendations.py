import os
import asyncio
import re
from collections import defaultdict

from backend.services.music_client import (
    lastfm_get_similar_tracks,
    deezer_search,
    deezer_get_related_artists,
    deezer_search_artist,
    deezer_get_artist_albums,
    deezer_get_album_tracks,
    deezer_search_album,
    spotify_search,
    spotify_get_recommendations,
    itunes_search,
)
from backend.services.invidious_client import api_get
from backend.services.ppr_engine import compute_ppr

NON_MUSIC_VIDEO_RE = re.compile(
    r"\b(review|reaction|meme|shitpost|analysis|explained|podcast|interview|breakdown|parody|fancam|fan cam|edit|amv)\b",
    re.IGNORECASE,
)
CANONICAL_MUSIC_VIDEO_RE = re.compile(
    r"\b(official (?:music )?video|official audio|lyrics?|lyric video|visualizer|audio)\b",
    re.IGNORECASE,
)
SOURCE_WEIGHTS = {
    "spotify": 1.0,
    "deezer": 0.9,
    "deezer_discography": 0.93,
    "lastfm": 0.85,
    "itunes": 0.55,
}


def _norm_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def track_identity_key(artist: str, track: str) -> str:
    """Stable graph node id for catalog-first recommendation graph."""
    a = _norm_text(artist)
    t = _norm_text(track)
    return f"{a}\x1f{t}"


def _source_confidence(item: dict) -> float:
    sources = list(dict.fromkeys([s for s in (item.get("_sources") or []) if s]))
    if not sources:
        one = item.get("source")
        sources = [one] if one else []
    if not sources:
        return 0.0
    avg_weight = sum(SOURCE_WEIGHTS.get(source, 0.5) for source in sources) / len(sources)
    diversity_bonus = min(len(sources), 4) * 0.11
    consensus_bonus = min(float(item.get("_count") or 1), 5.0) * 0.04
    return round(min(1.0, (avg_weight * 0.62) + diversity_bonus + consensus_bonus), 3)


def _score_video_result(video: dict, artist: str, track: str, position: int) -> float:
    title = video.get("title") or ""
    author = video.get("author") or ""
    lowered_title = title.lower()
    lowered_author = author.lower()
    if NON_MUSIC_VIDEO_RE.search(title) or NON_MUSIC_VIDEO_RE.search(author):
        return -1.0

    artist_norm = _norm_text(artist)
    track_norm = _norm_text(track)
    corpus = f"{_norm_text(title)} {_norm_text(author)}"

    score = max(0.0, 0.25 - (position * 0.01))
    if artist_norm and artist_norm in corpus:
        score += 0.3
    if track_norm and track_norm in corpus:
        score += 0.34
    if artist_norm and lowered_author.endswith(" - topic"):
        score += 0.18
    if CANONICAL_MUSIC_VIDEO_RE.search(lowered_title):
        score += 0.18

    duration = int(video.get("lengthSeconds") or 0)
    if 90 <= duration <= 540:
        score += 0.08
    elif duration >= 1200:
        score -= 0.25
    elif 0 < duration < 45:
        score -= 0.2

    if artist_norm and track_norm and (artist_norm not in corpus or track_norm not in corpus):
        score -= 0.18

    return round(score, 3)


def _merge_catalog_items(raw_items: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for item in raw_items:
        a = (item.get("artist") or "").strip()
        t = (item.get("track") or "").strip()
        key = (a.lower(), t.lower())
        if not key[0] and not key[1]:
            continue
        if key in seen:
            existing = seen[key]
            existing_sources = list(dict.fromkeys([s for s in (existing.get("_sources") or []) if s]))
            src = item.get("source")
            if src and src not in existing_sources:
                existing_sources.append(src)
            existing["_sources"] = existing_sources
            existing["_count"] = existing.get("_count", 1) + 1
            existing["album"] = existing.get("album") or item.get("album") or ""
        else:
            item_copy = dict(item)
            item_copy["_count"] = 1
            src = item.get("source")
            item_copy["_sources"] = [src] if src else []
            seen[key] = item_copy

    deduped = list(seen.values())
    for item in deduped:
        item["_metadata_confidence"] = _source_confidence(item)
    deduped.sort(
        key=lambda x: (
            -(x.get("_metadata_confidence") or 0.0),
            -x.get("_count", 1),
            x.get("track", ""),
        )
    )
    return deduped


async def _gather_service_recommendation_rows(track: str, artist: str, limit: int) -> list[dict]:
    async def _lastfm_recs() -> list[dict]:
        try:
            items = await lastfm_get_similar_tracks(track, artist, limit=limit)
            return [{"track": i.get("track", ""), "artist": i.get("artist", ""), "source": "lastfm"} for i in items]
        except Exception:
            return []

    async def _deezer_recs() -> list[dict]:
        try:
            seed = await deezer_search(f"{artist} {track}", limit=1)
            if not seed:
                return []
            artist_id = seed[0].get("deezer_artist_id")
            if not artist_id:
                return []
            related = await deezer_get_related_artists(artist_id, limit=5)
            recs = []
            for rel in related:
                try:
                    hits = await deezer_search(rel["artist"], limit=1)
                    if hits:
                        h = hits[0]
                        recs.append({
                            "track": h.get("track", ""),
                            "artist": h.get("artist", ""),
                            "album": h.get("album", ""),
                            "source": "deezer",
                        })
                except Exception:
                    pass
            return recs
        except Exception:
            return []

    async def _spotify_recs() -> list[dict]:
        try:
            seed = await spotify_search(f"{artist} {track}", limit=1)
            if not seed:
                return []
            seed_id = seed[0].get("spotify_track_id")
            if not seed_id:
                return []
            items = await spotify_get_recommendations([seed_id], limit=limit)
            return [
                {
                    "track": i["track"],
                    "artist": i["artist"],
                    "album": i.get("album", ""),
                    "source": "spotify",
                }
                for i in items
            ]
        except Exception:
            return []

    async def _itunes_recs() -> list[dict]:
        try:
            items = await itunes_search(f"{artist}", limit=10)
            out = []
            for i in items:
                t = i.get("track", "")
                if t.lower().strip() != track.lower().strip():
                    out.append({"track": t, "artist": i.get("artist", artist), "album": i.get("album", ""), "source": "itunes"})
            return out
        except Exception:
            return []

    lastfm_items, deezer_items, spotify_items, itunes_items = await asyncio.gather(
        _lastfm_recs(), _deezer_recs(), _spotify_recs(), _itunes_recs()
    )
    return _merge_catalog_items(lastfm_items + deezer_items + spotify_items + itunes_items)


async def _expand_lastfm_second_hop(
    hop1_items: list[dict],
    *,
    max_roots: int,
    sim_limit: int,
) -> tuple[list[tuple[str, str, float]], list[dict]]:
    """For each high-confidence 1-hop track, pull Last.fm similar tracks (2-hop), like related-video expansion."""
    roots = [
        it for it in hop1_items[:max_roots]
        if (it.get("track") or "").strip() and (it.get("artist") or "").strip()
    ]
    if not roots:
        return [], []

    tasks = [lastfm_get_similar_tracks(r["track"], r["artist"], limit=sim_limit) for r in roots]
    batches = await asyncio.gather(*tasks, return_exceptions=True)

    edges: list[tuple[str, str, float]] = []
    extra_rows: list[dict] = []

    for root, batch in zip(roots, batches):
        if isinstance(batch, Exception) or not batch:
            continue
        src_key = track_identity_key(root["artist"], root["track"])
        src_conf = float(root.get("_metadata_confidence") or 0.55)
        for sim in batch:
            t = sim.get("track") or ""
            a = sim.get("artist") or ""
            if not t.strip() or not a.strip():
                continue
            tgt_key = track_identity_key(a, t)
            if tgt_key == src_key:
                continue
            lf_match = float(sim.get("match") or 0.0)
            w = round(max(0.08, min(1.0, lf_match * 0.92 * max(0.35, src_conf))), 4)
            edges.append((src_key, tgt_key, w))
            extra_rows.append({
                "track": t,
                "artist": a,
                "album": "",
                "source": "lastfm",
                "_lf_hop2": True,
                "_lf_parent_key": src_key,
                "_lf_edge_weight": w,
            })

    return edges, extra_rows


async def _attach_youtube_to_catalog_item(item: dict, graph_score: float) -> dict:
    """Resolve catalog identity to an Invidious/YouTube row (same as ``get_recommendations`` attach step)."""
    video_hit = await _find_youtube_match(item, relaxed=False)
    if not video_hit or not video_hit.get("video_id"):
        video_hit = await _find_youtube_match(item, relaxed=True)
    return _catalog_row_to_output(item, graph_score=graph_score, video_row=video_hit)


def _catalog_row_to_output(item: dict, *, graph_score: float, video_row: dict | None) -> dict:
    t = item.get("track", "")
    a = item.get("artist", "")
    if video_row and video_row.get("video_id"):
        merged = {**video_row, "graph_score": graph_score}
        return merged
    title_bits = [x for x in (t, a) if x]
    catalog_title = " — ".join(title_bits) if title_bits else (t or a or "Unknown track")
    return {
        "track": t,
        "artist": a,
        "album": item.get("album", ""),
        "source": ",".join(item.get("_sources", [item.get("source", "")])),
        "video_id": "",
        "title": catalog_title,
        "author": a,
        "thumbnail": None,
        "lengthSeconds": None,
        "metadata_confidence": item.get("_metadata_confidence"),
        "video_match_score": None,
        "recommendation_score": round(
            (float(item.get("_metadata_confidence") or 0.0) * 0.74) + (graph_score * 0.26),
            3,
        ),
        "graph_score": graph_score,
    }


async def _find_youtube_match(item: dict, *, relaxed: bool) -> dict | None:
    t = item.get("track", "")
    a = item.get("artist", "")
    q = f"{a} {t}".strip()
    if not q:
        return None
    try:
        results = await api_get("/search", {"q": q, "type": "video"})
        if not results or not isinstance(results, list):
            return None
        scan = 20 if relaxed else 12
        threshold = 0.38 if relaxed else None
        metadata_confidence = float(item.get("_metadata_confidence") or 0.0)
        if threshold is None:
            threshold = 0.5 if metadata_confidence < 0.55 else 0.43

        scored_results: list[tuple[float, dict]] = []
        for index, video in enumerate(results[:scan]):
            if not isinstance(video, dict):
                continue
            score = _score_video_result(video, a, t, index)
            if score >= threshold:
                scored_results.append((score, video))
        if not scored_results:
            return None
        scored_results.sort(key=lambda pair: pair[0], reverse=True)
        top_video_score, v = scored_results[0]
        thumb = None
        thumbs = v.get("videoThumbnails", [])
        if thumbs:
            thumb = thumbs[0].get("url")
        rec_score = round((float(item.get("_metadata_confidence") or 0.0) * 0.62) + (top_video_score * 0.38), 3)
        return {
            "track": t,
            "artist": a,
            "album": item.get("album", ""),
            "source": ",".join(item.get("_sources", [item.get("source", "")])),
            "video_id": v.get("videoId", ""),
            "title": v.get("title", ""),
            "author": v.get("author"),
            "thumbnail": thumb,
            "lengthSeconds": v.get("lengthSeconds"),
            "metadata_confidence": item.get("_metadata_confidence"),
            "video_match_score": top_video_score,
            "recommendation_score": rec_score,
        }
    except Exception:
        return None


async def try_resolve_youtube_match(catalog: dict) -> dict | None:
    """Worker helper: strict Invidious match, then a looser fallback."""
    hit = await _find_youtube_match(catalog, relaxed=False)
    if hit and hit.get("video_id"):
        return hit
    return await _find_youtube_match(catalog, relaxed=True)


async def get_recommendations(track: str, artist: str, limit: int = 10) -> list[dict]:
    """Catalog-first music recommendations with a small PPR graph (API edges), YouTube/Invidious as attach step."""
    if os.getenv("DISABLE_EXTERNAL_APIS", "0") == "1":
        return []
    seed_track = (track or "").strip()
    seed_artist = (artist or "").strip()
    seed_key = track_identity_key(seed_artist, seed_track)
    if not _norm_text(seed_track) and not _norm_text(seed_artist):
        return []

    api_budget = max(limit + 10, 18)
    first_hop = await _gather_service_recommendation_rows(seed_track, seed_artist, limit=api_budget)
    if not first_hop:
        return []

    hop_edges, hop2_rows = await _expand_lastfm_second_hop(
        first_hop,
        max_roots=min(5, max(1, len(first_hop))),
        sim_limit=8,
    )

    merged_extra = _merge_catalog_items(hop2_rows) if hop2_rows else []
    items_by_key: dict[str, dict] = {}

    for it in first_hop:
        k = track_identity_key(it.get("artist") or "", it.get("track") or "")
        items_by_key[k] = it
    for it in merged_extra:
        k = track_identity_key(it.get("artist") or "", it.get("track") or "")
        if k == seed_key:
            continue
        if k not in items_by_key:
            items_by_key[k] = it
        else:
            cur = items_by_key[k]
            cur_sources = list(dict.fromkeys([*(cur.get("_sources") or []), *(it.get("_sources") or [])]))
            cur["_sources"] = [s for s in cur_sources if s]
            cur["_count"] = cur.get("_count", 1) + it.get("_count", 1)
            cur["_metadata_confidence"] = _source_confidence(cur)

    graph: dict[str, list[tuple[str, float]]] = defaultdict(list)

    for it in first_hop:
        tgt = track_identity_key(it.get("artist") or "", it.get("track") or "")
        if tgt == seed_key:
            continue
        w = max(0.1, float(it.get("_metadata_confidence") or 0.35))
        graph[seed_key].append((tgt, round(w, 4)))

    for src_key, tgt_key, w in hop_edges:
        if tgt_key == seed_key:
            continue
        graph[src_key].append((tgt_key, w))

    ranked_keys: list[tuple[str, float]] = []
    if graph:
        seeds = {seed_key: 1.0}
        scores = compute_ppr(dict(graph), seeds, alpha=0.14, max_iter=80, tol=1e-5)
        for k, sc in scores.items():
            if k == seed_key:
                continue
            if k not in items_by_key:
                continue
            ranked_keys.append((k, float(sc)))
        ranked_keys.sort(key=lambda x: -x[1])
    else:
        for it in first_hop:
            k = track_identity_key(it.get("artist") or "", it.get("track") or "")
            if k == seed_key:
                continue
            ranked_keys.append((k, float(it.get("_metadata_confidence") or 0.0)))
        ranked_keys.sort(key=lambda x: -x[1])

    seen_out: set[str] = set()
    ordered_items: list[tuple[dict, float]] = []
    for k, gscore in ranked_keys:
        if k in seen_out:
            continue
        it = items_by_key.get(k)
        if not it:
            continue
        seen_out.add(k)
        ordered_items.append((it, gscore))
        if len(ordered_items) >= limit * 2:
            break

    if not ordered_items:
        for it in first_hop:
            k = track_identity_key(it.get("artist") or "", it.get("track") or "")
            if k != seed_key and k not in seen_out:
                ordered_items.append((it, float(it.get("_metadata_confidence") or 0.0)))
                seen_out.add(k)
            if len(ordered_items) >= limit:
                break

    attached = await asyncio.gather(
        *[_attach_youtube_to_catalog_item(it, gs) for it, gs in ordered_items[:limit]]
    )
    out = [row for row in attached if row.get("track") or row.get("title")]
    out.sort(
        key=lambda row: (
            -(float(row.get("graph_score") or 0.0)),
            -(float(row.get("recommendation_score") or 0.0)),
            -(float(row.get("metadata_confidence") or 0.0)),
            -(float(row.get("video_match_score") or 0.0) if row.get("video_match_score") is not None else 0.0),
        )
    )
    return out[:limit]


def dedupe_music_recommendation_rows(rows: list[dict]) -> list[dict]:
    """Preserve order; prefer first occurrence per YouTube id, Bandcamp URL, or track identity."""
    seen_vid: set[str] = set()
    seen_bc: set[str] = set()
    seen_key: set[str] = set()
    out: list[dict] = []
    for r in rows:
        vid = (r.get("video_id") or "").strip()
        if vid:
            if vid in seen_vid:
                continue
            seen_vid.add(vid)
            out.append(r)
            continue
        bc = (r.get("bandcamp_url") or "").strip()
        if bc:
            if bc in seen_bc:
                continue
            seen_bc.add(bc)
            out.append(r)
            continue
        k = track_identity_key(r.get("artist") or "", r.get("track") or "")
        if not k or k in seen_key:
            continue
        seen_key.add(k)
        out.append(r)
    return out


async def get_same_artist_catalog_tracks(
    artist: str,
    seed_album: str | None,
    exclude_track: str | None,
    *,
    limit: int = 8,
) -> list[dict]:
    """Tracks from other albums by the same artist (Deezer), resolved to playable YouTube ids."""
    ar = (artist or "").strip()
    if not ar:
        return []

    artists = await deezer_search_artist(ar, limit=5)
    if not artists:
        return []
    artist_id = artists[0].get("deezer_artist_id")
    if not artist_id:
        return []

    albums_all = await deezer_get_artist_albums(artist_id, limit=40)
    seed_album_n = _norm_text(seed_album or "")
    albums: list[dict] = []
    for a in albums_all:
        title = (a.get("title") or "").strip()
        if seed_album_n and _norm_text(title) == seed_album_n:
            continue
        if not a.get("deezer_album_id"):
            continue
        albums.append(a)
        if len(albums) >= 14:
            break

    ex_tr = _norm_text(exclude_track or "")
    catalog_pairs: list[tuple[dict, float]] = []
    g0 = 0.78
    for ai, al in enumerate(albums):
        if len(catalog_pairs) >= max(limit * 4, 24):
            break
        aid = al.get("deezer_album_id")
        al_title = (al.get("title") or "").strip()
        tracks = await deezer_get_album_tracks(aid, limit=28)
        taken = 0
        for tr in tracks:
            tn = (tr.get("title") or "").strip()
            if ex_tr and _norm_text(tn) == ex_tr:
                continue
            ta = (tr.get("artist") or ar).strip()
            item = {
                "track": tn,
                "artist": ta,
                "album": al_title,
                "source": "deezer_discography",
                "_sources": ["deezer_discography"],
                "_metadata_confidence": 0.72,
            }
            catalog_pairs.append((item, round(g0 - ai * 0.018 - taken * 0.006, 4)))
            taken += 1
            if taken >= 2:
                break

    if not catalog_pairs:
        return []

    sem = asyncio.Semaphore(5)

    async def _one(pair: tuple[dict, float]) -> dict:
        it, gs = pair
        async with sem:
            return await _attach_youtube_to_catalog_item(it, gs)

    cap = max(limit * 4, 28)
    attached = await asyncio.gather(*[_one(p) for p in catalog_pairs[:cap]])
    resolved = [r for r in attached if (r.get("video_id") or "").strip()]
    resolved.sort(
        key=lambda row: (
            -(float(row.get("graph_score") or 0.0)),
            -(float(row.get("recommendation_score") or 0.0)),
        )
    )
    return resolved[:limit]


async def resolve_bandcamp_recommendation_row(row: dict) -> dict:
    """Try Invidious search, then Deezer album track picks, so Bandcamp sidebar rows get playable videos."""
    if (row.get("video_id") or "").strip():
        return row
    base = dict(row)
    hit = await try_resolve_youtube_match(base)
    if hit and hit.get("video_id"):
        merged = {**base, **hit}
        merged["bandcamp_url"] = base.get("bandcamp_url")
        merged.setdefault("source", "bandcamp")
        return merged

    artist = (base.get("artist") or "").strip()
    album_guess = (base.get("track") or "").strip()
    if not artist or not album_guess:
        return base

    albs = await deezer_search_album(f"{artist} {album_guess}", limit=4)
    ar_norm = _norm_text(artist)
    for al in albs:
        al_ar = _norm_text((al.get("artist") or ""))
        if al_ar and ar_norm and al_ar not in ar_norm and ar_norm not in al_ar:
            continue
        aid = al.get("deezer_album_id")
        if not aid:
            continue
        tracks = await deezer_get_album_tracks(aid, limit=14)
        for t in tracks:
            it = {
                "track": (t.get("title") or "").strip(),
                "artist": (t.get("artist") or artist).strip(),
                "album": (al.get("title") or "").strip(),
                "source": "bandcamp,deezer",
                "_sources": ["bandcamp", "deezer"],
                "_metadata_confidence": 0.65,
            }
            if not it["track"]:
                continue
            vh = await _find_youtube_match(it, relaxed=False)
            if not vh or not vh.get("video_id"):
                vh = await _find_youtube_match(it, relaxed=True)
            if vh and vh.get("video_id"):
                merged = {**base, **vh}
                merged["bandcamp_url"] = base.get("bandcamp_url")
                merged["album"] = it.get("album", "")
                merged["track"] = it.get("track", merged.get("track"))
                merged["artist"] = it.get("artist", merged.get("artist"))
                merged["source"] = "bandcamp,deezer"
                merged["graph_score"] = float(base.get("graph_score") or 0.82)
                return merged
    return base


async def get_playlist_aggregate_recommendations(
    seeds: list[tuple[str, str]],
    *,
    exclude_video_ids: set[str] | None = None,
    per_seed_limit: int = 8,
    out_limit: int = 24,
    max_concurrent: int = 4,
) -> list[dict]:
    """
    Merge per-track `get_recommendations` lists with round-robin ordering so each seed
    contributes before any list dominates; dedupe by video id and catalog identity key.
    """
    exclude_video_ids = exclude_video_ids or set()
    uniq: list[tuple[str, str]] = []
    seen_seed: set[str] = set()
    for tr, ar in seeds:
        a = (ar or "").strip()
        t = (tr or "").strip()
        sk = track_identity_key(a, t)
        if sk in seen_seed:
            continue
        seen_seed.add(sk)
        uniq.append((t, a))
    if not uniq:
        return []

    sem = asyncio.Semaphore(max_concurrent)

    async def _one(seed: tuple[str, str]) -> list[dict]:
        tr, ar = seed
        async with sem:
            return await get_recommendations(tr, ar, limit=per_seed_limit)

    groups = await asyncio.gather(*[_one(s) for s in uniq])

    filtered_groups: list[list[dict]] = []
    for g in groups:
        fg: list[dict] = []
        for r in g:
            vid = (r.get("video_id") or "").strip()
            if vid and vid in exclude_video_ids:
                continue
            fg.append(r)
        filtered_groups.append(fg)

    seen_vid: set[str] = set()
    seen_key: set[str] = set()
    out: list[dict] = []
    indices = [0] * len(filtered_groups)

    while len(out) < out_limit:
        progressed = False
        for i in range(len(filtered_groups)):
            if len(out) >= out_limit:
                break
            while indices[i] < len(filtered_groups[i]):
                rec = filtered_groups[i][indices[i]]
                indices[i] += 1
                vid = (rec.get("video_id") or "").strip()
                tkey = track_identity_key(rec.get("artist") or "", rec.get("track") or "")
                if vid and vid in seen_vid:
                    continue
                if tkey in seen_key:
                    continue
                if vid:
                    seen_vid.add(vid)
                seen_key.add(tkey)
                out.append(rec)
                progressed = True
                break
        if not progressed:
            break

    return out
