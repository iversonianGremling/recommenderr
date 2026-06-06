"""
fetch_health — shared live-state bus for the YouTube fetch pipeline.

Aggregates, in one place that the frontend overlay and admin UI can poll:
  * exit state (current Mullvad relay/IP, rotations, recent bot-blocks) from
    :mod:`exit_manager`;
  * per-method health for ``invidious`` / ``camoufox`` / ``ytdlp`` — up/down,
    last success, last error;
  * a rolling bot-block count.

A background heartbeat (``heartbeat()``, started from the FastAPI lifespan)
refreshes the gateway exit and pings each backend on an interval so the state
is always current — "everything communicating constantly".  Fetch code can also
push outcomes directly via :func:`record_success` / :func:`record_failure`.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import httpx

from backend.services import exit_manager

logger = logging.getLogger("fetch_health")

HEARTBEAT_INTERVAL = float(os.getenv("FETCH_HEALTH_INTERVAL", "30"))
INVIDIOUS_URL = (os.getenv("INVIDIOUS_URL") or "http://192.168.1.173:3000").rstrip("/")
CAMOUFOX_URL = (os.getenv("CAMOUFOX_URL") or "").rstrip("/")

_METHODS = ("invidious", "camoufox", "ytdlp")
_methods: dict[str, dict] = {
    m: {"status": "unknown", "last_ok_ts": None, "last_err": None, "last_err_ts": None}
    for m in _METHODS
}
_updated_at: float = 0.0

# Gateway-authoritative per-relay stats (fetched from CT103 /api/relay-stats).
_relay_stats: list = []
_RELAY_STATS_URL = exit_manager.GATEWAY_URL + "/api/relay-stats"


async def _probe_relay_stats() -> None:
    global _relay_stats
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_RELAY_STATS_URL)
        if resp.status_code == 200:
            _relay_stats = resp.json().get("relays", [])
    except Exception:  # noqa: BLE001
        pass


def record_success(method: str) -> None:
    st = _methods.get(method)
    if st is None:
        return
    st["status"] = "up"
    st["last_ok_ts"] = time.time()


def record_failure(method: str, err: str = "") -> None:
    st = _methods.get(method)
    if st is None:
        return
    st["status"] = "down"
    st["last_err"] = (err or "")[:300]
    st["last_err_ts"] = time.time()


async def _probe_invidious() -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{INVIDIOUS_URL}/api/v1/stats")
        if resp.status_code == 200:
            record_success("invidious")
        else:
            record_failure("invidious", f"HTTP {resp.status_code}")
    except Exception as e:  # noqa: BLE001
        record_failure("invidious", str(e))


async def _probe_camoufox() -> None:
    if not CAMOUFOX_URL:
        _methods["camoufox"]["status"] = "disabled"
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{CAMOUFOX_URL}/health")
        if resp.status_code == 200:
            record_success("camoufox")
        else:
            record_failure("camoufox", f"HTTP {resp.status_code}")
    except Exception as e:  # noqa: BLE001
        record_failure("camoufox", str(e))


def _derive_ytdlp_status() -> None:
    """ytdlp has no cheap probe; derive from recent bot-block pressure."""
    st = _methods["ytdlp"]
    recent = exit_manager.recent_bot_blocks(window=300.0)
    if recent >= 5:
        st["status"] = "degraded"
        st["last_err"] = f"{recent} bot-blocks in last 5 min"
        st["last_err_ts"] = time.time()
    elif st["last_ok_ts"] and time.time() - st["last_ok_ts"] < 600:
        st["status"] = "up"
    # else leave prior status (unknown until first extraction)


async def heartbeat(interval: float = HEARTBEAT_INTERVAL) -> None:
    """Periodically refresh exit + method health. Runs for the app lifetime."""
    global _updated_at
    logger.info("fetch_health heartbeat started (interval=%.0fs)", interval)
    while True:
        try:
            await exit_manager.refresh_status()
            await asyncio.gather(_probe_invidious(), _probe_camoufox(), _probe_relay_stats())
            _derive_ytdlp_status()
            _updated_at = time.time()
        except asyncio.CancelledError:
            logger.info("fetch_health heartbeat stopped")
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("fetch_health heartbeat error: %s", e)
        await asyncio.sleep(interval)


def snapshot() -> dict:
    exit_state = exit_manager.state()
    # Gateway is authoritative for relay reliability/selection — prefer its stats.
    if _relay_stats:
        exit_state["relay_stats"] = _relay_stats
    return {
        "updated_at": _updated_at,
        "exit": exit_state,
        "methods": _methods,
    }
