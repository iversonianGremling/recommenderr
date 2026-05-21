"""Items router — browse the generic item store and manage schemes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

router = APIRouter()


@router.get("/schemes")
async def list_schemes() -> list[dict]:
    from backend.db.items import list_schemes as _list
    return await run_in_threadpool(_list)


class SchemeCreate(BaseModel):
    name: str
    display_name: str
    description: str = ""
    fields: list[dict] = []


@router.post("/schemes")
async def create_scheme(body: SchemeCreate) -> dict:
    from backend.db.items import register_scheme
    await run_in_threadpool(
        register_scheme, body.name, body.display_name, body.fields, body.description
    )
    return {"ok": True, "name": body.name}


@router.get("")
@router.get("/")
async def search_items(
    scheme: str | None = Query(None),
    q: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    from backend.db.items import search_items as _search
    return await run_in_threadpool(_search, scheme, q, limit, offset)


@router.get("/{item_id}")
async def get_item(item_id: int) -> dict:
    from backend.db.items import get_item as _get
    item = await run_in_threadpool(_get, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item
