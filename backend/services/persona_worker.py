"""Persona worker — background task that keeps persona scores fresh.

Cloned from category_recs_worker. One worker is registered (see main.py lifespan).
SQLite write contention is the ceiling — don't add a second without benchmarks.
"""
from __future__ import annotations

import asyncio
import logging
import time

from backend.db import get_db

logger = logging.getLogger(__name__)

PERSONA_REFRESH_HOURS = 6
WORKER_SLEEP = 30.0


def _ensure_jobs_for_all_personas() -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO persona_jobs (persona_id, status, next_run_at) "
        "SELECT id, 'pending', ? FROM personas",
        (time.time(),),
    )
    cutoff = time.time() - PERSONA_REFRESH_HOURS * 3600
    conn.execute(
        "UPDATE persona_jobs SET status='pending', next_run_at=? "
        "WHERE status='done' AND last_run_at < ?",
        (time.time(), cutoff),
    )
    conn.commit()
    conn.close()


def _pick_next_job() -> int | None:
    """Atomically claim the next eligible persona job. Returns persona_id or None."""
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT pj.persona_id, p.version FROM persona_jobs pj "
            "JOIN personas p ON p.id = pj.persona_id "
            "WHERE pj.status='pending' AND (pj.next_run_at IS NULL OR pj.next_run_at <= ?) "
            "ORDER BY COALESCE(pj.last_run_at, 0) ASC LIMIT 1",
            (time.time(),),
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        pid = row["persona_id"]
        version = row["version"]
        conn.execute(
            "UPDATE persona_jobs SET status='running', claimed_version=? WHERE persona_id=?",
            (version, pid),
        )
        conn.execute("COMMIT")
        return pid
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _finish_job(persona_id: int, error: str | None = None) -> None:
    conn = get_db()
    next_run = time.time() + PERSONA_REFRESH_HOURS * 3600
    conn.execute(
        "UPDATE persona_jobs SET status=?, last_run_at=?, next_run_at=?, last_error=? "
        "WHERE persona_id=?",
        ("done" if error is None else "error", time.time(), next_run, error, persona_id),
    )
    conn.commit()
    conn.close()


async def persona_worker() -> None:
    logger.info("Persona worker started")
    await asyncio.sleep(10)  # let the app fully start first

    last_seed = 0.0
    while True:
        try:
            now = time.time()
            if now - last_seed > 60:
                _ensure_jobs_for_all_personas()
                last_seed = now

            pid = _pick_next_job()
            if pid is None:
                await asyncio.sleep(WORKER_SLEEP)
                continue

            t0 = time.time()
            try:
                from backend.services.persona_engine import compute_persona_ppr
                loop = asyncio.get_running_loop()
                count = await loop.run_in_executor(None, compute_persona_ppr, pid)
                _finish_job(pid, None)
                logger.info("Persona %d → %d scores in %.1fs", pid, count, time.time() - t0)
            except Exception as e:
                _finish_job(pid, str(e)[:200])
                logger.warning("Persona %d failed: %s", pid, e)

            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            logger.info("Persona worker stopped")
            raise
        except Exception as e:
            logger.error("Persona worker loop error: %s", e)
            await asyncio.sleep(5.0)
