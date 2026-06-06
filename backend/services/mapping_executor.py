"""Interpreter for converter mapping_code JSON.

mapping_code schema:
  { "operations": [ <op>, ... ] }

Operation types:
  passthrough  – copy field as-is, optionally rename
  rename       – alias for passthrough with explicit from/to
  merge        – first non-null value from a list of input fields
  transform    – apply a named built-in function to one or more inputs
  delete       – remove a field from the output (no-op if already absent)

Built-in transform via values:
  extract_thumbnail  – picks url from Invidious videoThumbnails array or returns string as-is
  first_non_null     – first non-null value (default for merge)
  join_comma         – join all non-null values with ", "
  lowercase          – lowercase the first non-null value
  position_weight    – 1/(1+position) weight for recommendation position
  normalize_duration – coerce to int seconds
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("mapping_executor")


def get_mapping_for_source(source_name: str) -> str:
    """Return the mapping_code for the first enabled converter that includes source_name."""
    try:
        from backend.db import get_db
        conn = get_db()
        rows = conn.execute(
            "SELECT mapping_code, sources FROM converters WHERE enabled=1 ORDER BY id"
        ).fetchall()
        conn.close()
        for row in rows:
            try:
                sources = json.loads(row["sources"] or "[]")
            except Exception:
                sources = []
            if source_name in sources:
                return row["mapping_code"] or "{}"
    except Exception as exc:
        logger.warning("Could not load mapping for source %s: %s", source_name, exc)
    return "{}"


def apply_mapping(record: dict, mapping_code: str) -> dict:
    """Apply field mapping operations to a raw record.

    Returns a new dict with standardised field names.
    If mapping_code is empty or has no operations, returns the record unchanged
    (passthrough — backwards-compatible fallback).
    """
    try:
        spec = json.loads(mapping_code or "{}")
    except Exception:
        logger.warning("Invalid mapping_code JSON, falling back to passthrough")
        return dict(record)

    ops = spec.get("operations", [])
    if not ops:
        return dict(record)

    result: dict[str, Any] = {}

    for op in ops:
        op_type = op.get("type", "")

        if op_type in ("passthrough", "rename"):
            src = op.get("from")
            dst = op.get("to", src)
            if src and dst:
                val = record.get(src)
                if val is not None:
                    result[dst] = val

        elif op_type == "merge":
            srcs = op.get("from", [])
            dst = op.get("to")
            via = op.get("via", "first_non_null")
            if dst:
                result[dst] = _transform(via, [record.get(s) for s in srcs], record)

        elif op_type == "transform":
            srcs = op.get("from")
            if isinstance(srcs, str):
                srcs = [srcs]
            dst = op.get("to")
            via = op.get("via", "")
            if dst:
                result[dst] = _transform(via, [record.get(s) for s in (srcs or [])], record)

        elif op_type == "delete":
            result.pop(op.get("field", ""), None)

    return result


def _transform(via: str, vals: list[Any], _record: dict) -> Any:
    if via in ("first_non_null", "merge", ""):
        return next((v for v in vals if v is not None), None)

    if via == "extract_thumbnail":
        raw = vals[0] if vals else None
        if isinstance(raw, list):
            return raw[0].get("url", "") if raw else ""
        if isinstance(raw, str):
            return raw
        return ""

    if via == "position_weight":
        pos = vals[0]
        return round(1.0 / (1 + (pos or 0)), 3)

    if via == "join_comma":
        return ", ".join(str(v) for v in vals if v is not None)

    if via == "lowercase":
        return next((str(v).lower() for v in vals if v is not None), None)

    if via == "normalize_duration":
        for v in vals:
            if isinstance(v, (int, float)):
                return int(v)
        return None

    return next((v for v in vals if v is not None), None)
