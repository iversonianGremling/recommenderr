"""Cosine-similarity scorer for the recommendation graph.

PPR uses random-walk probability; cosine uses neighborhood overlap.
A candidate scores high when the videos that point TO it are the same
videos that point to the user's seeds — a one-hop similarity signal.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict

from backend.db import get_db
from backend.services.ppr_engine import get_seed_weights, _GRAPH_EDGE_QUERIES


def compute_cosine_scores(graph_id: int = 1, content_type: str = "mixed", min_seed_rating: int = 0) -> dict[str, float]:
    """Return {video_id: cosine_score} for unwatched candidates.

    Score = dot(seed_weights * edge_weights, cand_in_weights)
            / (norm(seeds) * norm(cand_in_edges))

    This measures: "how strongly do your liked videos directly recommend
    this candidate?" — normalized so high-degree nodes don't dominate.
    """
    seeds = get_seed_weights(min_seed_rating=min_seed_rating)
    if not seeds:
        return {}

    seed_norm = math.sqrt(sum(w * w for w in seeds.values()))
    if seed_norm == 0:
        return {}

    conn = get_db()

    # Forward pass: accumulate dot-product numerator for each candidate
    # by walking outgoing edges from every seed.
    seed_ids = list(seeds.keys())
    ph = ",".join("?" * len(seed_ids))

    # Filter edges by content_type
    if content_type == "music":
        edge_filter = (
            f"AND re.source_video_id IN ("
            f"  SELECT video_id FROM recognition_cache WHERE is_music=1 "
            f"  UNION SELECT video_id FROM music_library)"
        )
        out_q = (
            f"SELECT re.source_video_id, re.target_video_id, re.weight "
            f"FROM recommendation_edges re "
            f"WHERE re.source_video_id IN ({ph}) {edge_filter}"
        )
    elif content_type == "video":
        edge_filter = (
            f"AND re.source_video_id NOT IN ("
            f"  SELECT video_id FROM recognition_cache WHERE is_music=1)"
        )
        out_q = (
            f"SELECT re.source_video_id, re.target_video_id, re.weight "
            f"FROM recommendation_edges re "
            f"WHERE re.source_video_id IN ({ph}) {edge_filter}"
        )
    else:
        out_q = (
            f"SELECT source_video_id, target_video_id, weight FROM recommendation_edges "
            f"WHERE source_video_id IN ({ph})"
        )

    out_rows = conn.execute(out_q, seed_ids).fetchall()

    raw: dict[str, float] = {}
    for r in out_rows:
        src, tgt, ew = r["source_video_id"], r["target_video_id"], r["weight"]
        if tgt in seeds:
            continue  # skip seeds as candidates
        raw[tgt] = raw.get(tgt, 0.0) + seeds[src] * ew

    if not raw:
        conn.close()
        return {}

    # Compute in-edge magnitude for each candidate (all sources, not just seeds).
    cand_ids = list(raw.keys())
    cph = ",".join("?" * len(cand_ids))
    in_rows = conn.execute(
        f"SELECT target_video_id, weight FROM recommendation_edges WHERE target_video_id IN ({cph})",
        cand_ids,
    ).fetchall()

    in_norm_sq: dict[str, float] = defaultdict(float)
    for r in in_rows:
        in_norm_sq[r["target_video_id"]] += r["weight"] * r["weight"]

    # Filter watched
    watched = {r["video_id"] for r in conn.execute("SELECT video_id FROM watch_history").fetchall()}
    conn.close()

    scores: dict[str, float] = {}
    for vid, num in raw.items():
        if vid in watched:
            continue
        cand_norm = math.sqrt(in_norm_sq.get(vid, 0.0))
        if cand_norm == 0:
            continue
        scores[vid] = num / (seed_norm * cand_norm)

    return scores


def update_cosine_scores(graph_id: int = 1, content_type: str = "mixed", min_seed_rating: int = 0) -> int:
    """Recompute cosine scores for a named graph and persist to cosine_scores table."""
    scores = compute_cosine_scores(graph_id=graph_id, content_type=content_type, min_seed_rating=min_seed_rating)
    if not scores:
        return 0

    now = time.time()
    conn = get_db()
    conn.execute("DELETE FROM cosine_scores WHERE graph_id = ?", (graph_id,))
    conn.executemany(
        "INSERT INTO cosine_scores (video_id, graph_id, score, computed_at) VALUES (?, ?, ?, ?)",
        [(vid, graph_id, score, now) for vid, score in scores.items()],
    )
    conn.commit()
    conn.close()
    return len(scores)
