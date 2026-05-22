"""Persona PPR engine — run per-persona Personalized PageRank.

Personas are named seed bundles. Each persona has a set of items (yt_video IDs)
with weights. The engine resolves items → graph node keys (bare video IDs, since
the graph still uses bare YouTube video IDs), runs compute_ppr, and persists the
top-N scores to persona_scores.

Without Phase A.5 the graph is yt_video-only, so only yt_video persona seeds
contribute propagation. Seeds from other schemes are silently ignored (zero weight
in the graph traversal) but stored fine.
"""
from __future__ import annotations

import logging
import time

from backend.db import get_db
from backend.services.ppr_engine import build_graph, compute_ppr

logger = logging.getLogger(__name__)

PERSONA_TOP_N = 500


def _resolve_seeds(persona_id: int, conn) -> dict[str, float]:
    """Return {video_id: weight} for all yt_video seeds of this persona."""
    rows = conn.execute(
        """
        SELECT i.scheme, i.external_id, ps.weight
        FROM persona_seeds ps
        JOIN items i ON i.id = ps.item_id
        WHERE ps.persona_id = ?
        """,
        (persona_id,),
    ).fetchall()
    seeds: dict[str, float] = {}
    for r in rows:
        if r["scheme"] == "yt_video":
            seeds[r["external_id"]] = float(r["weight"])
    return seeds


def compute_persona_ppr(persona_id: int) -> int:
    """Recompute PPR scores for one persona. Returns number of rows persisted."""
    conn = get_db()
    row = conn.execute(
        "SELECT alpha, version FROM personas WHERE id = ?", (persona_id,)
    ).fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Persona {persona_id} not found")

    alpha = float(row["alpha"])
    claimed_version = int(row["version"])

    seeds = _resolve_seeds(persona_id, conn)
    conn.close()

    if not seeds:
        return 0

    graph = build_graph()
    scores = compute_ppr(graph, seeds, alpha=alpha)

    watched = set()
    conn2 = get_db()
    watched = {r["video_id"] for r in conn2.execute("SELECT video_id FROM watch_history").fetchall()}

    # Version check: abort if persona was modified while we were computing.
    current_version = conn2.execute(
        "SELECT version FROM personas WHERE id = ?", (persona_id,)
    ).fetchone()
    if not current_version or int(current_version["version"]) != claimed_version:
        conn2.close()
        logger.info("Persona %d version changed during compute — aborting persist", persona_id)
        return 0

    now = time.time()
    conn2.execute("DELETE FROM persona_scores WHERE persona_id = ?", (persona_id,))
    top_rows = sorted(
        ((vid, sc) for vid, sc in scores.items() if vid not in watched and sc > 0),
        key=lambda x: -x[1],
    )[:PERSONA_TOP_N]

    conn2.executemany(
        "INSERT INTO persona_scores (persona_id, video_id, score, computed_at) VALUES (?, ?, ?, ?)",
        [(persona_id, vid, sc, now) for vid, sc in top_rows],
    )
    conn2.commit()
    conn2.close()
    return len(top_rows)
