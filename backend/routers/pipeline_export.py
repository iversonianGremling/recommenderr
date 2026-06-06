"""Export / import pipeline canvas state as YAML.

Exports: pipeline_config, ppr_config, sources (enabled+weight), feed_filters,
         weight_rules, graphs (user-created, id>3), custom_modules, personas+seeds.

Does NOT export computed data (scores, edges) or user interaction data.
"""
from __future__ import annotations

import datetime
import json
import time
from typing import Any

import yaml
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()


# ---------------------------------------------------------------------------
# Converter CRUD
# ---------------------------------------------------------------------------

def _row_to_converter(row) -> dict:
    d = dict(row)
    d["sources"] = json.loads(d.get("sources") or "[]")
    d["graph_ids"] = json.loads(d.get("graph_ids") or "[]")
    d["config"] = json.loads(d.get("config") or "{}")
    d["mapping_code"] = d.get("mapping_code") or "{}"
    d["enabled"] = bool(d["enabled"])
    return d


def _attach_stats(converters: list[dict]) -> list[dict]:
    """Attach live DB stats to each converter based on its content_type."""
    from backend.db import get_db
    conn = get_db()

    crawl_stats = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) as n FROM crawl_queue GROUP BY status"
    ).fetchall()}
    edges_n = conn.execute("SELECT COUNT(*) as n FROM recommendation_edges").fetchone()["n"]
    last_crawl = conn.execute(
        "SELECT MAX(crawled_at) as t FROM crawl_queue WHERE status='done'"
    ).fetchone()["t"]

    music_stats = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) as n FROM music_jobs GROUP BY status"
    ).fetchall()}
    lib_n = conn.execute("SELECT COUNT(*) as n FROM music_library").fetchone()["n"]
    rec_n = conn.execute("SELECT COUNT(*) as n FROM recognition_cache").fetchone()["n"]
    rec_music_n = conn.execute(
        "SELECT COUNT(*) as n FROM recognition_cache WHERE is_music=1"
    ).fetchone()["n"]
    last_job = conn.execute(
        "SELECT MAX(updated_at) as t FROM music_jobs WHERE status='done'"
    ).fetchone()["t"]

    conn.close()

    for c in converters:
        if c["content_type"] == "video":
            c["stats"] = {
                "queue_pending": crawl_stats.get("pending", 0),
                "queue_done": crawl_stats.get("done", 0),
                "queue_failed": crawl_stats.get("failed", 0),
                "edges_total": edges_n,
                "last_crawled_at": last_crawl,
            }
        else:
            c["stats"] = {
                "jobs_pending": music_stats.get("pending", 0),
                "jobs_processing": music_stats.get("processing", 0),
                "jobs_done": music_stats.get("done", 0),
                "jobs_errors": music_stats.get("error", 0) + music_stats.get("failed", 0),
                "library_total": lib_n,
                "recognized_total": rec_n,
                "recognized_music": rec_music_n,
                "last_job_at": last_job,
            }
    return converters


class ConverterCreate(BaseModel):
    name: str
    description: str = ""
    content_type: str = "video"
    sources: list[str] = []
    graph_ids: list[int] = []
    config: dict = {}
    mapping_code: str = "{}"
    enabled: bool = True


class ConverterUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content_type: str | None = None
    sources: list[str] | None = None
    graph_ids: list[int] | None = None
    config: dict | None = None
    mapping_code: str | None = None
    enabled: bool | None = None


@router.get("/converters")
def list_converters() -> dict:
    from backend.db import get_db
    conn = get_db()
    rows = conn.execute("SELECT * FROM converters ORDER BY id").fetchall()
    conn.close()
    converters = [_row_to_converter(r) for r in rows]
    return {"converters": _attach_stats(converters)}


