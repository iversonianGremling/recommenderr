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


def _apply_schema(con: sqlite3.Connection, sql: str) -> None:
    """Apply a SQL schema file statement-by-statement inside one transaction.

    Uses a state machine to parse statements (respecting -- comments and quoted
    strings) so we can call individual execute() calls rather than executescript(),
    which hangs on some SQLite builds when isolation_level is not None.

    All statements are wrapped in a single BEGIN/COMMIT so the schema lands in
    one WAL sync instead of one per DDL statement (which is ~13 seconds on the
    test host).  The connection must be opened with isolation_level=None.
    """
    stmts: list[str] = []
    stmt_chars: list[str] = []
    in_line_comment = False
    in_single_quote = False
    in_double_quote = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if not in_single_quote and not in_double_quote:
            if not in_line_comment and ch == '-' and sql[i:i+2] == '--':
                in_line_comment = True
                i += 2
                continue
            if in_line_comment:
                if ch == '\n':
                    in_line_comment = False
                i += 1
                continue
        if not in_line_comment:
            if ch == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
            elif ch == '"' and not in_single_quote:
                in_double_quote = not in_double_quote
        if ch == ';' and not in_line_comment and not in_single_quote and not in_double_quote:
            stmt = ''.join(stmt_chars).strip()
            if stmt:
                stmts.append(stmt)
            stmt_chars = []
            i += 1
            continue
        stmt_chars.append(ch)
        i += 1
    trailing = ''.join(stmt_chars).strip()
    if trailing:
        stmts.append(trailing)

    con.execute("BEGIN")
    try:
        for stmt in stmts:
            con.execute(stmt)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


@pytest.fixture
def tmp_db(monkeypatch):
    path = f"/tmp/recommenderr_test_{uuid.uuid4().hex}.db"
    monkeypatch.setenv("DB_PATH", path)
    monkeypatch.setenv("DISABLE_WORKERS", "1")
    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        _apply_schema(con, SCHEMA_PATH.read_text())
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
