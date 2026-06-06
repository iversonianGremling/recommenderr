"""Per-graph content source configuration."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

router = APIRouter()


class GraphSourceUpdate(BaseModel):
    in_graph: bool
    weight_override: float | None = None


@router.get("")
async def list_graph_sources_ep(graph_id: int) -> list:
    from backend.db import list_graph_sources
    return await run_in_threadpool(list_graph_sources, graph_id)


@router.put("/{source_name}")
async def update_graph_source(graph_id: int, source_name: str, body: GraphSourceUpdate) -> dict:
    from backend.db import upsert_graph_source, remove_graph_source
    from backend.services.source_registry import SOURCES_DECL
    if source_name not in SOURCES_DECL:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_name}")
    if body.in_graph:
        await run_in_threadpool(upsert_graph_source, graph_id, source_name, body.weight_override)
    else:
        await run_in_threadpool(remove_graph_source, graph_id, source_name)
    return {"ok": True}
