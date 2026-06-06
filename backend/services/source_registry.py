"""Source registry: declarative source catalogue + DB-backed runtime state.

Code declares available sources in SOURCES_DECL (only code can add new sources).
The DB persists runtime state: enabled, weight, credential overrides, health.

Usage
-----
    from backend.services.source_registry import get_credential, is_available, mark_success, mark_failure, with_source

    @with_source("lastfm")
    async def lastfm_search_track(...) -> list[dict]:
        key = get_credential("lastfm", "LASTFM_KEY")
        ...
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("source_registry")

# ---------------------------------------------------------------------------
# Declarations (code-side; UI cannot add new sources, only configure)
# ---------------------------------------------------------------------------

@dataclass
class SourceDecl:
    display_name: str
    kind: str = "api"                    # api | scraper | extractor | feed
    env_vars: list[str] = field(default_factory=list)
    requires_credentials: bool = False
    default_weight: float = 1.0
    default_rate_limit_per_min: int | None = None
    default_circuit_threshold: int = 3   # failures before tripping
    default_backoff_secs: float = 120.0


SOURCES_DECL: dict[str, SourceDecl] = {
    "lastfm": SourceDecl(
        display_name="Last.fm",
        env_vars=["LASTFM_KEY"],
        requires_credentials=True,
        default_weight=0.85,
        default_rate_limit_per_min=300,
    ),
    "spotify": SourceDecl(
        display_name="Spotify",
        env_vars=["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"],
        requires_credentials=True,
        default_weight=1.0,
        default_rate_limit_per_min=180,
    ),
    "deezer": SourceDecl(
        display_name="Deezer",
        env_vars=[],
        default_weight=0.90,
        default_rate_limit_per_min=50,
    ),
    "itunes": SourceDecl(
        display_name="iTunes",
        env_vars=[],
        default_weight=0.55,
    ),
    "musicbrainz": SourceDecl(
        display_name="MusicBrainz",
        env_vars=[],
        default_weight=0.80,
        default_rate_limit_per_min=60,
    ),
    "bandcamp": SourceDecl(
        display_name="Bandcamp",
        kind="scraper",
        env_vars=[],
        default_weight=0.70,
        default_rate_limit_per_min=5,
        default_circuit_threshold=5,
    ),
    "discogs": SourceDecl(
        display_name="Discogs",
        env_vars=["DISCOGS_TOKEN"],
        requires_credentials=True,
        default_weight=0.60,
        default_rate_limit_per_min=60,
    ),
    "invidious": SourceDecl(
        display_name="Invidious",
        kind="api",
        env_vars=["INVIDIOUS_URL"],
        requires_credentials=True,
        default_weight=1.0,
        default_circuit_threshold=2,
        default_backoff_secs=60.0,
    ),
    "ytdlp": SourceDecl(
        display_name="yt-dlp",
        kind="extractor",
        env_vars=["YTDLP_COOKIES_FILE", "YTDLP_COOKIES_FROM_BROWSER"],
        default_weight=1.0,
        default_circuit_threshold=1,
        default_backoff_secs=120.0,
    ),
    "youtube_rss": SourceDecl(
        display_name="YouTube RSS",
        kind="feed",
        env_vars=[],
        default_weight=0.90,
    ),
    "user_signals": SourceDecl(
        display_name="User Signals",
        kind="feedback",
        env_vars=[],
        default_weight=1.0,
        default_circuit_threshold=99,
    ),
}

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def seed_sources_table() -> None:
    """Insert or ignore each declared source into the DB on startup."""
    from backend.db import get_db
    conn = get_db()
    now = time.time()
    for name, decl in SOURCES_DECL.items():
        meta = {
            "env_vars": decl.env_vars,
            "requires_credentials": decl.requires_credentials,
            "default_weight": decl.default_weight,
            "default_circuit_threshold": decl.default_circuit_threshold,
            "default_backoff_secs": decl.default_backoff_secs,
        }
        conn.execute(
            """
            INSERT OR IGNORE INTO sources
                (name, display_name, kind, enabled, weight, rate_limit_per_min, metadata_json)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (name, decl.display_name, decl.kind, decl.default_weight,
             decl.default_rate_limit_per_min, json.dumps(meta)),
        )
    conn.commit()
    conn.close()