@router.post("/converters")
def create_converter(body: ConverterCreate) -> dict:
    if body.content_type not in ("video", "music", "mixed"):
        raise HTTPException(400, "content_type must be video, music, or mixed")
    from backend.db import get_db
    conn = get_db()
    now = time.time()
    try:
        cur = conn.execute(
            """INSERT INTO converters
               (name, description, content_type, sources, graph_ids, config, mapping_code, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                body.name.strip(),
                body.description.strip(),
                body.content_type,
                json.dumps(body.sources),
                json.dumps(body.graph_ids),
                json.dumps(body.config),
                body.mapping_code,
                1 if body.enabled else 0,
                now, now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM converters WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.close()
        return _row_to_converter(row)
    except Exception as exc:
        conn.close()
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(409, f"Converter named '{body.name}' already exists")
        raise HTTPException(500, str(exc))


@router.patch("/converters/{converter_id}")
def update_converter(converter_id: int, body: ConverterUpdate) -> dict:
    from backend.db import get_db
    conn = get_db()
    row = conn.execute("SELECT * FROM converters WHERE id=?", (converter_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Converter not found")
    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name.strip()
    if body.description is not None:
        updates["description"] = body.description.strip()
    if body.content_type is not None:
        if body.content_type not in ("video", "music", "mixed"):
            conn.close()
            raise HTTPException(400, "content_type must be video, music, or mixed")
        updates["content_type"] = body.content_type
    if body.sources is not None:
        updates["sources"] = json.dumps(body.sources)
    if body.graph_ids is not None:
        updates["graph_ids"] = json.dumps(body.graph_ids)
    if body.config is not None:
        updates["config"] = json.dumps(body.config)
    if body.mapping_code is not None:
        updates["mapping_code"] = body.mapping_code
    if body.enabled is not None:
        updates["enabled"] = 1 if body.enabled else 0
    updates["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in updates)
    try:
        conn.execute(f"UPDATE converters SET {sets} WHERE id=?", (*updates.values(), converter_id))
        conn.commit()
        row = conn.execute("SELECT * FROM converters WHERE id=?", (converter_id,)).fetchone()
        conn.close()
        return _row_to_converter(row)
    except Exception as exc:
        conn.close()
        if "UNIQUE constraint" in str(exc):
            raise HTTPException(409, "Another converter already has that name")
        raise HTTPException(500, str(exc))


@router.delete("/converters/{converter_id}")
def delete_converter(converter_id: int) -> dict:
    from backend.db import get_db
    conn = get_db()
    row = conn.execute("SELECT id FROM converters WHERE id=?", (converter_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Converter not found")
    conn.execute("DELETE FROM converters WHERE id=?", (converter_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce(value: str) -> Any:
    """Try to parse a DB string value as JSON scalar."""
    try:
        return json.loads(value)
    except Exception:
        return value


def _collect() -> dict:
    from backend.db import get_db
    conn = get_db()

    pipeline: dict = {}
    for row in conn.execute("SELECT key, value FROM pipeline_config").fetchall():
        pipeline[row["key"]] = _coerce(row["value"])

    ppr: dict = {}
    for row in conn.execute("SELECT key, value FROM ppr_config").fetchall():
        ppr[row["key"]] = _coerce(row["value"])

    sources: dict = {}
    for row in conn.execute("SELECT name, enabled, weight FROM sources").fetchall():
        sources[row["name"]] = {"enabled": bool(row["enabled"]), "weight": float(row["weight"])}

    feed_filters = [
        {"type": row["filter_type"], "value": row["match_value"]}
        for row in conn.execute(
            "SELECT filter_type, match_value FROM feed_filters ORDER BY id"
        ).fetchall()
    ]

    weight_rules = [
        {"type": row["rule_type"], "value": row["match_value"], "multiplier": float(row["multiplier"])}
        for row in conn.execute(
            "SELECT rule_type, match_value, multiplier FROM weight_rules ORDER BY id"
        ).fetchall()
    ]

    # user-created graphs only (id > 3 = not the 3 built-ins)
    graphs = [
        {
            "name": row["name"],
            "content_type": row["content_type"],
            "config_json": row["config_json"],
        }
        for row in conn.execute(
            "SELECT name, content_type, config_json FROM graphs WHERE id > 3 ORDER BY id"
        ).fetchall()
    ]

    custom_modules = [
        {
            "name": row["name"],
            "type": row["type"],
            "enabled": bool(row["enabled"]),
            "code": row["code"],
        }
        for row in conn.execute(
            "SELECT name, type, code, enabled FROM custom_modules ORDER BY id"
        ).fetchall()
    ]

    personas = []
    for row in conn.execute(
        "SELECT id, name, description, alpha, min_seed_rating FROM personas ORDER BY id"
    ).fetchall():
        seeds = [
            {"scheme": s["scheme"], "external_id": s["external_id"], "weight": float(s["weight"])}
            for s in conn.execute(
                """SELECT i.scheme, i.external_id, ps.weight
                   FROM persona_seeds ps
                   JOIN items i ON i.id = ps.item_id
                   WHERE ps.persona_id = ?
                   ORDER BY ps.weight DESC""",
                (row["id"],),
            ).fetchall()
        ]
        personas.append({
            "name": row["name"],
            "description": row["description"],
            "alpha": float(row["alpha"]),
            "min_seed_rating": int(row["min_seed_rating"]),
            "seeds": seeds,
        })

    conn.close()

    return {
        "version": 1,
        "exported_at": datetime.datetime.utcnow().isoformat() + "Z",
        "pipeline": pipeline,
        "ppr": ppr,
        "sources": sources,
        "feed_filters": feed_filters,
        "weight_rules": weight_rules,
        "graphs": graphs,
        "custom_modules": custom_modules,
        "personas": personas,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/converters")
def converters_status() -> dict:
    """Return per-converter ingestion pipeline stats."""
    from backend.db import get_db
    conn = get_db()

    crawl_rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM crawl_queue GROUP BY status"
    ).fetchall()
    crawl_stats = {r["status"]: r["n"] for r in crawl_rows}
    edges_n = conn.execute("SELECT COUNT(*) as n FROM recommendation_edges").fetchone()["n"]
    last_crawl = conn.execute(
        "SELECT MAX(crawled_at) as t FROM crawl_queue WHERE status='done'"
    ).fetchone()["t"]

    music_rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM music_jobs GROUP BY status"
    ).fetchall()
    music_stats = {r["status"]: r["n"] for r in music_rows}
    lib_n = conn.execute("SELECT COUNT(*) as n FROM music_library").fetchone()["n"]
    rec_n = conn.execute("SELECT COUNT(*) as n FROM recognition_cache").fetchone()["n"]
    rec_music_n = conn.execute(
        "SELECT COUNT(*) as n FROM recognition_cache WHERE is_music=1"
    ).fetchone()["n"]
    last_job = conn.execute(
        "SELECT MAX(updated_at) as t FROM music_jobs WHERE status='done'"
    ).fetchone()["t"]

    conn.close()

    return {
        "converters": [
            {
                "id": "video_crawler",
                "name": "Video Crawler",
                "description": (
                    "Fetches related-video recommendations from Invidious and builds "
                    "weighted video→video edges used by the PPR scorer."
                ),
                "sources": ["invidious"],
                "content_type": "video",
                "input": "Invidious API (related videos)",
                "output": "recommendation_edges + graph_feed_items",
                "stats": {
                    "queue_pending": crawl_stats.get("pending", 0),
                    "queue_done": crawl_stats.get("done", 0),
                    "queue_failed": crawl_stats.get("failed", 0),
                    "queue_retrying": crawl_stats.get("retrying", 0),
                    "edges_total": edges_n,
                    "last_crawled_at": last_crawl,
                },
            },
            {
                "id": "music_recognition",
                "name": "Music Recognition & Recommendations",
                "description": (
                    "Fingerprints music in video content, enriches with metadata from "
                    "Last.fm / Spotify / Deezer / iTunes / MusicBrainz, then aggregates "
                    "similar-track recommendations from all sources into a "
                    "confidence-weighted music recommendation graph."
                ),
                "sources": ["lastfm", "spotify", "deezer", "itunes", "musicbrainz", "bandcamp", "discogs"],
                "content_type": "music",
                "input": "Playlist videos → audio fingerprint → multi-source music APIs",
                "output": "recognition_cache + music_library + graph_feed_items",
                "stats": {
                    "jobs_pending": music_stats.get("pending", 0),
                    "jobs_processing": music_stats.get("processing", 0),
                    "jobs_done": music_stats.get("done", 0),
                    "jobs_errors": music_stats.get("error", 0) + music_stats.get("failed", 0),
                    "library_total": lib_n,
                    "recognized_total": rec_n,
                    "recognized_music": rec_music_n,
                    "last_job_at": last_job,
                },
            },
        ]
    }


@router.get("/export")
def export_pipeline():
    """Download the current pipeline configuration as pipeline.yaml."""
    data = _collect()
    text = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return Response(
        content=text,
        media_type="text/yaml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="pipeline.yaml"'},
    )


@router.get("/export/json")
def export_pipeline_json():
    """Return the current pipeline configuration as JSON."""
    return _collect()


@router.post("/import")
async def import_pipeline(file: UploadFile = File(...), dry_run: bool = False):
    """
    Import a pipeline.yaml previously exported by this endpoint.

    Pass ?dry_run=true to validate and preview what would change without writing.

    Safe rules:
    - pipeline / ppr config: upsert individual keys
    - sources: update enabled + weight only (never create new, never touch credentials)
    - feed_filters / weight_rules: replace entirely
    - graphs: insert-or-ignore (skip name conflicts with existing)
    - custom_modules: upsert by name
    - personas: insert-or-ignore by name; insert-or-ignore seeds
    """
    raw = await file.read()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"Invalid YAML: {exc}")

    if not isinstance(data, dict):
        raise HTTPException(400, "YAML root must be a mapping")

    from backend.db import get_db
    conn = get_db()
    now = time.time()
    applied: dict[str, int] = {}

    try:
        # --- pipeline_config ---
        pipeline = data.get("pipeline") or {}
        if isinstance(pipeline, dict):
            count = 0
            for k, v in pipeline.items():
                if isinstance(k, str):
                    conn.execute(
                        "INSERT OR REPLACE INTO pipeline_config (key, value, updated_at) VALUES (?, ?, ?)",
                        (k, json.dumps(v), now),
                    )
                    count += 1
            applied["pipeline_config"] = count

        # --- ppr_config ---
        ppr = data.get("ppr") or {}
        if isinstance(ppr, dict):
            count = 0
            for k, v in ppr.items():
                if isinstance(k, str):
                    conn.execute(
                        "INSERT OR REPLACE INTO ppr_config (key, value, updated_at) VALUES (?, ?, ?)",
                        (k, json.dumps(v), now),
                    )
                    count += 1
            applied["ppr_config"] = count

        # --- sources (enabled + weight only) ---
        sources = data.get("sources") or {}
        if isinstance(sources, dict):
            count = 0
            for name, cfg in sources.items():
                if not isinstance(cfg, dict):
                    continue
                fields: dict[str, Any] = {}
                if "enabled" in cfg:
                    fields["enabled"] = 1 if cfg["enabled"] else 0
                if "weight" in cfg:
                    fields["weight"] = float(cfg["weight"])
                if fields:
                    sets = ", ".join(f"{k} = ?" for k in fields)
                    conn.execute(
                        f"UPDATE sources SET {sets} WHERE name = ?",
                        list(fields.values()) + [name],
                    )
                    count += 1
            applied["sources"] = count

        # --- feed_filters (replace) ---
        feed_filters = data.get("feed_filters") or []
        if isinstance(feed_filters, list):
            conn.execute("DELETE FROM feed_filters")
            count = 0
            for f in feed_filters:
                if isinstance(f, dict) and "type" in f and "value" in f:
                    conn.execute(
                        "INSERT OR IGNORE INTO feed_filters (filter_type, match_value, created_at) VALUES (?, ?, ?)",
                        (str(f["type"]), str(f["value"]), now),
                    )
                    count += 1
            applied["feed_filters"] = count

        # --- weight_rules (replace) ---
        weight_rules = data.get("weight_rules") or []
        if isinstance(weight_rules, list):
            conn.execute("DELETE FROM weight_rules")
            count = 0
            for r in weight_rules:
                if isinstance(r, dict) and "type" in r and "value" in r:
                    mult = float(r.get("multiplier", 1.0))
                    conn.execute(
                        "INSERT OR IGNORE INTO weight_rules (rule_type, match_value, multiplier, created_at) VALUES (?, ?, ?, ?)",
                        (str(r["type"]), str(r["value"]), mult, now),
                    )
                    count += 1
            applied["weight_rules"] = count

        # --- graphs (insert-or-ignore user-created) ---
        graphs = data.get("graphs") or []
        if isinstance(graphs, list):
            count = 0
            for g in graphs:
                if not isinstance(g, dict) or "name" not in g:
                    continue
                ct = g.get("content_type", "mixed")
                if ct not in ("mixed", "music", "video", "album", "artist"):
                    ct = "mixed"
                conn.execute(
                    "INSERT OR IGNORE INTO graphs (name, content_type, config_json, created_at) VALUES (?, ?, ?, ?)",
                    (str(g["name"]), ct, g.get("config_json"), now),
                )
                count += 1
            applied["graphs"] = count

        # --- custom_modules (upsert by name) ---
        custom_modules = data.get("custom_modules") or []
        if isinstance(custom_modules, list):
            count = 0
            for m in custom_modules:
                if not isinstance(m, dict) or "name" not in m or "code" not in m:
                    continue
                mtype = m.get("type", "scorer")
                if mtype not in ("scorer", "filter"):
                    continue
                enabled = 1 if m.get("enabled", True) else 0
                existing = conn.execute(
                    "SELECT id FROM custom_modules WHERE name = ?", (m["name"],)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE custom_modules SET type=?, code=?, enabled=?, updated_at=? WHERE id=?",
                        (mtype, m["code"], enabled, now, existing["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO custom_modules (name, type, code, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                        (m["name"], mtype, m["code"], enabled, now, now),
                    )
                count += 1
            applied["custom_modules"] = count

        # --- personas (insert-or-ignore, then seeds) ---
        personas = data.get("personas") or []
        if isinstance(personas, list):
            count = 0
            for p in personas:
                if not isinstance(p, dict) or "name" not in p:
                    continue
                alpha = float(p.get("alpha", 0.15))
                min_rating = int(p.get("min_seed_rating", 0))
                existing = conn.execute(
                    "SELECT id FROM personas WHERE name = ?", (p["name"],)
                ).fetchone()
                if existing:
                    persona_id = existing["id"]
                else:
                    cur = conn.execute(
                        """INSERT INTO personas (name, description, alpha, min_seed_rating, created_at, updated_at, version)
                           VALUES (?, ?, ?, ?, ?, ?, 0)""",
                        (p["name"], p.get("description"), alpha, min_rating, now, now),
                    )
                    persona_id = cur.lastrowid

                for seed in (p.get("seeds") or []):
                    if not isinstance(seed, dict) or "scheme" not in seed or "external_id" not in seed:
                        continue
                    item_row = conn.execute(
                        "SELECT id FROM items WHERE scheme = ? AND external_id = ?",
                        (seed["scheme"], seed["external_id"]),
                    ).fetchone()
                    if not item_row:
                        continue
                    w = float(seed.get("weight", 1.0))
                    conn.execute(
                        "INSERT OR IGNORE INTO persona_seeds (persona_id, item_id, weight) VALUES (?, ?, ?)",
                        (persona_id, item_row["id"], w),
                    )
                count += 1
            applied["personas"] = count

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        raise HTTPException(500, f"Import failed: {exc}")

    conn.close()
    return {"ok": True, "dry_run": dry_run, "applied": applied}
