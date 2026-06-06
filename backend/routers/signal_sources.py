"""Configurable user signal source CRUD + manual sync trigger."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from backend.db import get_db

router = APIRouter(tags=["signal-sources"])

VALID_KINDS = {"watch_history", "likes", "playlists", "custom"}
VALID_CONVERTERS = {"ytfront_v1", "ytfront_likes_v1", "native"}


class SignalSourceCreate(BaseModel):
    name: str
    kind: str
    endpoint_url: str
    converter: str = "ytfront_v1"
    auth_header: str | None = None
    enabled: bool = True


class SignalSourceUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    endpoint_url: str | None = None
    converter: str | None = None
    auth_header: str | None = None
    enabled: bool | None = None


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "endpoint_url": row["endpoint_url"],
        "converter": row["converter"],
        "auth_header": row["auth_header"],
        "enabled": bool(row["enabled"]),
        "is_system": bool(row["is_system"]),
        "created_at": row["created_at"],
        "last_synced_at": row["last_synced_at"],
        "last_count": row["last_count"],
        "last_error": row["last_error"],
    }


@router.get("")
async def list_signal_sources() -> list[dict]:
    def _q():
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM signal_sources ORDER BY id"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()
    return await run_in_threadpool(_q)


@router.post("")
async def create_signal_source(body: SignalSourceCreate) -> dict:
    if body.kind not in VALID_KINDS:
        raise HTTPException(422, f"kind must be one of {sorted(VALID_KINDS)}")
    if body.converter not in VALID_CONVERTERS:
        raise HTTPException(422, f"converter must be one of {sorted(VALID_CONVERTERS)}")

    def _q():
        conn = get_db()
        try:
            cur = conn.execute(
                """INSERT INTO signal_sources
                   (name, kind, endpoint_url, converter, auth_header, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (body.name, body.kind, body.endpoint_url, body.converter,
                 body.auth_header, int(body.enabled), time.time()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM signal_sources WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            return _row_to_dict(row)
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise HTTPException(409, f"Signal source '{body.name}' already exists")
            raise
        finally:
            conn.close()
    return await run_in_threadpool(_q)


@router.patch("/{source_id}")
async def update_signal_source(source_id: int, body: SignalSourceUpdate) -> dict:
    def _q():
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM signal_sources WHERE id=?", (source_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Signal source not found")
            updates: dict = {}
            if body.name is not None:
                updates["name"] = body.name
            if body.kind is not None:
                if body.kind not in VALID_KINDS:
                    raise HTTPException(422, f"Invalid kind: {body.kind}")
                updates["kind"] = body.kind
            if body.endpoint_url is not None:
                updates["endpoint_url"] = body.endpoint_url
            if body.converter is not None:
                if body.converter not in VALID_CONVERTERS:
                    raise HTTPException(422, f"Invalid converter: {body.converter}")
                updates["converter"] = body.converter
            if body.auth_header is not None:
                updates["auth_header"] = body.auth_header
            if body.enabled is not None:
                updates["enabled"] = int(body.enabled)
            if not updates:
                return _row_to_dict(row)
            sets = ", ".join(f"{k}=?" for k in updates)
            conn.execute(
                f"UPDATE signal_sources SET {sets} WHERE id=?",
                (*updates.values(), source_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM signal_sources WHERE id=?", (source_id,)
            ).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()
    return await run_in_threadpool(_q)


@router.delete("/{source_id}")
async def delete_signal_source(source_id: int) -> dict:
    def _q():
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM signal_sources WHERE id=?", (source_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Signal source not found")
            if row["is_system"]:
                raise HTTPException(400, "Cannot delete system signal sources")
            conn.execute("DELETE FROM signal_sources WHERE id=?", (source_id,))
            conn.commit()
            return {"ok": True, "deleted": source_id}
        finally:
            conn.close()
    return await run_in_threadpool(_q)


@router.post("/{source_id}/sync")
async def sync_signal_source_endpoint(source_id: int) -> dict:
    """Trigger an immediate sync for one signal source."""
    def _get():
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT * FROM signal_sources WHERE id=?", (source_id,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "Signal source not found")
            return dict(row)
        finally:
            conn.close()

    source = await run_in_threadpool(_get)
    from backend.services.user_data_sync import sync_source
    result = await sync_source(source)
    return result
