import time
from collections import defaultdict
from backend.db import get_db

# Weight for a video that was merely watched (no rating, not in a playlist).
# Intentionally tiny — passive watch history should not compete with explicit signals.
# Lowered 0.01 -> 0.004 (2026-06-06) to further reduce passively-watched, unrated videos' pull.
WATCH_BASE = 0.004

# Weight added for explicit playlist membership (deliberate curation).
PLAYLIST_BASE = 3.0

# Base multiplier for feed_recommendation videos that carry a rating signal
# (explicit video rating, channel rating, or playlist membership) but have
# never been watched or added to a local playlist.  Multiplied by rating_mult()
# so the final seed weight scales with the rating just like history/playlist seeds.
# Keeps feed-only rated content from being invisible to PPR while staying weaker
# than an explicit playlist entry (PLAYLIST_BASE = 3.0).
FEED_REC_BASE = 1.5


_MUSIC_EDGE_FILTER = """
    re.source_video_id IN (
        SELECT video_id FROM recognition_cache WHERE is_music = 1
        UNION SELECT video_id FROM music_library
    )
"""

_GRAPH_EDGE_QUERIES: dict[str, str] = {
    "mixed": "SELECT source_video_id, target_video_id, weight FROM recommendation_edges",
    "music": f"""
        SELECT re.source_video_id, re.target_video_id, re.weight
        FROM recommendation_edges re
        WHERE {_MUSIC_EDGE_FILTER}
    """,
    "video": """
        SELECT re.source_video_id, re.target_video_id, re.weight
        FROM recommendation_edges re
        WHERE re.source_video_id NOT IN (
            SELECT video_id FROM recognition_cache WHERE is_music = 1
        )
    """,
    # album/artist: same music edges; output is aggregated by album/artist at feed-serve time
    "album": f"""
        SELECT re.source_video_id, re.target_video_id, re.weight
        FROM recommendation_edges re
        WHERE {_MUSIC_EDGE_FILTER}
    """,
    "artist": f"""
        SELECT re.source_video_id, re.target_video_id, re.weight
        FROM recommendation_edges re
        WHERE {_MUSIC_EDGE_FILTER}
    """,
}


def build_graph(content_type: str = "mixed"):
    """Build adjacency list from recommendation_edges, filtered by content_type.

    content_type: 'mixed' (all), 'music' (music-confirmed only), 'video' (non-music only).
    Returns {src: [(tgt, weight), ...]}."""
    query = _GRAPH_EDGE_QUERIES.get(content_type, _GRAPH_EDGE_QUERIES["mixed"])
    conn = get_db()
    rows = conn.execute(query).fetchall()
    conn.close()

    graph = defaultdict(list)
    for r in rows:
        graph[r["source_video_id"]].append((r["target_video_id"], r["weight"]))
    return dict(graph)


def rating_mult(rating: int) -> float:
    """Seed weight for a numeric rating 1-10.

      1        → 0.0  (block)
      2–5      → linear: rating/5.0  (2→0.4, 5→1.0)
      6–10     → quadratic: (rating-4)²  (6→4, 7→9, 8→16, 9→25, 10→36)
    """
    if rating <= 1:
        return 0.0
    if rating <= 5:
        return rating / 5.0
    return float((rating - 4) ** 2)


def _load_effective_ratings(candidate_ids: list, author_map: dict) -> dict[str, int]:
    """Return effective rating per video: explicit video rating, or channel rating as fallback.

    Videos with no video rating AND no channel rating return no entry (unrated).
    This lets channel ratings act as a default quality signal for all videos from
    that channel, without overriding explicit per-video judgments.
    """
    if not candidate_ids:
        return {}

    conn = get_db()
    ph = ",".join("?" * len(candidate_ids))

    video_ratings = {
        r["video_id"]: int(r["rating"])
        for r in conn.execute(
            f"SELECT video_id, rating FROM video_ratings WHERE video_id IN ({ph})", candidate_ids
        ).fetchall()
    }

    album_ratings = {
        r["video_id"]: int(r["rating"])
        for r in conn.execute(
            f"""
            SELECT at.video_id, ar.rating
            FROM album_tracks at
            JOIN album_ratings ar ON ar.album_key = at.album_key
            WHERE at.video_id IN ({ph})
            """,
            candidate_ids,
        ).fetchall()
    }

    author_ids = list({aid for aid in author_map.values() if aid})
    channel_ratings: dict[str, int] = {}
    if author_ids:
        aph = ",".join("?" * len(author_ids))
        channel_ratings = {
            r["channel_id"]: int(r["rating"])
            for r in conn.execute(
                f"SELECT channel_id, rating FROM channel_ratings WHERE channel_id IN ({aph})", author_ids
            ).fetchall()
        }

    conn.close()

    effective: dict[str, int] = {}
    for vid in candidate_ids:
        if vid in video_ratings:
            effective[vid] = video_ratings[vid]
        elif vid in album_ratings:
            effective[vid] = album_ratings[vid]
        else:
            aid = author_map.get(vid)
            if aid and aid in channel_ratings:
                effective[vid] = channel_ratings[aid]
    return effective


