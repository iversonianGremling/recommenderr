"""Shared fixtures for recommenderr tests.

Each test gets a fresh SQLite at /tmp/recommenderr_test_<uuid>.db via the
`tmp_db` fixture, with the schema applied. Background workers are disabled
via DISABLE_WORKERS=1.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

import pytest


SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


@pytest.fixture
def tmp_db(monkeypatch):
    path = f"/tmp/recommenderr_test_{uuid.uuid4().hex}.db"
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.setenv("DISABLE_WORKERS", "1")
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA_PATH.read_text())
        con.commit()
    finally:
        con.close()
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


@pytest.fixture
def app(tmp_db):
    """Return the FastAPI app with the ephemeral DB wired in."""
    from backend.main import app as _app

    return _app


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
