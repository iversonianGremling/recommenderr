# Extracted from ytfrontend/backend/services/database.py. Functions touching tables owned by other apps
# (subscriptions→ytvideo, artist_follows→ytmusic) are temporary stubs until those apps have REST endpoints.

import sqlite3
import os
import time
import random
import re

_DB_PATH_DEFAULT = "/opt/recommenderr/data/recommenderr.db"


def _db_path() -> str:
    return os.getenv("DB_PATH", _DB_PATH_DEFAULT)


def _norm_token(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def normalize_album_key(album_title: str | None, album_artist: str | None = None) -> str:
    title = _norm_token(album_title)
    artist = _norm_token(album_artist)
    if title and artist:
        return f"{artist}::{title}"
    return title or artist


def normalize_artist_key(artist_name: str | None) -> str:
    return _norm_token(artist_name)


def _ensure_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def get_db() -> sqlite3.Connection:
    path = _db_path()
    _ensure_dir(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Subscriptions (temporary — will move to ytvideo REST endpoint)
# ---------------------------------------------------------------------------

def get_subscriptions():
    conn = get_db()
    rows = conn.execute("SELECT * FROM subscriptions ORDER BY channel_name COLLATE NOCASE").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Video ratings helpers
# ---------------------------------------------------------------------------

def get_ratings_for_video_ids(video_ids: list[str]) -> dict[str, int]:
    if not video_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(video_ids))
    rows = conn.execute(
        f"SELECT video_id, CAST(rating AS INTEGER) AS rating FROM video_ratings WHERE video_id IN ({placeholders})",
        video_ids,
    ).fetchall()
    conn.close()
    return {str(r["video_id"]): int(r["rating"]) for r in rows}


def get_playlists_for_video_ids(video_ids: list[str], per_video_limit: int = 6) -> dict[str, list[dict]]:
    """Local playlist titles that contain each video (Invidious ids)."""
    if not video_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(video_ids))
    rows = conn.execute(
        f"""
        SELECT pv.video_id AS video_id, p.id AS playlist_id, p.title AS playlist_title
        FROM playlist_videos pv
        JOIN playlists p ON p.id = pv.playlist_id
        WHERE pv.video_id IN ({placeholders})
        """,
        video_ids,
    ).fetchall()
    conn.close()
    buckets: dict[str, list[tuple[int, str]]] = {}
    for r in rows:
        vid = str(r["video_id"])
        pid = int(r["playlist_id"])
        title = r["playlist_title"] or f"Playlist {pid}"
        buckets.setdefault(vid, []).append((pid, title))
    per: dict[str, list[dict]] = {}
    for vid, pairs in buckets.items():
        pairs.sort(key=lambda t: (t[1].lower(), t[0]))
        per[vid] = [{"id": pid, "title": title} for pid, title in pairs[:per_video_limit]]
    return per


def get_watch_progress(video_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM watch_progress WHERE video_id = ?', (video_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Music-labeled channels (subscriptions surface)
# ---------------------------------------------------------------------------

def get_music_labeled_channel_ids() -> set[str]:
    """Channels tagged *music* or assigned under a category tree that contains the name *music*."""
    conn = get_db()
    ids: set[str] = set()
    for r in conn.execute(
        """
        SELECT ct.channel_id FROM channel_tags ct
        INNER JOIN tags t ON t.id = ct.tag_id
        WHERE LOWER(t.name) = 'music'
        """
    ).fetchall():
        ids.add(str(r["channel_id"]))
    for r in conn.execute(
        """
        SELECT cca.channel_id FROM channel_category_assignments cca
        WHERE cca.category_id IS NOT NULL
        AND EXISTS (
            WITH RECURSIVE cat_anc AS (
                SELECT id, name, parent_id FROM categories WHERE id = cca.category_id
                UNION ALL
                SELECT c.id, c.name, c.parent_id FROM categories c INNER JOIN cat_anc ON c.id = cat_anc.parent_id
            )
            SELECT 1 FROM cat_anc WHERE LOWER(name) = 'music' LIMIT 1
        )
        """
    ).fetchall():
        ids.add(str(r["channel_id"]))
    conn.close()
    return ids


# ---------------------------------------------------------------------------
# Feed / recommendation edges / PPR
# ---------------------------------------------------------------------------

def _published_ts_from_invidious_rec(r: dict) -> float | None:
    """Invidious uses `published` as unix seconds for the real upload / public time."""
    p = r.get("published")
    if p is None:
        return None
    try:
        v = float(p)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def save_recommendations(
    source_video_id: str,
    source_video_title: str,
    recs: list,
    max_save: int = 10,
    graph_ids: list[int] | None = None,
):
    """Save a random sample of recommendations from a watched video.

    graph_ids: which graphs this source feeds. If None, falls back to graph 1 (Mixed).
    """
    if not recs:
        return
    if graph_ids is None:
        graph_ids = [1]

    conn = get_db()
    existing = conn.execute(
        "SELECT 1 FROM feed_recommendations WHERE source_video_id = ? LIMIT 1", (source_video_id,)
    ).fetchone()
    if existing:
        # Ensure graph membership even if metadata already exists
        for gid in graph_ids:
            conn.execute(
                "INSERT OR IGNORE INTO graph_feed_items (graph_id, video_id, source_video_id, added_at) "
                "SELECT ?, video_id, source_video_id, added_at FROM feed_recommendations "
                "WHERE source_video_id = ?",
                (gid, source_video_id),
            )
        conn.commit()
        conn.close()
        return

    sample = random.sample(recs, min(max_save, len(recs)))
    now = time.time()
    saved_vids = []
    for r in sample:
        # Accept both pre-mapped (standardised) and raw Invidious field names.
        vid = r.get("video_id") or r.get("videoId", "")
        if not vid:
            continue
        thumb = r.get("thumbnail") or ""
        if not thumb and r.get("videoThumbnails"):
            thumb = r["videoThumbnails"][0].get("url", "")
        pub = _published_ts_from_invidious_rec(r)
        conn.execute(
            "INSERT OR IGNORE INTO feed_recommendations "
            "(video_id, title, thumbnail, duration, author, author_id, source_video_id, source_video_title, added_at, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                vid,
                r.get("title", ""),
                thumb,
                r.get("duration") or r.get("lengthSeconds"),
                r.get("author") or r.get("uploader", ""),
                r.get("author_id") or r.get("authorId", ""),
                source_video_id,
                source_video_title,
                now,
                pub,
            )
        )
        saved_vids.append(vid)
    conn.commit()

    # Per-graph feed membership
    for gid in graph_ids:
        for vid in saved_vids:
            conn.execute(
                "INSERT OR IGNORE INTO graph_feed_items (graph_id, video_id, source_video_id, added_at) VALUES (?,?,?,?)",
                (gid, vid, source_video_id, now),
            )
    conn.commit()

    # Insert edges for PPR graph
    for vid in saved_vids:
        conn.execute(
            "INSERT OR IGNORE INTO recommendation_edges (source_video_id, target_video_id, weight, added_at) VALUES (?, ?, ?, ?)",
            (source_video_id, vid, 1.0, now)
        )
    conn.commit()

    # Safety-valve: prevent unbounded growth if PPR recompute stops running.
    conn.execute("""
        DELETE FROM feed_recommendations WHERE id NOT IN (
            SELECT id FROM feed_recommendations ORDER BY added_at DESC LIMIT 50000
        )
    """)
    conn.commit()

    # Categorize new videos
    from backend.services.categorizer import bulk_categorize
    cat_videos = [
        {"video_id": vid, "title": r.get("title", ""), "author": r.get("author") or r.get("uploader", "")}
        for r in sample
        if (vid := r.get("videoId") or r.get("video_id", ""))
    ]
    if cat_videos:
        bulk_categorize(cat_videos)

    # Invalidate PPR scores for affected graphs
    for gid in graph_ids:
        conn.execute("DELETE FROM ppr_scores WHERE graph_id=?", (gid,))
    conn.commit()

    conn.close()