def get_seed_weights(
    min_seed_rating: int = 0,
    recency_halflife_days: float = 0.0,
    graph_id: int = 1,
    watch_base: float = WATCH_BASE,
    playlist_base: float = PLAYLIST_BASE,
    feed_rec_base: float = FEED_REC_BASE,
):
    """Build personalization vector from history, playlists, video ratings, and channel ratings.

    Weight model (normal mode, min_seed_rating=0):
      seeds[vid] = watch_component + playlist_component + rating_component
      - watch_component    = WATCH_BASE (0.01) if in watch history
      - playlist_component = PLAYLIST_BASE (3.0) if in any playlist
      - rating_component   = rating_mult(effective_rating) if rated
        effective_rating = explicit video rating, OR channel rating as fallback.
        This is additive, not a multiplier on the base — makes ratings dominant.

    Feed-rec direct seeding (applied after history/playlist loop):
      Videos in feed_recommendations that are NOT already in watch_history or
      playlist_videos can still carry a rating signal via:
        - an explicit video rating
        - a channel rating on their author
        - playlist membership (video_id in playlist_videos — already covered above)
      For these, seeds[vid] = FEED_REC_BASE * rating_mult(effective_rating).
      This makes rated-channel/video content in the recommendation pool a first-class
      PPR seed, so PPR propagates *from* them rather than only *to* them.

    Strict mode (min_seed_rating > 0):
      Only videos whose effective_rating >= min_seed_rating contribute.
      Unrated watched/playlist videos are excluded entirely.
      Weight = rating_mult(effective_rating) only (no watch/playlist base).
      Feed-rec seeds also respect this threshold.
      Use this to anchor recommendations exclusively on highly-rated content.
    """
    conn = get_db()

    history = conn.execute("SELECT video_id, author_id FROM watch_history").fetchall()
    author_map: dict[str, str] = {r["video_id"]: r["author_id"] for r in history}
    watched_set: set[str] = {r["video_id"] for r in history}

    playlist_vids = conn.execute("SELECT DISTINCT video_id, author_id FROM playlist_videos").fetchall()
    playlist_set: set[str] = set()
    for r in playlist_vids:
        playlist_set.add(r["video_id"])
        if r["author_id"]:
            author_map[r["video_id"]] = r["author_id"]

    conn.close()

    all_candidates = list(watched_set | playlist_set)
    effective_rating = _load_effective_ratings(all_candidates, author_map)

    seeds: dict[str, float] = {}

    if min_seed_rating > 0:
        # Strict mode: only videos with effective_rating >= threshold.
        for vid in all_candidates:
            r = effective_rating.get(vid)
            if r is not None and r >= min_seed_rating:
                seeds[vid] = rating_mult(r)
    else:
        # Normal mode: additive signals.
        for vid in all_candidates:
            w = 0.0
            if vid in watched_set:
                w += watch_base
            if vid in playlist_set:
                w += playlist_base
            r = effective_rating.get(vid)
            if r is not None:
                rm = rating_mult(r)
                if rm == 0.0:
                    w = 0.0  # rating 1 = hard block
                else:
                    w += rm  # additive: base + explicit quality signal
            seeds[vid] = w

    # Direct seeding from feed_recommendations based on ratings / playlist membership.
    #
    # Videos sitting in the recommendation pool that carry an explicit rating signal
    # (video rating, channel rating) but have never been watched or added to a local
    # playlist are invisible to the history/playlist seed loop above.  By seeding them
    # here with FEED_REC_BASE * rating_mult(effective_rating) we give PPR a direct
    # starting point at those videos, so recommendations propagate *from* them.
    #
    # Playlist-membership for feed-rec videos is already handled: if a video was added
    # to a local playlist it appears in playlist_videos and is covered by playlist_set.
    # We skip any video already in watched_set | playlist_set to avoid double-counting.
    conn = get_db()
    feed_rated_rows = conn.execute("""
        SELECT fr.video_id,
               CAST(COALESCE(vr.rating, ar.rating, cr.rating) AS INTEGER) AS effective_rating
        FROM feed_recommendations fr
        LEFT JOIN video_ratings vr ON vr.video_id = fr.video_id
        LEFT JOIN album_tracks at ON at.video_id = fr.video_id
        LEFT JOIN album_ratings ar ON ar.album_key = at.album_key
        LEFT JOIN channel_ratings cr ON cr.channel_id = fr.author_id
        WHERE COALESCE(vr.rating, ar.rating, cr.rating) IS NOT NULL
          AND CAST(COALESCE(vr.rating, ar.rating, cr.rating) AS INTEGER) > 1
        GROUP BY fr.video_id
    """).fetchall()
    conn.close()

    for r in feed_rated_rows:
        vid = r["video_id"]
        if vid in watched_set or vid in playlist_set:
            continue  # already seeded more strongly above
        eff_r = r["effective_rating"]
        if eff_r is None:
            continue
        if min_seed_rating > 0 and eff_r < min_seed_rating:
            continue
        rm = rating_mult(int(eff_r))
        if rm <= 0:
            continue
        seeds[vid] = seeds.get(vid, 0.0) + feed_rec_base * rm

    # Feed recommendation feedback
    conn = get_db()
    feedback_rows = conn.execute("SELECT video_id, feedback FROM feed_feedback").fetchall()
    conn.close()
    for r in feedback_rows:
        vid = r["video_id"]
        if r["feedback"] == 1:
            seeds[vid] = seeds.get(vid, 0.5) * 1.4
        elif r["feedback"] == -1:
            seeds[vid] = 0.0

    # "Too much of this channel": down-weight seeds from that channel. Strength scales with
    # the disliked video's numeric rating (default 4) so changing the rating later retunes impact.
    conn = get_db()
    channel_rows = conn.execute(
        """
        SELECT ff.author_id, CAST(COALESCE(vr.rating, 4) AS INTEGER) AS r
        FROM feed_feedback ff
        LEFT JOIN video_ratings vr ON vr.video_id = ff.video_id
        WHERE ff.feedback = -1
          AND ff.dislike_reason = 'too_much_channel'
          AND ff.author_id IS NOT NULL
          AND TRIM(ff.author_id) != ''
        """
    ).fetchall()
    conn.close()

    channel_penalty: dict[str, float] = {}
    for r in channel_rows:
        aid = r["author_id"]
        rating = int(r["r"] or 4)
        strength = max(0.0, min(1.0, rating / 10.0))
        factor = max(0.08, 1.0 - 0.58 * strength)
        channel_penalty[aid] = min(channel_penalty.get(aid, 1.0), factor)

    if channel_penalty:
        conn = get_db()
        seed_ids = [v for v in seeds if seeds.get(v, 0) > 0]
        if seed_ids:
            ph = ",".join("?" * len(seed_ids))
            vid_to_author: dict[str, str] = {}
            for tbl in (
                f"SELECT video_id, author_id FROM feed_recommendations WHERE video_id IN ({ph}) AND author_id IS NOT NULL",
                f"SELECT video_id, author_id FROM watch_history WHERE video_id IN ({ph}) AND author_id IS NOT NULL",
                f"SELECT video_id, author_id FROM playlist_videos WHERE video_id IN ({ph}) AND author_id IS NOT NULL",
            ):
                for row in conn.execute(tbl, seed_ids):
                    vid = row["video_id"]
                    aid = row["author_id"]
                    if aid and vid not in vid_to_author:
                        vid_to_author[vid] = aid
            conn.close()
            for vid, w in list(seeds.items()):
                if w <= 0:
                    continue
                aid = vid_to_author.get(vid)
                if aid and aid in channel_penalty:
                    seeds[vid] = w * channel_penalty[aid]
        else:
            conn.close()

    # Weight rules: keyword / genre / category / attribute (graph-scoped)
    conn = get_db()
    rules = conn.execute(
        "SELECT rule_type, match_value, multiplier FROM weight_rules WHERE graph_id=?",
        (graph_id,)
    ).fetchall()
    keyword_rules = {r["match_value"]: r["multiplier"] for r in rules if r["rule_type"] == "keyword"}
    genre_rules   = {r["match_value"]: r["multiplier"] for r in rules if r["rule_type"] == "genre"}
    category_rules = {r["match_value"]: r["multiplier"] for r in rules if r["rule_type"] == "category"}
    attribute_rules = {r["match_value"]: r["multiplier"] for r in rules if r["rule_type"] == "attribute"}

    if keyword_rules or genre_rules or category_rules or attribute_rules:
        seed_ids = list(seeds)
        placeholders = ",".join("?" * len(seed_ids))

        genre_map = {}
        if genre_rules:
            for r in conn.execute(
                f"SELECT video_id, genre FROM video_metadata WHERE video_id IN ({placeholders})", seed_ids
            ).fetchall():
                genre_map[r["video_id"]] = r["genre"]

        kw_map: dict[str, set] = defaultdict(set)
        if keyword_rules:
            for r in conn.execute(
                f"SELECT video_id, keyword FROM video_keywords WHERE video_id IN ({placeholders})", seed_ids
            ).fetchall():
                kw_map[r["video_id"]].add(r["keyword"])

        cat_map = {}
        if category_rules:
            for r in conn.execute(
                f"SELECT video_id, category FROM video_categories WHERE video_id IN ({placeholders})", seed_ids
            ).fetchall():
                cat_map[r["video_id"]] = r["category"]

        vid_attr_map: dict[str, dict] = defaultdict(dict)
        chan_attr_map: dict[str, dict] = defaultdict(dict)
        if attribute_rules:
            for r in conn.execute(
                f"SELECT video_id, attribute, score FROM video_attribute_scores WHERE video_id IN ({placeholders})", seed_ids
            ).fetchall():
                vid_attr_map[r["video_id"]][r["attribute"]] = r["score"]
            author_ids = [aid for aid in author_map.values() if aid]
            if author_ids:
                ch_ph = ",".join("?" * len(author_ids))
                for r in conn.execute(
                    f"SELECT channel_id, attribute, score FROM channel_attribute_scores WHERE channel_id IN ({ch_ph})",
                    author_ids
                ).fetchall():
                    chan_attr_map[r["channel_id"]][r["attribute"]] = r["score"]

        for vid in list(seeds):
            mult = 1.0

            for kw, m in keyword_rules.items():
                if kw in kw_map.get(vid, set()):
                    mult = max(mult, m)

            g = genre_map.get(vid)
            if g and g in genre_rules:
                mult = max(mult, genre_rules[g])

            c = cat_map.get(vid)
            if c:
                for rule_cat, rule_mult in category_rules.items():
                    if c == rule_cat or c.startswith(rule_cat + "/"):
                        mult = max(mult, rule_mult)

            for attr, base_mult in attribute_rules.items():
                score = vid_attr_map[vid].get(attr)
                if score is None:
                    aid = author_map.get(vid)
                    if aid:
                        score = chan_attr_map[aid].get(attr)
                if score is not None:
                    mult = max(mult, base_mult * (score / 10.0))

            if mult != 1.0:
                seeds[vid] *= mult

    # Apply feed filters (graph-scoped)
    filters = conn.execute(
        "SELECT filter_type, match_value FROM feed_filters WHERE graph_id=?",
        (graph_id,)
    ).fetchall()
    if filters:
        keyword_filters = [f["match_value"] for f in filters if f["filter_type"] == "keyword"]
        channel_id_filters = {f["match_value"] for f in filters if f["filter_type"] == "channel_id"}
        channel_name_filters = [f["match_value"] for f in filters if f["filter_type"] == "channel_name"]

        if keyword_filters or channel_name_filters:
            seed_ids = list(seeds)
            ph = ",".join("?" * len(seed_ids))
            title_rows = conn.execute(
                f"SELECT video_id, LOWER(title) as title, LOWER(author) as author, author_id FROM watch_history WHERE video_id IN ({ph})",
                seed_ids
            ).fetchall()
            pl_rows = conn.execute(
                f"SELECT video_id, LOWER(title) as title, LOWER(author) as author, author_id FROM playlist_videos WHERE video_id IN ({ph})",
                seed_ids
            ).fetchall()
            vid_info = {r["video_id"]: dict(r) for r in title_rows}
            vid_info.update({r["video_id"]: dict(r) for r in pl_rows})
        else:
            vid_info = {}

        for vid in list(seeds):
            info = vid_info.get(vid, {})
            title = info.get("title", "")
            author = info.get("author", "")
            author_id = info.get("author_id", "")
            blocked = (
                any(kw in title for kw in keyword_filters)
                or author_id in channel_id_filters
                or any(cn in author for cn in channel_name_filters)
            )
            if blocked:
                seeds[vid] = 0.0

    conn.close()

    if recency_halflife_days > 0:
        import math as _math
        halflife_secs = recency_halflife_days * 86400
        decay_k = _math.log(2) / halflife_secs
        now = time.time()
        conn_ts = get_db()
        ts_rows = conn_ts.execute(
            "SELECT video_id, watched_at FROM watch_history WHERE watched_at IS NOT NULL"
        ).fetchall()
        conn_ts.close()
        ts_map = {r["video_id"]: r["watched_at"] for r in ts_rows}
        for vid in list(seeds):
            ts = ts_map.get(vid)
            if ts:
                age_secs = max(0.0, now - ts)
                seeds[vid] *= _math.exp(-decay_k * age_secs)

    seeds = {k: v for k, v in seeds.items() if v > 0}
    total = sum(seeds.values())
    if total > 0:
        seeds = {k: v / total for k, v in seeds.items()}

    return seeds


