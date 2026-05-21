"""Shared-secret auth middleware for the recommenderr REST API.

Frontends send `Authorization: Bearer $RECOMMENDERR_TOKEN`. /health and /admin/*
are exempt (admin has its own ADMIN_TOKEN check).
"""
from __future__ import annotations

import os

from fastapi import Header, HTTPException, status

RECOMMENDERR_TOKEN = os.environ.get("RECOMMENDERR_TOKEN", "")


def require_service_token(authorization: str | None = Header(default=None)) -> None:
    if not RECOMMENDERR_TOKEN:
        # If no token is configured we run open — the deployment will set it.
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    presented = authorization.removeprefix("Bearer ").strip()
    if presented != RECOMMENDERR_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
        )
