"""Sandboxed execution of custom scorer and filter modules.

Each module is a Python snippet compiled with RestrictedPython.
Scorers: define `score(candidates) -> dict[str, float]`
Filters: define `filter_items(items) -> list`

Allowed builtins: math, min/max/abs/round/sum/len/sorted/enumerate/
  zip/range/isinstance, basic type constructors.
Blocked: import, open, exec, eval, __import__, file I/O.
"""
from __future__ import annotations

import math
import time
from typing import Any

from RestrictedPython import compile_restricted, safe_globals, safe_builtins

# ---------------------------------------------------------------------------
# Sandbox globals
# ---------------------------------------------------------------------------

_ALLOWED_BUILTINS = dict(safe_builtins)
for _name in (
    "min", "max", "abs", "round", "sum", "len", "sorted", "reversed",
    "enumerate", "zip", "range", "isinstance", "int", "float", "str",
    "bool", "list", "dict", "set", "tuple", "any", "all",
):
    _ALLOWED_BUILTINS[_name] = eval(_name)  # noqa: S307

_SANDBOX_GLOBALS: dict[str, Any] = dict(safe_globals)
_SANDBOX_GLOBALS["__builtins__"] = _ALLOWED_BUILTINS
_SANDBOX_GLOBALS["math"] = math
_SANDBOX_GLOBALS["_getiter_"] = lambda ob: ob
_SANDBOX_GLOBALS["_write_"] = lambda ob: ob
_SANDBOX_GLOBALS["_getattr_"] = getattr
_SANDBOX_GLOBALS["_getitem_"] = lambda ob, key: ob[key]
_SANDBOX_GLOBALS["_inplacevar_"] = lambda op, x, y: (
    x + y if op == "+=" else
    x - y if op == "-=" else
    x * y if op == "*=" else
    (_ for _ in ()).throw(ValueError(f"unsupported op: {op}"))
)


def _make_globals() -> dict:
    return dict(_SANDBOX_GLOBALS)


# ---------------------------------------------------------------------------
# Compile / validate
# ---------------------------------------------------------------------------

def validate_module(code: str, module_type: str) -> list[str]:
    """Return a list of error strings (empty = ok)."""
    errors = []
    try:
        byte_code = compile_restricted(code, "<module>", "exec")
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]

    glb = _make_globals()
    try:
        exec(byte_code, glb)  # noqa: S102
    except Exception as e:
        return [f"RuntimeError at compile-time: {e}"]

    entry = "score" if module_type == "scorer" else "filter_items"
    if entry not in glb or not callable(glb[entry]):
        errors.append(f"Module must define a callable `{entry}(candidates)`")

    return errors


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_scorer(code: str, candidates: list[dict]) -> dict[str, float]:
    """Run a scorer module; returns {video_id: float}."""
    byte_code = compile_restricted(code, "<scorer>", "exec")
    glb = _make_globals()
    exec(byte_code, glb)  # noqa: S102
    result = glb["score"](candidates)
    if not isinstance(result, dict):
        raise TypeError(f"score() must return a dict, got {type(result).__name__}")
    return {str(k): float(v) for k, v in result.items()}


def run_filter(code: str, items: list[dict]) -> list[dict]:
    """Run a filter module; returns filtered/reordered list."""
    byte_code = compile_restricted(code, "<filter>", "exec")
    glb = _make_globals()
    exec(byte_code, glb)  # noqa: S102
    result = glb["filter_items"](items)
    if not isinstance(result, list):
        raise TypeError(f"filter_items() must return a list, got {type(result).__name__}")
    return result


# ---------------------------------------------------------------------------
# Recompute and store scores for a scorer module
# ---------------------------------------------------------------------------

def update_module_scores(module_id: int) -> int:
    """Run the scorer module against current feed candidates and store results."""
    from backend.db import get_db

    conn = get_db()
    row = conn.execute(
        "SELECT code, type, enabled FROM custom_modules WHERE id = ?", (module_id,)
    ).fetchone()
    if not row or row["type"] != "scorer" or not row["enabled"]:
        conn.close()
        return 0

    code = row["code"]

    # Fetch top candidates with PPR + cosine scores
    candidates_raw = conn.execute("""
        SELECT fr.video_id, fr.title, fr.author, fr.duration,
               p.score as ppr_score,
               cs.score as cosine_score
        FROM feed_recommendations fr
        LEFT JOIN ppr_scores p ON p.video_id = fr.video_id AND p.graph_id = 1
        LEFT JOIN cosine_scores cs ON cs.video_id = fr.video_id AND cs.graph_id = 1
        LEFT JOIN watch_history wh ON wh.video_id = fr.video_id
        WHERE wh.video_id IS NULL
        GROUP BY fr.video_id
        ORDER BY COALESCE(p.score, 0) DESC
        LIMIT 2000
    """).fetchall()
    conn.close()

    candidates = [
        {
            "video_id": r["video_id"],
            "title": r["title"],
            "author": r["author"],
            "duration": r["duration"],
            "score": r["ppr_score"] or 0.0,
            "ppr_score": r["ppr_score"] or 0.0,
            "cosine_score": r["cosine_score"] or 0.0,
        }
        for r in candidates_raw
    ]

    scores = run_scorer(code, candidates)
    if not scores:
        return 0

    now = time.time()
    conn = get_db()
    conn.execute("DELETE FROM custom_module_scores WHERE module_id = ?", (module_id,))
    conn.executemany(
        "INSERT INTO custom_module_scores (module_id, video_id, score, computed_at) VALUES (?, ?, ?, ?)",
        [(module_id, vid, score, now) for vid, score in scores.items()],
    )
    conn.commit()
    conn.close()
    return len(scores)