def compute_ppr(graph, seeds, alpha=0.15, max_iter=100, tol=1e-6):
    """Personalized PageRank via power iteration.

    Args:
        graph: adjacency list {src: [(tgt, weight), ...]}
        seeds: personalization vector {node: weight}, normalized to sum=1
        alpha: teleport probability
        max_iter: maximum iterations
        tol: convergence tolerance (L1 norm)

    Returns:
        dict of {node: score}
    """
    all_nodes = set(seeds.keys())
    for src, edges in graph.items():
        all_nodes.add(src)
        for tgt, _ in edges:
            all_nodes.add(tgt)

    if not all_nodes:
        return {}

    out_weight = {}
    for src, edges in graph.items():
        out_weight[src] = sum(w for _, w in edges)

    # Pre-normalize edge weights once so the hot loop only multiplies, never divides.
    in_edges: dict = defaultdict(list)
    for src, edges in graph.items():
        ow = out_weight.get(src, 0)
        if ow > 0:
            for tgt, w in edges:
                in_edges[tgt].append((src, w / ow))

    scores = {node: seeds.get(node, 0.0) for node in all_nodes}

    for _ in range(max_iter):
        new_scores = {}
        for node in all_nodes:
            propagated = 0.0
            for src, nw in in_edges.get(node, []):
                propagated += scores.get(src, 0.0) * nw
            new_scores[node] = alpha * seeds.get(node, 0.0) + (1 - alpha) * propagated

        diff = sum(abs(new_scores.get(n, 0) - scores.get(n, 0)) for n in all_nodes)
        scores = new_scores
        if diff < tol:
            break

    return scores


