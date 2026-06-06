"""Serendipity scorer: high PPR but low direct seed adjacency (cosine)."""
import time
from backend.db import get_db


def compute_serendipity_scores(graph_id: int = 1) -> dict[str, float]:
    conn = get_db()
    rows = conn.execute("""
        SELECT p.video_id, p.score as ppr_score, COALESCE(c.score, 0) as cosine_score
        FROM ppr_scores p
        LEFT JOIN cosine_scores c ON c.video_id = p.video_id AND c.graph_id = p.graph_id
        WHERE p.score > 0 AND p.graph_id = ?
    """, (graph_id,)).fetchall()
    conn.close()

    if not rows:
        return {}

    max_cosine = max((r["cosine_score"] for r in rows), default=1e-9) or 1e-9

    scores = {}
    for r in rows:
        cos_norm = r["cosine_score"] / max_cosine
        scores[r["video_id"]] = r["ppr_score"] * (1.0 - cos_norm)

    return scores


def update_serendipity_scores(graph_id: int = 1) -> int:
    scores = compute_serendipity_scores(graph_id=graph_id)
    if not scores:
        return 0
    now = time.time()
    conn = get_db()
    conn.execute("DELETE FROM serendipity_scores WHERE graph_id=?", (graph_id,))
    conn.executemany(
        "INSERT INTO serendipity_scores (video_id, graph_id, score, computed_at) VALUES (?, ?, ?, ?)",
        [(vid, graph_id, score, now) for vid, score in scores.items()]
    )
    conn.commit()
    conn.close()
    return len(scores)
