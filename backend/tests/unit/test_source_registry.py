"""Unit tests for source_registry state machine."""
from __future__ import annotations

import json
import os
import sqlite3
import time

import pytest

from backend.services.source_registry import (
    SOURCES_DECL,
    get_credential,
    get_weight,
    is_available,
    list_sources,
    mark_failure,
    mark_success,
    reset_circuit,
    seed_sources_table,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _get_row(name: str) -> dict:
    from backend.db import get_db
    conn = get_db()
    row = conn.execute("SELECT * FROM sources WHERE name = ?", (name,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def _set_field(name: str, **kwargs) -> None:
    from backend.db import get_db
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [name]
    conn = get_db()
    conn.execute(f"UPDATE sources SET {sets} WHERE name = ?", vals)
    conn.commit()
    conn.close()


# ── seeding ───────────────────────────────────────────────────────────────────


def test_seed_populates_declared_sources(tmp_db):
    seed_sources_table()
    rows = {r["name"] for r in list_sources()}
    assert set(SOURCES_DECL.keys()) <= rows


def test_seed_is_idempotent(tmp_db):
    seed_sources_table()
    seed_sources_table()
    names = [r["name"] for r in list_sources()]
    assert len(names) == len(set(names))


# ── is_available ──────────────────────────────────────────────────────────────


def test_enabled_source_is_available(tmp_db):
    seed_sources_table()
    assert is_available("lastfm") is True


def test_disabled_source_is_not_available(tmp_db):
    seed_sources_table()
    _set_field("lastfm", enabled=0)
    assert is_available("lastfm") is False


def test_circuit_open_blocks_availability(tmp_db):
    seed_sources_table()
    future = time.time() + 3600
    _set_field("lastfm", circuit_open_until=future)
    assert is_available("lastfm") is False


def test_expired_circuit_restores_availability(tmp_db):
    seed_sources_table()
    past = time.time() - 1
    _set_field("lastfm", circuit_open_until=past)
    assert is_available("lastfm") is True


def test_unknown_source_is_available(tmp_db):
    # Undeclared sources shouldn't block execution.
    assert is_available("nonexistent_source_xyz") is True


# ── mark_success / mark_failure ───────────────────────────────────────────────


def test_mark_success_clears_streak(tmp_db):
    seed_sources_table()
    _set_field("deezer", failure_streak=5, circuit_open_until=time.time() + 60)
    mark_success("deezer")
    row = _get_row("deezer")
    assert row["failure_streak"] == 0
    assert (row.get("circuit_open_until") or 0) == 0 or row["circuit_open_until"] <= time.time()


def test_mark_failure_bumps_streak(tmp_db):
    seed_sources_table()
    mark_failure("deezer", "timeout")
    row = _get_row("deezer")
    assert row["failure_streak"] == 1


def test_mark_failure_trips_circuit_at_threshold(tmp_db):
    seed_sources_table()
    decl = SOURCES_DECL["deezer"]
    for _ in range(decl.default_circuit_threshold):
        mark_failure("deezer", "err")
    assert is_available("deezer") is False


def test_mark_failure_ytdlp_trips_on_first(tmp_db):
    seed_sources_table()
    # ytdlp has threshold=1
    mark_failure("ytdlp", "err")
    assert is_available("ytdlp") is False


def test_reset_circuit_restores_source(tmp_db):
    seed_sources_table()
    _set_field("lastfm", circuit_open_until=time.time() + 3600, failure_streak=10)
    reset_circuit("lastfm")
    assert is_available("lastfm") is True
    row = _get_row("lastfm")
    assert row["failure_streak"] == 0


# ── get_credential ────────────────────────────────────────────────────────────


def test_get_credential_reads_env(tmp_db, monkeypatch):
    seed_sources_table()
    monkeypatch.setenv("LASTFM_KEY", "env-key-123")
    val = get_credential("lastfm", "LASTFM_KEY")
    assert val == "env-key-123"


def test_get_credential_db_override_wins(tmp_db, monkeypatch):
    seed_sources_table()
    monkeypatch.setenv("LASTFM_KEY", "env-key")
    from backend.db import get_db
    conn = get_db()
    conn.execute(
        "UPDATE sources SET credentials_json = ? WHERE name = 'lastfm'",
        (json.dumps({"LASTFM_KEY": "db-override"}),),
    )
    conn.commit()
    conn.close()
    val = get_credential("lastfm", "LASTFM_KEY")
    assert val == "db-override"


def test_get_credential_missing_returns_empty(tmp_db, monkeypatch):
    seed_sources_table()
    monkeypatch.delenv("LASTFM_KEY", raising=False)
    val = get_credential("lastfm", "LASTFM_KEY")
    assert val == ""


# ── get_weight ────────────────────────────────────────────────────────────────


def test_get_weight_returns_declared_default(tmp_db):
    seed_sources_table()
    w = get_weight("spotify")
    assert w == SOURCES_DECL["spotify"].default_weight


def test_get_weight_db_override_respected(tmp_db):
    seed_sources_table()
    _set_field("spotify", weight=0.42)
    assert abs(get_weight("spotify") - 0.42) < 0.001


def test_get_weight_unknown_source_returns_one(tmp_db):
    assert get_weight("totally_unknown_xyz") == 1.0


# ── list_sources ──────────────────────────────────────────────────────────────


def test_list_sources_never_exposes_credentials(tmp_db):
    seed_sources_table()
    from backend.db import get_db
    conn = get_db()
    conn.execute(
        "UPDATE sources SET credentials_json = ? WHERE name = 'lastfm'",
        (json.dumps({"LASTFM_KEY": "secret"}),),
    )
    conn.commit()
    conn.close()
    sources = list_sources()
    lastfm = next(s for s in sources if s["name"] == "lastfm")
    assert "credentials_json" not in lastfm
    assert "secret" not in json.dumps(lastfm)
    assert lastfm["credential_status"]["LASTFM_KEY"] is True