def get_feed_recommendations_for_source(source_video_id: str, limit: int = 24) -> list[dict]:
    """Targets previously saved for this source video (Invidious related list snapshot)."""
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT video_id, title, thumbnail, duration, author, author_id, source_video_id, added_at, published_at
            FROM feed_recommendations
            WHERE source_video_id = ?
            ORDER BY added_at DESC
            LIMIT ?
            """,
            (source_video_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _diversify_ppr_feed_rows(rows: list[dict], out_limit: int) -> list[dict]:
    """Spread recommendations across channels: strong channels get a higher per-page quota."""
    if not rows or out_limit <= 0:
        return rows[:out_limit]

    def ch_key(r: dict) -> str:
        aid = r.get("author_id")
        if aid:
            return str(aid)
        return (r.get("author") or "").strip().lower() or "_unknown"

    by_channel: dict[str, list[dict]] = {}
    for r in rows:
        by_channel.setdefault(ch_key(r), []).append(r)

    scores = {k: float(v[0].get("effective_ppr_score") or 0) for k, v in by_channel.items() if v}
    gmax = max(scores.values()) if scores else 1e-12

    def quota_for(ch: str) -> int:
        top = scores.get(ch, 0.0)
        ratio = top / gmax if gmax > 0 else 0.0
        return max(1, min(6, 1 + int(ratio * 5)))

    quotas = {ch: quota_for(ch) for ch in by_channel}
    ch_order = sorted(by_channel.keys(), key=lambda k: scores[k], reverse=True)
    taken = {k: 0 for k in ch_order}
    out: list[dict] = []

    while len(out) < out_limit:
        progressed = False
        for k in ch_order:
            if len(out) >= out_limit:
                break
            if taken[k] >= quotas[k]:
                continue
            bucket = by_channel[k]
            if not bucket:
                continue
            out.append(bucket.pop(0))
            taken[k] += 1
            progressed = True
        if not progressed:
            break

    if len(out) < out_limit:
        remainder: list[dict] = []
        for k in ch_order:
            remainder.extend(by_channel[k])
        remainder.sort(key=lambda r: float(r.get("effective_ppr_score") or 0), reverse=True)
        seen = {r["video_id"] for r in out}
        for r in remainder:
            if len(out) >= out_limit:
                break
            vid = r.get("video_id")
            if vid in seen:
                continue
            out.append(r)
            seen.add(vid)

    return out[:out_limit]


def get_ppr_feed(limit: int = 100, offset: int = 0, category: str = None, sort: str = 'score',
                 max_spam_mass: float = 1.0, _skip_recompute: bool = False,
                 persona_id: int | None = None, graph_id: int = 1):
    """Return PPR-ranked recommendations, optionally filtered by category/persona/graph.

    When persona_id is set, scores come from persona_scores for that persona instead of
    the global ppr_scores table. graph_id selects which graph's ppr_scores to use
    (default 1 = mixed/default graph).
    """
    conn = get_db()

    if not _skip_recompute and persona_id is None:
        row = conn.execute(
            "SELECT MIN(computed_at) as oldest FROM ppr_scores WHERE graph_id=?", (graph_id,)
        ).fetchone()
        if not row or not row["oldest"] or (time.time() - row["oldest"]) > 300:
            conn.close()
            from backend.services.ppr_engine import update_ppr_scores
            update_ppr_scores(graph_id=graph_id, compute_spam_mass=(max_spam_mass < 1.0))
            conn = get_db()

    params = []
    category_filter = ""
    if category:
        category_filter = "AND (vc.category = ? OR vc.category LIKE ?)"
        params.append(category)
        params.append(category + "/%")

    spam_filter = ""
    if max_spam_mass < 1.0:
        spam_filter = "AND (ppr.spam_mass IS NULL OR ppr.spam_mass <= ?)"
        params.append(max_spam_mass)

    sql_limit = min(limit * 4, 600) if sort == 'score' else limit
    params.extend([sql_limit, offset])

    _RATING_MULT_SQL = """CASE
        WHEN COALESCE(vr_vid.rating, ar.rating, cr.rating) IS NULL THEN 1.0
        WHEN CAST(COALESCE(vr_vid.rating, ar.rating, cr.rating) AS INTEGER) <= 1 THEN 0.0
        WHEN CAST(COALESCE(vr_vid.rating, ar.rating, cr.rating) AS INTEGER) <= 5
            THEN CAST(COALESCE(vr_vid.rating, ar.rating, cr.rating) AS REAL) / 5.0
        ELSE (CAST(COALESCE(vr_vid.rating, ar.rating, cr.rating) AS INTEGER) - 4)
           * (CAST(COALESCE(vr_vid.rating, ar.rating, cr.rating) AS INTEGER) - 4) * 1.0
    END"""

    _RECENCY_MULT_SQL = """(1.0 + CASE
        WHEN (CAST(strftime('%s','now') AS REAL) - COALESCE(MAX(fr.published_at), MAX(fr.added_at))) < 0 THEN 0.0
        ELSE min(0.08, 0.08 * max(0.0, 1.0 - min(1.0,
            (CAST(strftime('%s','now') AS REAL) - COALESCE(MAX(fr.published_at), MAX(fr.added_at))) / 1209600.0)))
    END)"""
    _EFFECTIVE_PPR_SQL = f"(COALESCE(ppr.score, 0) * {_RATING_MULT_SQL}) * {_RECENCY_MULT_SQL}"

    order_clause = f"{_EFFECTIVE_PPR_SQL} DESC, fr.added_at DESC"
    if sort == 'date':
        order_clause = "COALESCE(MAX(fr.published_at), MAX(fr.added_at)) DESC"
    elif sort == 'title':
        order_clause = "LOWER(fr.title) ASC"
    elif sort == 'channel':
        order_clause = f"LOWER(fr.author) ASC, {_EFFECTIVE_PPR_SQL} DESC"
    elif sort == 'duration':
        order_clause = "fr.duration DESC"
    elif sort == 'spam_mass':
        order_clause = f"COALESCE(ppr.spam_mass, 1.0) ASC, {_EFFECTIVE_PPR_SQL} DESC"

    _exclude_music_labeled_channels_sql = """
        AND NOT EXISTS (
            SELECT 1 FROM channel_tags ct
            INNER JOIN tags t ON t.id = ct.tag_id
            WHERE ct.channel_id = fr.author_id AND LOWER(t.name) = 'music'
        )
        AND NOT EXISTS (
            SELECT 1 FROM channel_category_assignments cca
            WHERE cca.channel_id = fr.author_id AND cca.category_id IS NOT NULL
            AND EXISTS (
                WITH RECURSIVE cat_anc AS (
                    SELECT id, name, parent_id FROM categories WHERE id = cca.category_id
                    UNION ALL
                    SELECT c.id, c.name, c.parent_id FROM categories c INNER JOIN cat_anc ON c.id = cat_anc.parent_id
                )
                SELECT 1 FROM cat_anc WHERE LOWER(name) = 'music' LIMIT 1
            )
        )"""

    # Score source: persona_scores when persona_id set, else ppr_scores for graph_id
    if persona_id is not None:
        score_join = f"LEFT JOIN persona_scores ppr ON ppr.video_id = fr.video_id AND ppr.persona_id = {int(persona_id)}"
    else:
        score_join = f"LEFT JOIN ppr_scores ppr ON ppr.video_id = fr.video_id AND ppr.graph_id = {int(graph_id)}"

    query_params = params[:]
    rows = conn.execute(f"""
        SELECT fr.video_id, fr.title, fr.thumbnail, fr.duration,
               fr.author, fr.author_id, fr.source_video_title,
               COALESCE(ppr.score, 0) as ppr_score,
               {_EFFECTIVE_PPR_SQL} AS effective_ppr_score,
               CAST(COALESCE(vr_vid.rating, ar.rating, cr.rating) AS INTEGER) AS effective_rating,
               ppr.spam_mass,
               vc.category,
               fr.added_at,
               MAX(fr.published_at) AS published_at
        FROM feed_recommendations fr
        JOIN graph_feed_items gfi ON gfi.video_id = fr.video_id AND gfi.graph_id = {int(graph_id)}
        {score_join}
        LEFT JOIN video_categories vc ON vc.video_id = fr.video_id
        LEFT JOIN video_ratings vr_vid ON vr_vid.video_id = fr.video_id
        LEFT JOIN album_tracks at ON at.video_id = fr.video_id
        LEFT JOIN album_ratings ar ON ar.album_key = at.album_key
        LEFT JOIN watch_history wh ON wh.video_id = fr.video_id
        LEFT JOIN channel_ratings cr ON cr.channel_id = fr.author_id
        WHERE (vr_vid.rating IS NULL OR vr_vid.rating > 1)
        AND (ar.rating IS NULL OR ar.rating > 1)
        AND (cr.rating IS NULL OR cr.rating > 1)
        AND wh.video_id IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM feed_filters ff
            WHERE ff.graph_id = {int(graph_id)}
            AND ((ff.filter_type = 'keyword' AND (
                    LOWER(fr.title) LIKE '%' || ff.match_value || '%'
                    OR EXISTS (SELECT 1 FROM video_keywords vk
                               WHERE vk.video_id = fr.video_id
                                 AND LOWER(vk.keyword) LIKE '%' || ff.match_value || '%')))
               OR (ff.filter_type = 'channel_id' AND fr.author_id = ff.match_value)
               OR (ff.filter_type = 'channel_name' AND LOWER(fr.author) LIKE '%' || ff.match_value || '%'))
        )
        {_exclude_music_labeled_channels_sql}
        {category_filter}
        {spam_filter}
        GROUP BY fr.video_id
        ORDER BY {order_clause}
        LIMIT ? OFFSET ?
    """, query_params).fetchall()
    conn.close()
    result = [dict(r) for r in rows]

    # Score blending if cosine or serendipity enabled
    cfg = get_pipeline_config(graph_id=graph_id)
    w_ppr = cfg.get("scorer.ppr.weight", 1.0) if cfg.get("scorer.ppr.enabled", 1.0) else 0.0
    w_cosine = cfg.get("scorer.cosine.weight", 0.0) if cfg.get("scorer.cosine.enabled", 0.0) else 0.0
    w_serendipity = cfg.get("scorer.serendipity.weight", 0.0) if cfg.get("scorer.serendipity.enabled", 0.0) else 0.0
    w_embedding = cfg.get("scorer.embedding.weight", 0.0) if cfg.get("scorer.embedding.enabled", 0.0) else 0.0

    if (w_cosine > 0 or w_serendipity > 0 or w_embedding > 0) and sort == 'score':
        cand_ids = [r["video_id"] for r in result]
        if cand_ids:
            conn2 = get_db()
            ph = ",".join("?" * len(cand_ids))
            cosine_map = {r["video_id"]: r["score"] for r in conn2.execute(
                f"SELECT video_id, score FROM cosine_scores WHERE graph_id={int(graph_id)} AND video_id IN ({ph})", cand_ids
            ).fetchall()}
            seren_map = {r["video_id"]: r["score"] for r in conn2.execute(
                f"SELECT video_id, score FROM serendipity_scores WHERE graph_id={int(graph_id)} AND video_id IN ({ph})", cand_ids
            ).fetchall()}
            emb_map = {r["video_id"]: r["score"] for r in conn2.execute(
                f"SELECT video_id, score FROM embedding_scores WHERE graph_id={int(graph_id)} AND video_id IN ({ph})", cand_ids
            ).fetchall()} if w_embedding > 0 else {}
            conn2.close()

            for r in result:
                vid = r["video_id"]
                blended = (
                    w_ppr * float(r.get("effective_ppr_score") or 0) +
                    w_cosine * cosine_map.get(vid, 0.0) +
                    w_serendipity * seren_map.get(vid, 0.0) +
                    w_embedding * emb_map.get(vid, 0.0)
                )
                r["effective_ppr_score"] = blended
            result.sort(key=lambda r: float(r.get("effective_ppr_score") or 0), reverse=True)

    if sort == 'score' and result:
        if cfg.get("diversity.enabled", 0.0):
            result = mmr_rerank(result,
                                lambda_param=cfg.get("diversity.lambda", 0.7),
                                max_per_channel=int(cfg.get("diversity.max_per_channel", 3)))
        else:
            result = _diversify_ppr_feed_rows(result, limit)
    return result


# ---------------------------------------------------------------------------
# Pipeline config
# ---------------------------------------------------------------------------

PIPELINE_DEFAULTS = {
    "temporal.recency_halflife_days": 0.0,
    "scorer.ppr.enabled": 1.0,
    "scorer.ppr.weight": 1.0,
    "scorer.cosine.enabled": 0.0,
    "scorer.cosine.weight": 0.5,
    "scorer.serendipity.enabled": 0.0,
    "scorer.serendipity.weight": 0.5,
    "scorer.embedding.enabled": 0.0,
    "scorer.embedding.weight": 0.5,
    "diversity.enabled": 0.0,
    "diversity.lambda": 0.7,
    "diversity.max_per_channel": 3.0,
}


def get_pipeline_config(graph_id: int = 1) -> dict:
    conn = get_db()
    rows = conn.execute(
        "SELECT key, value FROM pipeline_config WHERE graph_id=?", (graph_id,)
    ).fetchall()
    conn.close()
    cfg = dict(PIPELINE_DEFAULTS)
    for r in rows:
        if r["key"] in cfg:
            try:
                cfg[r["key"]] = float(r["value"])
            except Exception:
                pass
    return cfg


def set_pipeline_config(updates: dict, graph_id: int = 1) -> None:
    conn = get_db()
    now = time.time()
    for k, v in updates.items():
        conn.execute(
            "INSERT OR REPLACE INTO pipeline_config (graph_id, key, value, updated_at) VALUES (?, ?, ?, ?)",
            (graph_id, k, str(v), now)
        )
    conn.commit()
    conn.close()


def mmr_rerank(items: list[dict], lambda_param: float = 0.7, max_per_channel: int = 3) -> list[dict]:
    """Maximal Marginal Relevance reranker for channel diversity."""
    if not items:
        return items

    selected = []
    remaining = list(items)
    channel_counts: dict[str, int] = {}

    score_key = "effective_ppr_score"

    while remaining:
        best = None
        best_mmr = float('-inf')

        for item in remaining:
            ch = item.get("author_id") or (item.get("author") or "").strip().lower() or "_unknown"
            ch_count = channel_counts.get(ch, 0)
            if max_per_channel > 0 and ch_count >= max_per_channel:
                continue

            relevance = float(item.get(score_key) or 0)

            if selected:
                max_sim = max(
                    1.0 if (s.get("author_id") or (s.get("author") or "").strip().lower() or "_unknown") == ch else 0.0
                    for s in selected[-10:]
                )
            else:
                max_sim = 0.0

            mmr = lambda_param * relevance - (1 - lambda_param) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best = item

        if best is None:
            remaining.sort(key=lambda r: float(r.get(score_key) or 0), reverse=True)
            selected.extend(remaining)
            break

        selected.append(best)
        remaining.remove(best)
        ch = best.get("author_id") or (best.get("author") or "").strip().lower() or "_unknown"
        channel_counts[ch] = channel_counts.get(ch, 0) + 1

    return selected


# ---------------------------------------------------------------------------
# Weight rules
# ---------------------------------------------------------------------------

def get_weight_rules(graph_id: int = 1):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM weight_rules WHERE graph_id=? ORDER BY created_at DESC", (graph_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_weight_rule(rule_type: str, match_value: str, multiplier: float, graph_id: int = 1):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO weight_rules (graph_id, rule_type, match_value, multiplier, created_at) VALUES (?,?,?,?,?)",
        (graph_id, rule_type, match_value, multiplier, time.time())
    )
    conn.execute("DELETE FROM ppr_scores WHERE graph_id=?", (graph_id,))
    conn.commit()
    conn.close()


def delete_weight_rule(rule_id: int, graph_id: int = 1):
    conn = get_db()
    conn.execute("DELETE FROM weight_rules WHERE id = ? AND graph_id = ?", (rule_id, graph_id))
    conn.execute("DELETE FROM ppr_scores WHERE graph_id=?", (graph_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------

def get_attributes():
    conn = get_db()
    rows = conn.execute("SELECT name, description FROM attributes ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_attribute(name: str, description: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO attributes (name, description, created_at) VALUES (?,?,?)",
        (name, description, time.time())
    )
    conn.commit()
    conn.close()


def remove_attribute(name: str):
    conn = get_db()
    conn.execute("DELETE FROM attributes WHERE name = ?", (name,))
    conn.execute("DELETE FROM video_attribute_scores WHERE attribute = ?", (name,))
    conn.execute("DELETE FROM channel_attribute_scores WHERE attribute = ?", (name,))
    conn.execute("DELETE FROM ppr_scores")
    conn.commit()
    conn.close()


def get_video_attribute_scores(video_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT attribute, score FROM video_attribute_scores WHERE video_id = ? ORDER BY attribute",
        (video_id,)
    ).fetchall()
    conn.close()
    return {r["attribute"]: r["score"] for r in rows}


def set_video_attribute_score(video_id: str, attribute: str, score: float):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO video_attribute_scores (video_id, attribute, score, scored_at) VALUES (?,?,?,?)",
        (video_id, attribute, max(0.0, min(10.0, score)), time.time())
    )
    conn.execute("DELETE FROM ppr_scores")
    conn.commit()
    conn.close()


def delete_video_attribute_score(video_id: str, attribute: str):
    conn = get_db()
    conn.execute(
        "DELETE FROM video_attribute_scores WHERE video_id = ? AND attribute = ?",
        (video_id, attribute)
    )
    conn.execute("DELETE FROM ppr_scores")
    conn.commit()
    conn.close()


def get_channel_attribute_scores(channel_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT attribute, score FROM channel_attribute_scores WHERE channel_id = ? ORDER BY attribute",
        (channel_id,)
    ).fetchall()
    conn.close()
    return {r["attribute"]: r["score"] for r in rows}


def set_channel_attribute_score(channel_id: str, channel_name: str, attribute: str, score: float):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO channel_attribute_scores (channel_id, channel_name, attribute, score, scored_at) VALUES (?,?,?,?,?)",
        (channel_id, channel_name, attribute, max(0.0, min(10.0, score)), time.time())
    )
    conn.execute("DELETE FROM ppr_scores")
    conn.commit()
    conn.close()


def delete_channel_attribute_score(channel_id: str, attribute: str):
    conn = get_db()
    conn.execute(
        "DELETE FROM channel_attribute_scores WHERE channel_id = ? AND attribute = ?",
        (channel_id, attribute)
    )
    conn.execute("DELETE FROM ppr_scores")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Music library
# ---------------------------------------------------------------------------

def get_music_library_genres_for_video_ids(video_ids: list[str]) -> dict[str, str]:
    if not video_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(video_ids))
    rows = conn.execute(
        f"""
        SELECT video_id, genre
        FROM music_library
        WHERE video_id IN ({placeholders})
          AND NULLIF(TRIM(genre), '') IS NOT NULL
        """,
        video_ids,
    ).fetchall()
    conn.close()
    return {str(r["video_id"]): str(r["genre"]).strip() for r in rows}


def set_music_library_genre(video_id: str, genre: str | None) -> None:
    """Set `music_library.genre`; creates a minimal row if none exists."""
    conn = get_db()
    now = time.time()
    g = (genre or "").strip() or None
    try:
        row = conn.execute("SELECT video_id FROM music_library WHERE video_id=?", (video_id,)).fetchone()
        if row:
            conn.execute("UPDATE music_library SET genre=? WHERE video_id=?", (g, video_id))
        else:
            conn.execute(
                """
                INSERT INTO music_library (video_id, title, genre, added_at)
                VALUES (?, ?, ?, ?)
                """,
                (video_id, video_id, g, now),
            )
        if g:
            from backend.services.music_tags import ensure_slash_genre_subtag
            ensure_slash_genre_subtag(conn, g)
        conn.commit()
    finally:
        conn.close()


def delete_music_library_item(video_id: str, playlist_id: int | None = None) -> dict:
    removed_from_playlist = False
    if playlist_id is not None:
        _remove_video_from_playlist(playlist_id, video_id)
        removed_from_playlist = True

    conn = get_db()
    try:
        deleted = conn.execute(
            "DELETE FROM music_library WHERE video_id = ?",
            (video_id,),
        ).rowcount > 0
        conn.commit()
        return {
            "deleted": deleted,
            "removed_from_playlist": removed_from_playlist,
        }
    finally:
        conn.close()


def _remove_video_from_playlist(playlist_id: int, video_id: str):
    """Internal helper used by delete_music_library_item."""
    conn = get_db()
    now = time.time()
    row = conn.execute(
        """
        SELECT pv.source_managed, p.source_playlist_id, p.source_updated_at
        FROM playlist_videos pv
        JOIN playlists p ON p.id = pv.playlist_id
        WHERE pv.playlist_id = ? AND pv.video_id = ?
        """,
        (playlist_id, video_id),
    ).fetchone()
    if row and row["source_managed"] and (row["source_playlist_id"] is not None or row["source_updated_at"] is not None):
        conn.execute(
            """
            INSERT INTO playlist_video_overrides (playlist_id, video_id, is_deleted, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                is_deleted = excluded.is_deleted,
                updated_at = excluded.updated_at
            """,
            (playlist_id, video_id, now),
        )
    else:
        conn.execute(
            "DELETE FROM playlist_video_overrides WHERE playlist_id = ? AND video_id = ?",
            (playlist_id, video_id),
        )
    conn.execute("DELETE FROM playlist_videos WHERE playlist_id = ? AND video_id = ?", (playlist_id, video_id))
    conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (now, playlist_id))
    conn.execute("DELETE FROM ppr_scores")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Music tags read helpers
# ---------------------------------------------------------------------------

def get_music_tags_for_video_ids(video_ids: list[str], per_video_limit: int = 12) -> dict[str, list[dict]]:
    """Music tag names assigned to library rows for these video ids."""
    if not video_ids:
        return {}
    conn = get_db()
    placeholders = ",".join("?" * len(video_ids))
    rows = conn.execute(
        f"""
        SELECT mta.video_id AS video_id, mt.id AS tag_id, mt.name AS tag_name,
               g.name AS group_name
        FROM music_tag_assignments mta
        JOIN music_tags mt ON mt.id = mta.tag_id
        LEFT JOIN music_tag_groups g ON g.id = mt.group_id
        WHERE mta.video_id IN ({placeholders})
        """,
        video_ids,
    ).fetchall()
    conn.close()
    buckets: dict[str, list[tuple[int, str, str]]] = {}
    for r in rows:
        vid = str(r["video_id"])
        tid = int(r["tag_id"])
        name = str(r["tag_name"] or "").strip()
        group_name = str(r["group_name"] or "").strip()
        buckets.setdefault(vid, []).append((tid, name or f"Tag {tid}", group_name))
    per: dict[str, list[dict]] = {}
    for vid, triples in buckets.items():
        triples.sort(key=lambda t: (t[1].lower(), t[0]))
        per[vid] = [
            {"id": tid, "name": name, "group_name": group_name or None}
            for tid, name, group_name in triples[:per_video_limit]
        ]
    return per


def ensure_slash_genre_subtag(conn, genre_label: str) -> None:
    """Delegate to music_tags module (lives there to avoid circular import at module level)."""
    from backend.services.music_tags import ensure_slash_genre_subtag as _impl
    _impl(conn, genre_label)


def get_tag_by_id(tag_id: int):
    conn = get_db()
    row = conn.execute('SELECT id, name, description, created_at FROM tags WHERE id=?', (tag_id,)).fetchone()
    if not row:
        conn.close()
        return None
    vc = conn.execute('SELECT COUNT(*) FROM video_tags WHERE tag_id=?', (tag_id,)).fetchone()[0]
    cc = conn.execute('SELECT COUNT(*) FROM channel_tags WHERE tag_id=?', (tag_id,)).fetchone()[0]
    conn.close()
    return {**dict(row), 'video_count': vc, 'channel_count': cc}


# ---------------------------------------------------------------------------
# Album ratings
# ---------------------------------------------------------------------------

def get_album_rating(album_key: str):
    if not album_key:
        return None
    conn = get_db()
    row = conn.execute(
        """
        SELECT album_key, album_title, album_artist, cover_art, source,
               playlist_id, playlist_title, CAST(rating AS INTEGER) AS rating, rated_at
        FROM album_ratings
        WHERE album_key = ?
        """,
        (album_key,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Artist follow / release events (temporary — will move to ytmusic REST endpoint)
# ---------------------------------------------------------------------------

def get_artist_follow(artist_name: str):
    artist_key = normalize_artist_key(artist_name)
    if not artist_key:
        return None
    conn = get_db()
    row = conn.execute(
        """
        SELECT *
        FROM artist_follows
        WHERE artist_key = ?
        """,
        (artist_key,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_artist_follows(limit: int = 100):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT *
        FROM artist_follows
        ORDER BY updated_at DESC, artist_name COLLATE NOCASE ASC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def save_artist_follow(
    artist_name: str,
    *,
    image: str | None = None,
    source: str | None = None,
    spotify_artist_id: str | None = None,
    deezer_artist_id: str | None = None,
    itunes_artist_id: str | None = None,
):
    artist_key = normalize_artist_key(artist_name)
    if not artist_key:
        return None

    existing = get_artist_follow(artist_name)
    now = time.time()
    conn = get_db()
    conn.execute(
        """
        INSERT INTO artist_follows (
            artist_key,
            artist_name,
            image,
            source,
            spotify_artist_id,
            deezer_artist_id,
            itunes_artist_id,
            last_release_key,
            last_release_title,
            last_release_date,
            last_release_cover_art,
            last_release_source,
            last_checked_at,
            created_at,
            updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(artist_key) DO UPDATE SET
            artist_name=excluded.artist_name,
            image=COALESCE(NULLIF(excluded.image, ''), artist_follows.image),
            source=COALESCE(NULLIF(excluded.source, ''), artist_follows.source),
            spotify_artist_id=COALESCE(NULLIF(excluded.spotify_artist_id, ''), artist_follows.spotify_artist_id),
            deezer_artist_id=COALESCE(NULLIF(excluded.deezer_artist_id, ''), artist_follows.deezer_artist_id),
            itunes_artist_id=COALESCE(NULLIF(excluded.itunes_artist_id, ''), artist_follows.itunes_artist_id),
            last_release_key=COALESCE(excluded.last_release_key, artist_follows.last_release_key),
            last_release_title=COALESCE(excluded.last_release_title, artist_follows.last_release_title),
            last_release_date=COALESCE(excluded.last_release_date, artist_follows.last_release_date),
            last_release_cover_art=COALESCE(excluded.last_release_cover_art, artist_follows.last_release_cover_art),
            last_release_source=COALESCE(excluded.last_release_source, artist_follows.last_release_source),
            last_checked_at=COALESCE(excluded.last_checked_at, artist_follows.last_checked_at),
            updated_at=excluded.updated_at
        """,
        (
            artist_key,
            artist_name.strip(),
            image or (existing or {}).get("image"),
            source or (existing or {}).get("source"),
            spotify_artist_id or (existing or {}).get("spotify_artist_id"),
            deezer_artist_id or (existing or {}).get("deezer_artist_id"),
            itunes_artist_id or (existing or {}).get("itunes_artist_id"),
            (existing or {}).get("last_release_key"),
            (existing or {}).get("last_release_title"),
            (existing or {}).get("last_release_date"),
            (existing or {}).get("last_release_cover_art"),
            (existing or {}).get("last_release_source"),
            (existing or {}).get("last_checked_at"),
            (existing or {}).get("created_at") or now,
            now,
        ),
    )
    conn.commit()
    conn.close()
    return get_artist_follow(artist_name)


def delete_artist_follow(artist_name: str):
    artist_key = normalize_artist_key(artist_name)
    if not artist_key:
        return
    conn = get_db()
    conn.execute("DELETE FROM artist_follows WHERE artist_key = ?", (artist_key,))
    conn.commit()
    conn.close()


def sync_artist_follows_from_album_ratings(min_rating: int = 8) -> dict[str, int]:
    """Create artist_follows rows for every distinct album artist with rating >= min_rating."""
    min_rating = max(1, min(10, int(min_rating)))
    conn = get_db()
    rows = conn.execute(
        """
        SELECT DISTINCT TRIM(album_artist) AS artist_name
        FROM album_ratings
        WHERE rating >= ? AND album_artist IS NOT NULL AND TRIM(album_artist) != ''
        """,
        (min_rating,),
    ).fetchall()
    conn.close()
    added = 0
    already_following = 0
    for row in rows:
        name = (row["artist_name"] or "").strip()
        if not name:
            continue
        if get_artist_follow(name):
            already_following += 1
            continue
        save_artist_follow(name)
        added += 1
    return {
        "added": added,
        "already_following": already_following,
        "distinct_artists": len(rows),
    }


def save_artist_release_snapshot(
    artist_name: str,
    *,
    release_key: str | None,
    title: str | None,
    release_date: str | None = None,
    cover_art: str | None = None,
    source: str | None = None,
    checked_at: float | None = None,
):
    artist_key = normalize_artist_key(artist_name)
    if not artist_key:
        return None
    now = checked_at if checked_at is not None else time.time()
    conn = get_db()
    conn.execute(
        """
        UPDATE artist_follows
        SET
            last_release_key = ?,
            last_release_title = ?,
            last_release_date = ?,
            last_release_cover_art = ?,
            last_release_source = ?,
            last_checked_at = ?,
            updated_at = ?
        WHERE artist_key = ?
        """,
        (
            release_key or None,
            title or None,
            release_date or None,
            cover_art or None,
            source or None,
            now,
            now,
            artist_key,
        ),
    )
    conn.commit()
    conn.close()
    return get_artist_follow(artist_name)


def record_artist_release_event(
    artist_name: str,
    *,
    release_key: str,
    title: str,
    release_date: str | None = None,
    cover_art: str | None = None,
    source: str | None = None,
):
    artist_key = normalize_artist_key(artist_name)
    if not artist_key or not release_key or not title:
        return False
    conn = get_db()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO artist_release_events (
            artist_key,
            artist_name,
            release_key,
            title,
            release_date,
            cover_art,
            source,
            created_at
        )
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            artist_key,
            artist_name.strip(),
            release_key,
            title,
            release_date or None,
            cover_art or None,
            source or None,
            time.time(),
        ),
    )
    conn.commit()
    created = cur.rowcount > 0
    conn.close()
    return created


def list_artist_release_events(limit: int = 24):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT are.*
        FROM artist_release_events are
        JOIN artist_follows af ON af.artist_key = are.artist_key
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Video media overrides
# ---------------------------------------------------------------------------

def get_video_media_override(video_id: str):
    conn = get_db()
    row = conn.execute(
        "SELECT media_override FROM video_media_overrides WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    conn.close()
    return row["media_override"] if row else None


def set_video_media_override(video_id: str, media_override: str):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO video_media_overrides (video_id, media_override, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            media_override=excluded.media_override,
            updated_at=excluded.updated_at
        """,
        (video_id, media_override, time.time()),
    )
    conn.commit()
    conn.close()


def delete_video_media_override(video_id: str):
    conn = get_db()
    conn.execute("DELETE FROM video_media_overrides WHERE video_id = ?", (video_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Feed filters
# ---------------------------------------------------------------------------

def get_feed_filters(graph_id: int = 1):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM feed_filters WHERE graph_id=? ORDER BY created_at DESC", (graph_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_feed_filter(filter_type: str, match_value: str, graph_id: int = 1):
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO feed_filters (graph_id, filter_type, match_value, created_at) VALUES (?,?,?,?)",
        (graph_id, filter_type, match_value, time.time()),
    )
    conn.commit()
    conn.close()


def delete_feed_filter(filter_id: int, graph_id: int = 1):
    conn = get_db()
    conn.execute("DELETE FROM feed_filters WHERE id = ? AND graph_id = ?", (filter_id, graph_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Graph sources (per-graph content source enablement)
# ---------------------------------------------------------------------------

def list_graph_sources(graph_id: int) -> list[dict]:
    """Return all sources for a graph, merging global state with per-graph overrides."""
    from backend.services import source_registry
    conn = get_db()
    rows = conn.execute(
        "SELECT source_name, weight_override FROM graph_sources WHERE graph_id=?", (graph_id,)
    ).fetchall()
    conn.close()
    graph_map = {r["source_name"]: r["weight_override"] for r in rows}
    all_sources = source_registry.list_sources()
    result = []
    for s in all_sources:
        if s["name"] in graph_map:
            s = dict(s)
            s["in_graph"] = True
            s["weight_override"] = graph_map[s["name"]]
        else:
            s = dict(s)
            s["in_graph"] = False
            s["weight_override"] = None
        result.append(s)
    return result


def upsert_graph_source(graph_id: int, source_name: str, weight_override=None) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO graph_sources (graph_id, source_name, weight_override) VALUES (?,?,?)",
        (graph_id, source_name, weight_override),
    )
    conn.commit()
    conn.close()


def remove_graph_source(graph_id: int, source_name: str) -> None:
    conn = get_db()
    conn.execute(
        "DELETE FROM graph_sources WHERE graph_id=? AND source_name=?", (graph_id, source_name)
    )
    conn.commit()
    conn.close()


def get_graph_source_names(graph_id: int) -> list[str]:
    """Return source names that are enabled for a graph (globally available + in graph_sources)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT source_name FROM graph_sources WHERE graph_id=?", (graph_id,)
    ).fetchall()
    conn.close()
    return [r["source_name"] for r in rows]


# ---------------------------------------------------------------------------
# Graph feed items (per-graph feed membership)
# ---------------------------------------------------------------------------

def add_graph_feed_items(graph_id: int, video_id: str, source_video_id: str, added_at: float) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO graph_feed_items (graph_id, video_id, source_video_id, added_at) VALUES (?,?,?,?)",
        (graph_id, video_id, source_video_id, added_at),
    )
    conn.commit()
    conn.close()


def get_graph_ids_for_source(source_name: str) -> list[int]:
    """Return graph IDs that the given source feeds into."""
    conn = get_db()
    rows = conn.execute(
        "SELECT graph_id FROM graph_sources WHERE source_name=?", (source_name,)
    ).fetchall()
    conn.close()
    return [r["graph_id"] for r in rows]


# ---------------------------------------------------------------------------
# Category helpers (for category_recs)
# ---------------------------------------------------------------------------

def get_category_descendant_ids(conn, cat_id: int):
    ids = [cat_id]
    queue = [cat_id]
    while queue:
        cur = queue.pop()
        children = conn.execute('SELECT id FROM categories WHERE parent_id=?', (cur,)).fetchall()
        for c in children:
            ids.append(c['id'])
            queue.append(c['id'])
    return ids


# ---------------------------------------------------------------------------
# Invidious API response cache
# ---------------------------------------------------------------------------

import json as _json


def get_invidious_cache(cache_key: str) -> dict | None:
    conn = get_db()
    row = conn.execute(
        "SELECT response_json FROM invidious_cache WHERE cache_key = ? AND expires_at > ?",
        (cache_key, time.time()),
    ).fetchone()
    conn.close()
    if row:
        return _json.loads(row["response_json"])
    return None


def set_invidious_cache(cache_key: str, data: dict, ttl: float) -> None:
    now = time.time()
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO invidious_cache (cache_key, response_json, fetched_at, expires_at) VALUES (?, ?, ?, ?)",
        (cache_key, _json.dumps(data), now, now + ttl),
    )
    conn.commit()
    conn.close()


def purge_expired_invidious_cache() -> int:
    conn = get_db()
    result = conn.execute("DELETE FROM invidious_cache WHERE expires_at <= ?", (time.time(),))
    deleted = result.rowcount
    conn.commit()
    conn.close()
    return deleted


