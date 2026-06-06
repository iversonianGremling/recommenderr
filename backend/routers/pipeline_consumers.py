"""CRUD for user-registered downstream feed consumers (documentary).

Consumers describe external systems that read the recommendation feed. They are
documentary only — recommenderr does not push to them; the canvas renders them so
the pipeline's downstream edges stay legible. graph_id NULL = applies to all
graphs; a graph_id pins the consumer to one graph's lane.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from backend.db import get_db

router = APIRouter()

VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


class ConsumerCreate(BaseModel):
    name: str
    url: str = ""
    method: str = "GET"
    path: str = ""
    graph_id: int | None = None
    enabled: bool = True


class ConsumerUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    method: str | None = None
    path: str | None = None
    graph_id: int | None = None
    enabled: bool | None = None


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "graph_id": row["graph_id"],
        "name": row["name"],
        "url": row["url"],
        "method": row["method"],
        "path": row["path"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


@router.get("/consumers")
async def list_consumers(graph_id: int | None = None) -> list[dict]:
    """List registered consumers. With ?graph_id=, returns global (graph_id NULL)
    plus consumers pinned to that graph."""
    def _q():
        conn = get_db()
        try:
            if graph_id is None:
                rows = conn.execute(
                    "SELECT * FROM pipeline_consumers ORDER BY id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pipeline_consumers "
                    "WHERE graph_id IS NULL OR graph_id=? ORDER BY id",
                    (graph_id,),
                ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()
    return await run_in_threadpool(_q)


@router.post("/consumers")
async def create_consumer(body: ConsumerCreate) -> dict:
    method = body.method.upper().strip()
    if method not in VALID_METHODS:
        raise HTTPException(422, f"method must be one of {sorted(VALID_METHODS)}")
    if not body.name.strip():
        raise HTTPException(422, "name is required")

    def _q():
        conn = get_db()
        try:
            cur = conn.execute(
                """INSERT INTO pipeline_consumers
                   (graph_id, name, url, method, path, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (body.graph_id, body.name.strip(), body.url.strip(), method,
                 body.path.strip(), int(body.enabled), time.time()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM pipeline_consumers WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()
    return await run_in_threadpool(_q)


@router.patch("/consumers/{consumer_id}")
async def update_consumer(consumer_id: int, body: ConsumerUpdate) -> dict:
    def _q():
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM pipeline_consumers WHERE id=?", (consumer_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Consumer not found")
            updates: dict = {}
            if body.name is not None:
                updates["name"] = body.name.strip()
            if body.url is not None:
                updates["url"] = body.url.strip()
            if body.method is not None:
                m = body.method.upper().strip()
                if m not in VALID_METHODS:
                    raise HTTPException(422, f"Invalid method: {body.method}")
                updates["method"] = m
            if body.path is not None:
                updates["path"] = body.path.strip()
            if body.graph_id is not None:
                updates["graph_id"] = body.graph_id
            if body.enabled is not None:
                updates["enabled"] = int(body.enabled)
            if not updates:
                return _row_to_dict(row)
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE pipeline_consumers SET {sets} WHERE id=?",
                (*updates.values(), consumer_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM pipeline_consumers WHERE id=?", (consumer_id,)
            ).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()
    return await run_in_threadpool(_q)


@router.delete("/consumers/{consumer_id}")
async def delete_consumer(consumer_id: int) -> dict:
    def _q():
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT id FROM pipeline_consumers WHERE id=?", (consumer_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Consumer not found")
            conn.execute("DELETE FROM pipeline_consumers WHERE id=?", (consumer_id,))
            conn.commit()
            return {"ok": True, "deleted": consumer_id}
        finally:
            conn.close()
    return await run_in_threadpool(_q)
