"""Fetch-pipeline health router — exposes /fetch/* observability endpoints.

Surfaces the shared live state (exit IP/relay, rotations, per-backend health)
for the player overlay and admin UI, and allows a manual exit rotation.
"""
from __future__ import annotations

from fastapi import APIRouter

from backend.services import exit_manager, fetch_health

router = APIRouter()


@router.get("/health")
async def fetch_health_snapshot() -> dict:
    """Live snapshot of exit state + per-method health."""
    return fetch_health.snapshot()


@router.post("/rotate")
async def fetch_rotate() -> dict:
    """Manually rotate the Mullvad exit IP (coalesced with the cooldown)."""
    return await exit_manager.rotate("manual")