def compute_global_ppr(graph):
    """Run PPR with a uniform seed vector over all graph nodes.

    Returns an unbiased structural-popularity score for each node —
    how reachable it is from everywhere in the graph, with no taste bias.
    Used together with trusted PPR to compute spam_mass.
    """
    all_nodes: set[str] = set()
    for src, edges in graph.items():
        all_nodes.add(src)
        for tgt, _ in edges:
            all_nodes.add(tgt)

    if not all_nodes:
        return {}

    n = len(all_nodes)
    uniform_seeds = {node: 1.0 / n for node in all_nodes}
    return compute_ppr(graph, uniform_seeds)


def update_ppr_scores(
    graph_id: int = 1,
    content_type: str = "mixed",
    min_seed_rating: int = 0,
    compute_spam_mass: bool = True,
):
    """Recompute PPR scores for a named graph and cache in the database.

    Pass 1 (always): Trusted PPR — seeds from rated/watched/playlist content.
    Pass 2 (optional): Global PPR — uniform seeds over all nodes, used to
      compute spam_mass = (global - trusted) / global.
        ≈ 0  → reachable from your taste seeds  → good recommendation
        ≈ 1  → globally popular but not from your taste → structural slop
      Skipped when compute_spam_mass=False (saves ~2s on a large graph).
      spam_mass is then stored as NULL for all rows.

    Args:
        graph_id: which graph to store scores for (default 1 = 'default').
        content_type: edge filter — 'mixed', 'music', or 'video'.
        min_seed_rating: if > 0, only videos with effective_rating >= this
                         value contribute to trusted seeds.
        compute_spam_mass: whether to run the second (global) PPR pass.
    """
    graph = build_graph(content_type=content_type)
    from backend.db import get_pipeline_config
    from backend.routers.ppr import _get_ppr_config
    cfg = get_pipeline_config(graph_id=graph_id)
    ppr_cfg = _get_ppr_config(graph_id)
    halflife = cfg.get("temporal.recency_halflife_days", 0.0)
    alpha = float(ppr_cfg.get("alpha", 0.25))
    seeds = get_seed_weights(
        min_seed_rating=min_seed_rating,
        recency_halflife_days=halflife,
        graph_id=graph_id,
        watch_base=float(ppr_cfg.get("watch_base", WATCH_BASE)),
        playlist_base=float(ppr_cfg.get("playlist_base", PLAYLIST_BASE)),
        feed_rec_base=float(ppr_cfg.get("feed_rec_base", FEED_REC_BASE)),
    )

    if not seeds:
        return

    trusted_scores = compute_ppr(graph, seeds, alpha=alpha)
    global_scores = compute_global_ppr(graph) if compute_spam_mass else {}

    conn = get_db()
    watched = {r["video_id"] for r in conn.execute("SELECT video_id FROM watch_history").fetchall()}

    now = time.time()
    conn.execute("DELETE FROM ppr_scores WHERE graph_id = ?", (graph_id,))

    rows = []
    for vid, trusted in trusted_scores.items():
        if vid in watched or trusted <= 0:
            continue
        if compute_spam_mass:
            g = global_scores.get(vid, 0.0)
            spam_mass = max(0.0, min(1.0, (g - trusted) / g)) if g > 0 else None
        else:
            spam_mass = None
        rows.append((vid, graph_id, trusted, spam_mass, now))

    conn.executemany(
        "INSERT INTO ppr_scores (video_id, graph_id, score, spam_mass, computed_at) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()

    # Only prune feed_recommendations when updating the default graph (graph_id=1).
    # Prunes to 2000 highest-scoring unique unwatched videos using effective score.
    if graph_id == 1:
        conn.execute("""
            DELETE FROM feed_recommendations
            WHERE video_id NOT IN (
                SELECT fr.video_id
                FROM feed_recommendations fr
                LEFT JOIN ppr_scores p ON p.video_id = fr.video_id AND p.graph_id = 1
                LEFT JOIN watch_history wh ON wh.video_id = fr.video_id
                LEFT JOIN video_ratings vr ON vr.video_id = fr.video_id
                LEFT JOIN channel_ratings cr ON cr.channel_id = fr.author_id
                WHERE wh.video_id IS NULL
                GROUP BY fr.video_id
                ORDER BY COALESCE(MAX(p.score), 0) * CASE
                    WHEN COALESCE(vr.rating, cr.rating) IS NULL THEN 1.0
                    WHEN CAST(COALESCE(vr.rating, cr.rating) AS INTEGER) <= 1 THEN 0.0
                    WHEN CAST(COALESCE(vr.rating, cr.rating) AS INTEGER) <= 5
                        THEN CAST(COALESCE(vr.rating, cr.rating) AS REAL) / 5.0
                    ELSE (CAST(COALESCE(vr.rating, cr.rating) AS INTEGER) - 4)
                       * (CAST(COALESCE(vr.rating, cr.rating) AS INTEGER) - 4) * 1.0
                END DESC, MAX(fr.added_at) DESC
                LIMIT 2000
            )
        """)
        conn.commit()
    conn.close()


def explain_recommendation(video_id: str, graph_id: int = 1) -> dict:
    """Return the top seed videos that contributed to this video's recommendation score."""
    conn = get_db()

    sources_raw = conn.execute("""
        SELECT re.source_video_id, re.weight,
               COALESCE(wh.title, pv.title) as title,
               COALESCE(wh.author, pv.author) as author,
               COALESCE(wh.author_id, pv.author_id) as author_id
        FROM recommendation_edges re
        LEFT JOIN watch_history wh ON wh.video_id = re.source_video_id
        LEFT JOIN (
            SELECT video_id, title, author, author_id FROM playlist_videos GROUP BY video_id
        ) pv ON pv.video_id = re.source_video_id
        WHERE re.target_video_id = ?
        ORDER BY re.weight DESC
        LIMIT 24
    """, (video_id,)).fetchall()

    candidate_ids = [s["source_video_id"] for s in sources_raw]

    playlist_map: dict[str, list] = {}
    watched_set: set[str] = set()

    if candidate_ids:
        ph = ",".join("?" * len(candidate_ids))

        watched_set = {r["video_id"] for r in conn.execute(
            f"SELECT video_id FROM watch_history WHERE video_id IN ({ph})", candidate_ids
        ).fetchall()}

        for r in conn.execute(f"""
            SELECT pv.video_id, p.title as playlist_title
            FROM playlist_videos pv
            JOIN playlists p ON p.id = pv.playlist_id
            WHERE pv.video_id IN ({ph})
        """, candidate_ids).fetchall():
            playlist_map.setdefault(r["video_id"], []).append(r["playlist_title"])

    conn.close()

    author_map = {s["source_video_id"]: s["author_id"] for s in sources_raw if s["author_id"]}
    effective_rating = _load_effective_ratings(candidate_ids, author_map)

    # Load raw video ratings separately for display purposes
    video_rating_map: dict[str, int] = {}
    channel_rating_map: dict[str, int] = {}
    if candidate_ids:
        conn2 = get_db()
        ph = ",".join("?" * len(candidate_ids))
        video_rating_map = {r["video_id"]: int(r["rating"]) for r in conn2.execute(
            f"SELECT video_id, rating FROM video_ratings WHERE video_id IN ({ph})", candidate_ids
        ).fetchall()}
        author_ids = list({s["author_id"] for s in sources_raw if s["author_id"]})
        if author_ids:
            aph = ",".join("?" * len(author_ids))
            channel_rating_map = {r["channel_id"]: int(r["rating"]) for r in conn2.execute(
                f"SELECT channel_id, rating FROM channel_ratings WHERE channel_id IN ({aph})", author_ids
            ).fetchall()}
        conn2.close()

    def _seed(sid, author_id):
        """Mirror get_seed_weights(min_seed_rating=0) for a single video."""
        w = 0.0
        if sid in watched_set:
            w += WATCH_BASE
        if sid in playlist_map:
            w += PLAYLIST_BASE
        if w == 0.0:
            return 0.0
        r = effective_rating.get(sid)
        if r is not None:
            rm = rating_mult(r)
            if rm == 0.0:
                return 0.0
            w += rm
        return w

    scored = []
    for s in sources_raw:
        sid = s["source_video_id"]
        seed_w = _seed(sid, s["author_id"])
        contrib = s["weight"] * seed_w
        if contrib > 0:
            scored.append((contrib, seed_w, s))

    scored.sort(key=lambda x: x[0], reverse=True)
    sources = [s for _, _, s in scored[:8]]
    source_ids = [s["source_video_id"] for s in sources]

    factors = {}
    for sid in source_ids:
        f = []
        if sid in watched_set:
            f.append("watched")
        if sid in playlist_map:
            f.append(f"in playlist: {', '.join(playlist_map[sid])}")
        if sid in video_rating_map:
            f.append(f"rated {video_rating_map[sid]}/10")
        aid = next((s["author_id"] for s in sources if s["source_video_id"] == sid), None)
        if aid and aid in channel_rating_map:
            label = "channel rated"
            if sid not in video_rating_map:
                label = "channel rated (effective)"
            f.append(f"{label} {channel_rating_map[aid]}/10")
        factors[sid] = f or ["in recommendation graph"]

    conn3 = get_db()
    rules = conn3.execute(
        "SELECT rule_type, match_value, multiplier FROM weight_rules WHERE graph_id = ? ORDER BY multiplier DESC LIMIT 5",
        (graph_id,),
    ).fetchall()
    conn3.close()

    return {
        "sources": [
            {
                "video_id": s["source_video_id"],
                "title": s["title"] or s["source_video_id"],
                "author": s["author"],
                "author_id": s["author_id"],
                "edge_weight": s["weight"],
                "factors": factors.get(s["source_video_id"], []),
                "rating": video_rating_map.get(s["source_video_id"]) if source_ids else None,
                "effective_rating": effective_rating.get(s["source_video_id"]),
            }
            for s in sources
        ],
        "top_weight_rules": [dict(r) for r in rules],
    }


def explore_from_seeds(seeds_input: list, limit: int = 50) -> list:
    """Run PPR from an explicit set of seed videos/playlists/channels."""
    conn = get_db()
    seeds = {}

    for item in seeds_input:
        t = item.get("type")
        sid = item.get("id")
        if not sid:
            continue

        if t == "video":
            seeds[sid] = seeds.get(sid, 0) + 3.0

        elif t == "playlist":
            rows = conn.execute(
                "SELECT video_id FROM playlist_videos WHERE playlist_id = ?", (sid,)
            ).fetchall()
            for r in rows:
                seeds[r["video_id"]] = seeds.get(r["video_id"], 0) + 3.0

        elif t == "channel":
            rows = conn.execute(
                "SELECT video_id FROM watch_history WHERE author_id = ?", (sid,)
            ).fetchall()
            for r in rows:
                seeds[r["video_id"]] = seeds.get(r["video_id"], 0) + 2.0
            rows = conn.execute(
                "SELECT DISTINCT video_id FROM feed_recommendations WHERE author_id = ?", (sid,)
            ).fetchall()
            for r in rows:
                seeds[r["video_id"]] = seeds.get(r["video_id"], 0) + 2.0

    if seeds:
        seed_ids = list(seeds)
        ph = ",".join("?" * len(seed_ids))

        author_rows = conn.execute(
            f"SELECT video_id, author_id FROM watch_history WHERE video_id IN ({ph})", seed_ids
        ).fetchall()
        author_map = {r["video_id"]: r["author_id"] for r in author_rows if r["author_id"]}
        fr_rows = conn.execute(
            f"SELECT DISTINCT video_id, author_id FROM feed_recommendations WHERE video_id IN ({ph})", seed_ids
        ).fetchall()
        for r in fr_rows:
            if r["video_id"] not in author_map and r["author_id"]:
                author_map[r["video_id"]] = r["author_id"]

        eff = _load_effective_ratings(seed_ids, author_map)
        for vid, r in eff.items():
            if vid in seeds:
                seeds[vid] *= rating_mult(r)

    seeds = {k: v for k, v in seeds.items() if v > 0}
    total = sum(seeds.values())
    if not total:
        conn.close()
        return []
    seeds = {k: v / total for k, v in seeds.items()}

    graph = build_graph()
    scores = compute_ppr(graph, seeds)

    watched = {r["video_id"] for r in conn.execute("SELECT video_id FROM watch_history").fetchall()}

    candidates = sorted(
        [(vid, score) for vid, score in scores.items() if vid not in watched and score > 0],
        key=lambda x: x[1], reverse=True
    )[:limit * 6]

    if not candidates:
        conn.close()
        return []

    cand_ids = [vid for vid, _ in candidates]
    score_map = {vid: score for vid, score in candidates}
    ph = ",".join("?" * len(cand_ids))

    meta = {}
    for r in conn.execute(
        f"SELECT video_id, title, author, author_id, thumbnail, duration FROM feed_recommendations WHERE video_id IN ({ph}) GROUP BY video_id",
        cand_ids
    ).fetchall():
        meta[r["video_id"]] = dict(r)
    for r in conn.execute(
        f"SELECT video_id, title, author, author_id, thumbnail FROM watch_history WHERE video_id IN ({ph})",
        cand_ids
    ).fetchall():
        if r["video_id"] not in meta:
            meta[r["video_id"]] = dict(r)
    for r in conn.execute(
        f"SELECT video_id, title, author, author_id, thumbnail, duration FROM playlist_videos WHERE video_id IN ({ph})",
        cand_ids
    ).fetchall():
        if r["video_id"] not in meta:
            meta[r["video_id"]] = dict(r)

    unknown_ids = [vid for vid, _ in candidates if not meta.get(vid, {}).get("title")]
    if unknown_ids:
        try:
            import time as _time
            qconn = get_db()
            for vid in unknown_ids[:30]:
                qconn.execute(
                    "INSERT OR IGNORE INTO crawl_queue (video_id, status, added_at) VALUES (?, 'pending', ?)",
                    (vid, _time.time())
                )
            qconn.commit()
            qconn.close()
        except Exception:
            pass

    known = [(vid, s) for vid, s in candidates if meta.get(vid, {}).get("title")][:limit]
    conn.close()

    return [
        {
            "video_id": vid,
            "title": meta[vid]["title"],
            "author": meta[vid].get("author"),
            "author_id": meta[vid].get("author_id"),
            "thumbnail": meta[vid].get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "duration": meta[vid].get("duration"),
            "score": round(score_map[vid], 6),
        }
        for vid, _ in known
    ]