def _get_row(name: str) -> dict | None:
    from backend.db import get_db
    conn = get_db()
    row = conn.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _set_fields(name: str, **kwargs: Any) -> None:
    from backend.db import get_db
    if not kwargs:
        return
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [name]
    conn = get_db()
    conn.execute(f"UPDATE sources SET {sets} WHERE name = ?", vals)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_credential(source_name: str, env_var: str) -> str:
    """Return DB credential override if present, else os.getenv(env_var, '')."""
    row = _get_row(source_name)
    if row and row.get("credentials_json"):
        try:
            overrides = json.loads(row["credentials_json"])
            if env_var in overrides and overrides[env_var]:
                return overrides[env_var]
        except Exception:
            pass
    return os.getenv(env_var, "")


def is_available(source_name: str) -> bool:
    """Return True if the source is enabled and the circuit breaker is not open."""
    row = _get_row(source_name)
    if row is None:
        return True  # unknown source → don't block
    if not row["enabled"]:
        return False
    circuit_until = row.get("circuit_open_until") or 0.0
    return time.time() >= circuit_until


def get_weight(source_name: str) -> float:
    """Return the DB-configured weight for this source, or the declared default."""
    row = _get_row(source_name)
    if row:
        return float(row["weight"])
    decl = SOURCES_DECL.get(source_name)
    return decl.default_weight if decl else 1.0


def mark_success(source_name: str) -> None:
    _set_fields(
        source_name,
        last_success_at=time.time(),
        failure_streak=0,
        circuit_open_until=None,
        last_error=None,
    )


def mark_failure(source_name: str, err: str = "") -> None:
    row = _get_row(source_name)
    if row is None:
        return
    streak = (row.get("failure_streak") or 0) + 1
    decl = SOURCES_DECL.get(source_name)
    threshold = decl.default_circuit_threshold if decl else 3
    backoff = decl.default_backoff_secs if decl else 120.0
    now = time.time()
    circuit_until = row.get("circuit_open_until") or 0.0
    if streak >= threshold and now >= circuit_until:
        circuit_until = now + backoff
        logger.info(
            "%s circuit-breaker tripped after %d failures — unavailable for %.0fs",
            source_name, streak, backoff,
        )
    _set_fields(
        source_name,
        failure_streak=streak,
        last_error_at=now,
        last_error=str(err)[:500],
        circuit_open_until=circuit_until if circuit_until > now else None,
    )


def reset_circuit(source_name: str) -> None:
    _set_fields(
        source_name,
        failure_streak=0,
        circuit_open_until=None,
        last_error=None,
    )


def list_sources_for_graph(graph_id: int) -> list[dict]:
    """Return sources that are assigned to this graph and not globally disabled."""
    from backend.db import get_db
    conn = get_db()
    rows = conn.execute(
        "SELECT source_name, weight_override FROM graph_sources WHERE graph_id=?", (graph_id,)
    ).fetchall()
    conn.close()
    graph_map = {r["source_name"]: r["weight_override"] for r in rows}
    all_srcs = list_sources()
    result = []
    now = time.time()
    for s in all_srcs:
        if s["name"] not in graph_map:
            continue
        if not s.get("enabled", True):
            continue
        if s.get("circuit_open"):
            continue
        s = dict(s)
        wo = graph_map[s["name"]]
        if wo is not None:
            s["weight"] = wo
        result.append(s)
    return result


def list_sources() -> list[dict]:
    from backend.db import get_db
    conn = get_db()
    rows = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
    conn.close()
    now = time.time()
    result = []
    for row in rows:
        d = dict(row)
        # Parse metadata_json for env_var info
        try:
            meta = json.loads(d.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        d["env_vars"] = meta.get("env_vars", [])
        # Check which env vars have values (never return the values themselves)
        d["credential_status"] = {
            var: bool(get_credential(d["name"], var))
            for var in d["env_vars"]
        }
        d.pop("credentials_json", None)   # never expose credential values
        d.pop("metadata_json", None)
        d["circuit_open"] = bool(
            d.get("circuit_open_until") and d["circuit_open_until"] > now
        )
        d["circuit_open_until_seconds"] = max(
            0, round((d.get("circuit_open_until") or 0) - now)
        )
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def with_source(source_name: str, default=None):
    """
    Decorator for async functions. Returns `default` (or []) early if the
    source is unavailable; calls mark_success / mark_failure around the call.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            if not is_available(source_name):
                return default if default is not None else []
            try:
                result = await fn(*args, **kwargs)
                # Only count as success if we actually got data
                if result:
                    mark_success(source_name)
                return result
            except Exception as exc:
                mark_failure(source_name, str(exc))
                return default if default is not None else []
        return wrapper
    return decorator
