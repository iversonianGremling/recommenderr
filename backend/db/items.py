"""DB helpers for the generic items / schemes abstraction."""
from __future__ import annotations

import json
import time
from typing import Any

from backend.db import get_db


# ---------------------------------------------------------------------------
# Schemes
# ---------------------------------------------------------------------------

def list_schemes() -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT name, display_name, description, fields_json, created_at FROM schemes ORDER BY name"
    ).fetchall()
    conn.close()
    return [
        {
            "name": r["name"],
            "display_name": r["display_name"],
            "description": r["description"],
            "fields": json.loads(r["fields_json"] or "[]"),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def register_scheme(
    name: str,
    display_name: str,
    fields: list[dict],
    description: str = "",
) -> None:
    conn = get_db()
    conn.execute(
        """
        INSERT INTO schemes (name, display_name, description, fields_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            display_name = excluded.display_name,
            description  = excluded.description,
            fields_json  = excluded.fields_json
        """,
        (name, display_name, description, json.dumps(fields), time.time()),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def upsert_item(scheme: str, external_id: str, metadata: dict[str, Any]) -> int:
    """Insert or update an item; return its integer id."""
    conn = get_db()
    conn.execute(
        """
        INSERT INTO items (scheme, external_id, metadata_json, added_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(scheme, external_id) DO UPDATE SET
            metadata_json = excluded.metadata_json
        """,
        (scheme, external_id, json.dumps(metadata), time.time()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM items WHERE scheme = ? AND external_id = ?",
        (scheme, external_id),
    ).fetchone()
    conn.close()
    return row["id"]


def get_item(item_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute(
        """
        SELECT i.id, i.scheme, i.external_id, i.metadata_json, i.added_at,
               s.display_name AS scheme_display_name, s.fields_json
        FROM items i
        JOIN schemes s ON s.name = i.scheme
        WHERE i.id = ?
        """,
        (item_id,),
    ).fetchone()
    aliases = conn.execute(
        "SELECT alias_scheme, alias_external_id FROM item_aliases WHERE item_id = ?",
        (item_id,),
    ).fetchall()
    conn.close()
    if row is None:
        return None
    return {
        "id": row["id"],
        "scheme": row["scheme"],
        "scheme_display_name": row["scheme_display_name"],
        "external_id": row["external_id"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "fields": json.loads(row["fields_json"] or "[]"),
        "added_at": row["added_at"],
        "aliases": [
            {"scheme": a["alias_scheme"], "external_id": a["alias_external_id"]}
            for a in aliases
        ],
    }


def search_items(
    scheme: str | None = None,
    q: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    conn = get_db()
    clauses: list[str] = []
    params: list[Any] = []

    if scheme:
        clauses.append("i.scheme = ?")
        params.append(scheme)
    if q:
        clauses.append("i.metadata_json LIKE ?")
        params.append(f"%{q}%")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params += [limit, offset]

    rows = conn.execute(
        f"""
        SELECT i.id, i.scheme, i.external_id, i.metadata_json, i.added_at,
               s.display_name AS scheme_display_name
        FROM items i
        JOIN schemes s ON s.name = i.scheme
        {where}
        ORDER BY i.added_at DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "scheme": r["scheme"],
            "scheme_display_name": r["scheme_display_name"],
            "external_id": r["external_id"],
            "metadata": json.loads(r["metadata_json"] or "{}"),
            "added_at": r["added_at"],
        }
        for r in rows
    ]


def resolve_alias(alias_scheme: str, alias_external_id: str) -> int | None:
    """Return item_id for an alias key, or None if not found."""
    conn = get_db()
    row = conn.execute(
        "SELECT item_id FROM item_aliases WHERE alias_scheme = ? AND alias_external_id = ?",
        (alias_scheme, alias_external_id),
    ).fetchone()
    conn.close()
    return row["item_id"] if row else None


def add_alias(item_id: int, alias_scheme: str, alias_external_id: str) -> None:
    conn = get_db()
    conn.execute(
        """
        INSERT OR IGNORE INTO item_aliases (item_id, alias_scheme, alias_external_id)
        VALUES (?, ?, ?)
        """,
        (item_id, alias_scheme, alias_external_id),
    )
    conn.commit()
    conn.close()
