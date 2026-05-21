"""Server-side feed page cache.

Pre-computes PRECOMPUTE_LIMIT feed items once in a background task and serves
slices immediately.  A request never blocks on PPR recomputation — stale data
is returned while a background refresh runs, then swapped atomically.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("feed_cache")

PRECOMPUTE_LIMIT = 500
REFRESH_INTERVAL = 270.0   # seconds before cache is considered stale (~4.5 min)


@dataclass
class _Snapshot:
    items: list[dict] = field(default_factory=list)
    computed_at: float = 0.0


_snapshot: _Snapshot = _Snapshot()
_is_refreshing = False
_initial_ready = asyncio.Event()
_refresh_lock: Optional[asyncio.Lock] = None   # created inside the event loop


def _get_lock() -> asyncio.Lock:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    return _refresh_lock


async def _do_refresh() -> None:
    global _snapshot, _is_refreshing
    _is_refreshing = True
    try:
        from backend.services.user_data_sync import sync_user_data_cache
        from backend.services.ppr_engine import update_ppr_scores
        from backend.db import get_ppr_feed
        await sync_user_data_cache()
        # Run recompute + query in a thread so the event loop stays responsive.
        def _compute():
            update_ppr_scores()
            return get_ppr_feed(limit=PRECOMPUTE_LIMIT, offset=0, sort="score", _skip_recompute=True)
        items = await asyncio.get_running_loop().run_in_executor(None, _compute)
        _snapshot = _Snapshot(items=items, computed_at=time.monotonic())
        logger.info("feed_cache: refreshed %d items", len(items))
    except Exception as exc:
        logger.warning("feed_cache: refresh failed: %s", exc)
    finally:
        _is_refreshing = False
        _initial_ready.set()


async def warm() -> None:
    """Trigger an immediate foreground-ish warm (called at lifespan startup)."""
    await _do_refresh()


async def ensure_fresh() -> None:
    """Called on every feed request.

    Returns immediately.  If the cache is stale and no refresh is running,
    fires a background task to refresh it.  Callers always get the current
    (possibly stale) snapshot via :func:`get_page`.
    """
    if _is_refreshing:
        return
    age = time.monotonic() - _snapshot.computed_at
    if age < REFRESH_INTERVAL:
        return
    # Stale — kick off background refresh without awaiting it.
    asyncio.ensure_future(_do_refresh())


async def wait_for_initial() -> None:
    """Block until at least one successful computation has completed."""
    await asyncio.wait_for(_initial_ready.wait(), timeout=30.0)


def get_page(offset: int = 0, limit: int = 100) -> tuple[list[dict], int]:
    """Return (items_slice, total) from the current snapshot. Always instant."""
    items = _snapshot.items
    return items[offset: offset + limit], len(items)
