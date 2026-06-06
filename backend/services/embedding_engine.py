"""Content-based (semantic) scorer using ollama text embeddings.

Two stages, mirroring the cosine/serendipity scorers:

1. embed_catalog()  — embed each video's title + description + keywords once,
   cached in video_embeddings (re-embedded only when the text or model changes).
   This is the only stage that talks to ollama / costs compute.

2. update_embedding_scores(graph_id) — build a Rocchio "taste vector"
   (centroid of liked/seeded videos − beta·centroid of disliked videos) and
   score every candidate by cosine similarity to it, into embedding_scores.
   Pure vector math, no ollama — runs in the feed_cache refresh path.

The blend in db.get_ppr_feed adds  w_embedding · embedding_scores.score  to the
final score (scorer.embedding.* pipeline config; weight slider on the canvas).
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import struct
import time
from array import array

import httpx

from backend.db import get_db
from backend.services.ppr_engine import get_seed_weights

logger = logging.getLogger("embedding_engine")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.1.176:11434").rstrip("/")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
# Rocchio negative weight: how hard disliked content pushes the taste vector away.
ROCCHIO_BETA = float(os.environ.get("EMBED_ROCCHIO_BETA", "0.5"))
# Strong explicit-feedback weights relative to implicit seed weights.
POS_FEEDBACK_WEIGHT = 3.0


# ── storage helpers ────────────────────────────────────────────────────────

def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def _text_hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\x00{text}".encode("utf-8")).hexdigest()


def ensure_tables(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS video_embeddings (
               video_id   TEXT PRIMARY KEY,
               model      TEXT NOT NULL,
               dim        INTEGER NOT NULL,
               vec        BLOB NOT NULL,
               text_hash  TEXT NOT NULL,
               updated_at REAL NOT NULL
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS embedding_scores (
               video_id    TEXT NOT NULL,
               graph_id    INTEGER NOT NULL DEFAULT 1,
               score       REAL NOT NULL,
               computed_at REAL NOT NULL,
               PRIMARY KEY (video_id, graph_id)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_embedding_score ON embedding_scores(graph_id, score DESC)"
    )


# ── ollama ─────────────────────────────────────────────────────────────────

def _embed(client: httpx.Client, text: str) -> list[float] | None:
    try:
        r = client.post("/api/embeddings", json={"model": EMBED_MODEL, "prompt": text})
        r.raise_for_status()
        vec = r.json().get("embedding")
        return vec if vec else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("embedding_engine: embed failed: %s", exc)
        return None


def ollama_health() -> dict:
    """Cheap reachability check for the admin UI / endpoints."""
    try:
        with httpx.Client(base_url=OLLAMA_URL, timeout=4.0) as c:
            r = c.get("/api/tags")
            r.raise_for_status()
            models = [m.get("name") for m in r.json().get("models", [])]
            return {"ok": True, "url": OLLAMA_URL, "model": EMBED_MODEL,
                    "model_present": any(EMBED_MODEL in (m or "") for m in models)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": OLLAMA_URL, "error": str(exc)}


# ── stage 1: embed catalog ─────────────────────────────────────────────────

def _corpus_text(title: str, genre: str, keywords: str, description: str) -> str:
    parts = []
    if title:
        parts.append(title.strip())
    head = " ".join(p for p in (genre.strip(), keywords.strip()) if p)
    if head:
        parts.append(head)
    if description:
        # Keep it short: title+keywords carry most topical signal and shorter
        # input is markedly faster on CPU. The intro usually states the topic.
        parts.append(description.strip()[:500])
    return "\n".join(parts).strip()


def embed_catalog(limit: int = 500) -> dict:
    """Embed up to `limit` videos whose embedding is missing or stale (text/model
    changed). Returns {embedded, skipped, remaining}. Talks to ollama."""
    conn = get_db()
    conn.execute("PRAGMA busy_timeout=15000")
    ensure_tables(conn)

    # Keyword bag per video.
    kw_map: dict[str, str] = {}
    for r in conn.execute(
        "SELECT video_id, GROUP_CONCAT(keyword, ' ') AS kw FROM video_keywords GROUP BY video_id"
    ):
        kw_map[r["video_id"]] = r["kw"] or ""

    # Candidate corpus: anything we have a title (feed pool) or metadata for.
    rows = conn.execute(
        """SELECT v.video_id,
                  COALESCE(fr.title, '')       AS title,
                  COALESCE(vm.genre, '')       AS genre,
                  COALESCE(vm.description, '')  AS description
           FROM (SELECT video_id FROM feed_recommendations
                 UNION SELECT video_id FROM video_metadata) v
           LEFT JOIN (SELECT video_id, MAX(title) AS title
                        FROM feed_recommendations GROUP BY video_id) fr
                  ON fr.video_id = v.video_id
           LEFT JOIN video_metadata vm ON vm.video_id = v.video_id"""
    ).fetchall()

    existing = {
        r["video_id"]: r["text_hash"]
        for r in conn.execute("SELECT video_id, text_hash FROM video_embeddings")
    }

    embedded = skipped = 0
    pending: list[tuple[str, str, str]] = []  # (video_id, text, text_hash)
    for r in rows:
        text = _corpus_text(r["title"], r["genre"], kw_map.get(r["video_id"], ""), r["description"])
        if not text:
            continue
        th = _text_hash(EMBED_MODEL, text)
        if existing.get(r["video_id"]) == th:
            skipped += 1
            continue
        pending.append((r["video_id"], text, th))

    remaining = max(0, len(pending) - limit)
    pending = pending[:limit]

    if pending:
        with httpx.Client(base_url=OLLAMA_URL, timeout=60.0) as client:
            for vid, text, th in pending:
                # Embed with NO open transaction so the slow network call never
                # holds the sqlite write lock; commit each row immediately so
                # concurrent writers (feed_cache refresh, workers) aren't blocked.
                vec = _embed(client, text)
                if not vec:
                    continue
                conn.execute(
                    """INSERT INTO video_embeddings (video_id, model, dim, vec, text_hash, updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(video_id) DO UPDATE SET
                         model=excluded.model, dim=excluded.dim, vec=excluded.vec,
                         text_hash=excluded.text_hash, updated_at=excluded.updated_at""",
                    (vid, EMBED_MODEL, len(vec), _pack(vec), th, time.time()),
                )
                conn.commit()
                embedded += 1

    conn.close()
    logger.info("embedding_engine: embedded %d, skipped %d, remaining %d", embedded, skipped, remaining)
    return {"embedded": embedded, "skipped": skipped, "remaining": remaining, "model": EMBED_MODEL}


# ── stage 2: score candidates ──────────────────────────────────────────────

def _load_vecs(conn, video_ids: list[str]) -> dict[str, list[float]]:
    if not video_ids:
        return {}
    out: dict[str, list[float]] = {}
    CHUNK = 400
    for i in range(0, len(video_ids), CHUNK):
        chunk = video_ids[i:i + CHUNK]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT video_id, vec FROM video_embeddings WHERE video_id IN ({ph})", chunk
        ):
            out[r["video_id"]] = _unpack(r["vec"])
    return out


def _weighted_centroid(vecs: dict[str, list[float]], weights: dict[str, float], dim: int) -> list[float] | None:
    acc = [0.0] * dim
    total = 0.0
    for vid, w in weights.items():
        v = vecs.get(vid)
        if not v or w <= 0:
            continue
        for i in range(dim):
            acc[i] += w * v[i]
        total += w
    if total <= 0:
        return None
    return [x / total for x in acc]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def update_embedding_scores(graph_id: int = 1, content_type: str = "mixed",
                            min_seed_rating: int = 0, beta: float = ROCCHIO_BETA) -> int:
    """Score this graph's candidates by cosine to the Rocchio taste vector.
    Returns the number of candidates scored (0 if no positive signal/embeddings)."""
    conn = get_db()
    conn.execute("PRAGMA busy_timeout=15000")
    ensure_tables(conn)

    # Candidates = this graph's PPR-scored videos that have an embedding.
    cand_ids = [r["video_id"] for r in conn.execute(
        "SELECT video_id FROM ppr_scores WHERE graph_id=?", (graph_id,)
    )]
    if not cand_ids:
        conn.close()
        return 0

    # Positive signal: implicit seeds (history/ratings/playlists) + explicit +1.
    pos_weights: dict[str, float] = dict(get_seed_weights(min_seed_rating=min_seed_rating, graph_id=graph_id))
    neg_ids: list[str] = []
    for r in conn.execute("SELECT video_id, feedback FROM feed_feedback"):
        if r["feedback"] == 1:
            pos_weights[r["video_id"]] = pos_weights.get(r["video_id"], 0.0) + POS_FEEDBACK_WEIGHT
        elif r["feedback"] == -1:
            neg_ids.append(r["video_id"])

    # Gather every vector we need in as few queries as possible.
    need = set(cand_ids) | set(pos_weights) | set(neg_ids)
    vecs = _load_vecs(conn, list(need))
    if not vecs:
        conn.close()
        return 0
    dim = len(next(iter(vecs.values())))

    taste_pos = _weighted_centroid(vecs, pos_weights, dim)
    if taste_pos is None:
        conn.close()
        return 0
    neg_centroid = _weighted_centroid(vecs, {vid: 1.0 for vid in neg_ids}, dim)
    taste = taste_pos
    if neg_centroid is not None and beta > 0:
        taste = [taste_pos[i] - beta * neg_centroid[i] for i in range(dim)]

    now = time.time()
    scored = []
    for vid in cand_ids:
        v = vecs.get(vid)
        if not v:
            continue
        s = _cosine(taste, v)
        if s > 0:  # negatives contribute nothing; keep scale ~[0,1] like cosine_scores
            scored.append((vid, graph_id, s, now))

    conn.execute("DELETE FROM embedding_scores WHERE graph_id=?", (graph_id,))
    if scored:
        conn.executemany(
            "INSERT INTO embedding_scores (video_id, graph_id, score, computed_at) VALUES (?,?,?,?)",
            scored,
        )
    conn.commit()
    conn.close()
    logger.info("embedding_engine: graph %d scored %d candidates", graph_id, len(scored))
    return len(scored)
