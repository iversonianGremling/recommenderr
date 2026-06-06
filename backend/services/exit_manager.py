"""
exit_manager — single coordination point for the YouTube fetch pipeline's
outbound egress.

Responsibilities
----------------
* Pin yt-dlp egress to the vpn-gateway's rotating Mullvad SOCKS proxy
  (``microsocks`` on ``10.10.10.1:1080``).  Rotation is transparent to the
  warm yt-dlp workers because the SOCKS address never changes — only the
  upstream Mullvad exit does.
* Rotate the Mullvad exit IP on bot-detection by POSTing to the gateway's
  ``/api/blocked`` endpoint (picks a fresh, *different* DAITA-aware relay).
  Rotations are serialized and rate-limited so a burst of concurrent
  bot-blocks coalesces into a single rotation instead of a stampede.
* Track an "interactive in-flight" counter so background crawlers can yield to
  user-driven playback (temporal IP isolation without a second tunnel).
* Expose live state for the fetch-health bus.

This module replaces the old ``_reconnect_vpn`` logic in ``ytdlp_service`` whose
120 s cooldown vs ~2 s retry cadence meant it almost never actually rotated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from typing import Optional

import httpx

logger = logging.getLogger("exit_manager")

# SOCKS5 proxy on the vpn-gateway bound to the live Mullvad exit (microsocks).
# ``or`` so an empty env value (the old "disabled" sentinel) falls back to the proxy.
YT_PROXY = os.getenv("YTDLP_PROXY") or "socks5://10.10.10.1:1080"
# Tor SOCKS — reserved for non-YouTube scraping (Tor exits are blocked by YT).
TOR_PROXY = os.getenv("TOR_PROXY") or "socks5://10.10.10.29:9050"

# Gateway control API (mullvad-ui server.mjs on CT103).
GATEWAY_URL = (os.getenv("MULLVAD_UI_URL") or "http://10.10.10.1").rstrip("/")
_BLOCKED_URL = GATEWAY_URL + "/api/blocked"
_STATUS_URL = GATEWAY_URL + "/api/status"

# Minimum seconds between real rotations — bursts within this window coalesce.
ROTATE_COOLDOWN = float(os.getenv("EXIT_ROTATE_COOLDOWN", "15"))
# Seconds to let a freshly-rotated tunnel settle before retrying an extraction.
ROTATE_SETTLE = float(os.getenv("EXIT_ROTATE_SETTLE", "2.5"))

_rotate_lock = asyncio.Lock()
_last_rotate_ts: float = 0.0


def _clean(v: Optional[str]) -> Optional[str]:
    """Treat the gateway's "N/A" placeholder (and blanks) as missing."""
    if not v or v.strip().upper() == "N/A":
        return None
    return v.strip()

# ── Live state (read by the fetch-health bus) ───────────────────────────────
current_ip: Optional[str] = None
current_relay: Optional[str] = None
rotations_total: int = 0
_bot_block_times: deque = deque(maxlen=200)

# Circuit breaker: when consecutive YouTube failures (bot walls / SOCKS refusals)
# pile up, the whole Mullvad exit pool is flagged — rotating just adds slow
# reconnects and more blocks without escaping, and disrupts in-flight playback.
# Trip the breaker to stop per-request rotation amplification; allow a single
# probe rotation per interval to detect pool recovery. Any success resets it.
BREAKER_THRESHOLD = int(os.getenv("EXIT_BREAKER_THRESHOLD", "3"))
BREAKER_PROBE_INTERVAL = float(os.getenv("EXIT_BREAKER_PROBE", "300"))
_consecutive_yt_fails: int = 0
_breaker_tripped_since: float = 0.0
_last_probe_rotate_ts: float = 0.0


def current_proxy() -> str:
    return YT_PROXY


def proxy_opts() -> dict:
    """yt-dlp proxy option dict for the YouTube egress class."""
    p = current_proxy()
    return {"proxy": p} if p else {}


# ── Per-relay reliability stats ──────────────────────────────────────────────
# Attributes extraction outcomes to whichever Mullvad relay was active, so we
# can see (and later prefer) relays that don't get bot-blocked. Persisted so the
# history survives restarts.
STATS_PATH = os.getenv("EXIT_STATS_PATH") or "/opt/recommenderr/data/exit_stats.json"
_RELAY_RE = re.compile(r"[a-z]{2}-[a-z]{3}-wg-\d+")
_relay_stats: dict[str, dict] = {}


def _relay_key(relay: Optional[str]) -> Optional[str]:
    """Extract the bare relay code (e.g. 'de-ber-wg-007') from a status string."""
    if not relay:
        return None
    m = _RELAY_RE.search(relay)
    return m.group(0) if m else (relay.strip()[:40] or None)


def _load_stats() -> None:
    global _relay_stats
    try:
        with open(STATS_PATH) as f:
            _relay_stats = json.load(f)
    except Exception:  # noqa: BLE001
        _relay_stats = {}


def _save_stats() -> None:
    try:
        tmp = STATS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_relay_stats, f)
        os.replace(tmp, STATS_PATH)
    except Exception:  # noqa: BLE001
        pass


def _record_relay(ok: bool) -> None:
    key = _relay_key(current_relay)
    if not key:
        return
    st = _relay_stats.setdefault(key, {"successes": 0, "bot_blocks": 0, "last_ts": 0.0})
    st["successes" if ok else "bot_blocks"] += 1
    st["last_ts"] = time.time()
    _save_stats()


def relay_stats_view() -> list[dict]:
    """Relay stats sorted best-first (highest success rate, then most successes)."""
    out = []
    for k, v in _relay_stats.items():
        s = int(v.get("successes", 0))
        b = int(v.get("bot_blocks", 0))
        total = s + b
        out.append({
            "relay": k,
            "successes": s,
            "bot_blocks": b,
            "conn_fails": int(v.get("conn_fails", 0)),
            "success_rate": round(s / total, 3) if total else None,
            "last_ts": v.get("last_ts", 0.0),
        })
    out.sort(key=lambda r: (-(r["success_rate"] if r["success_rate"] is not None else 0.0), -r["successes"]))
    return out


_load_stats()


def note_bot_block() -> None:
    _bot_block_times.append(time.time())
    _record_relay(False)
    _note_yt_fail()


def note_success() -> None:
    global _consecutive_yt_fails, _breaker_tripped_since
    if _breaker_tripped_since:
        logger.info("[exit] circuit breaker RESET after success")
    _consecutive_yt_fails = 0
    _breaker_tripped_since = 0.0
    _record_relay(True)


def note_conn_fail() -> None:
    """Record a connection/SOCKS-level failure (the exit IP was refused upstream,
    e.g. YouTube throttling). Tracked separately from HTTP bot blocks; used to
    rotate off a flagged exit instead of hammering the same blocked IP."""
    _note_yt_fail()
    key = _relay_key(current_relay)
    if not key:
        return
    st = _relay_stats.setdefault(key, {"successes": 0, "bot_blocks": 0, "last_ts": 0.0})
    st["conn_fails"] = int(st.get("conn_fails", 0)) + 1
    st["last_ts"] = time.time()
    _save_stats()


def _note_yt_fail() -> None:
    global _consecutive_yt_fails, _breaker_tripped_since
    _consecutive_yt_fails += 1
    if _consecutive_yt_fails >= BREAKER_THRESHOLD and not _breaker_tripped_since:
        _breaker_tripped_since = time.time()
        logger.warning("[exit] circuit breaker TRIPPED — %d consecutive YouTube failures; "
                       "exit pool looks flagged, suppressing rotation (probe every %.0fs)",
                       _consecutive_yt_fails, BREAKER_PROBE_INTERVAL)


def breaker_tripped() -> bool:
    return _consecutive_yt_fails >= BREAKER_THRESHOLD


def should_rotate_now() -> bool:
    """Worth rotating? Always when the breaker is closed. When tripped (pool
    flagged), only once per probe interval — rotating to another flagged IP just
    adds slow reconnects and disrupts playback."""
    global _last_probe_rotate_ts
    if not breaker_tripped():
        return True
    now = time.time()
    if now - _last_probe_rotate_ts >= BREAKER_PROBE_INTERVAL:
        _last_probe_rotate_ts = now
        return True
    return False


def recent_bot_blocks(window: float = 600.0) -> int:
    now = time.time()
    return sum(1 for t in _bot_block_times if now - t < window)


def is_rotating() -> bool:
    return _rotate_lock.locked()


async def rotate(reporter: str = "ytdlp") -> dict:
    """Rotate to a fresh Mullvad exit IP via the gateway, coalescing bursts.

    Returns ``{"changed": bool, "new_ip": str|None, "skipped": bool}``.
    A ``skipped`` result means another rotation happened within the cooldown
    window — the caller already has a fresh IP and should just retry.
    """
    global _last_rotate_ts, current_ip, current_relay, rotations_total
    async with _rotate_lock:
        now = time.time()
        if now - _last_rotate_ts < ROTATE_COOLDOWN:
            logger.info("[exit] rotate coalesced — last rotation %.0fs ago (<%.0fs)",
                        now - _last_rotate_ts, ROTATE_COOLDOWN)
            return {"changed": False, "new_ip": current_ip, "skipped": True}
        _last_rotate_ts = now
        try:
            async with httpx.AsyncClient(timeout=70.0) as client:
                resp = await client.post(_BLOCKED_URL, json={"reporter": reporter})
                data = resp.json()
            changed = data.get("status") == "reconnected"
            current_ip = _clean(data.get("new_ip")) or current_ip
            current_relay = _clean(data.get("new_relay")) or current_relay
            if changed:
                rotations_total += 1
            logger.info("[exit] rotate via gateway: status=%s relay=%s ip=%s changed=%s",
                        data.get("status"), current_relay, current_ip, changed)
            if changed and ROTATE_SETTLE > 0:
                await asyncio.sleep(ROTATE_SETTLE)
            return {"changed": changed, "new_ip": current_ip, "skipped": False}
        except Exception as e:  # noqa: BLE001 — best-effort; never block extraction
            logger.warning("[exit] rotate failed: %s", e)
            return {"changed": False, "new_ip": current_ip, "skipped": False, "error": str(e)}


async def refresh_status() -> dict:
    """Poll the gateway for the current relay/IP (used by the health heartbeat)."""
    global current_ip, current_relay
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_STATUS_URL)
            data = resp.json()
        current_ip = _clean(data.get("ip")) or current_ip
        current_relay = _clean(data.get("relay")) or current_relay
        return data
    except Exception as e:  # noqa: BLE001
        logger.debug("[exit] status poll failed: %s", e)
        return {}


# ── Interactive gate ─────────────────────────────────────────────────────────
# Lets background crawlers yield to user-driven playback so bulk crawling does
# not poison the exit IP reputation interactive playback depends on.
_interactive_count = 0
_idle_event = asyncio.Event()
_idle_event.set()


class _Interactive:
    async def __aenter__(self):
        global _interactive_count
        _interactive_count += 1
        _idle_event.clear()
        return self

    async def __aexit__(self, *exc):
        global _interactive_count
        _interactive_count = max(0, _interactive_count - 1)
        if _interactive_count == 0:
            _idle_event.set()


def interactive() -> "_Interactive":
    """Async context manager marking a user-driven fetch as in-flight."""
    return _Interactive()


def interactive_active() -> bool:
    return _interactive_count > 0


async def wait_until_idle(timeout: float = 120.0) -> None:
    """Block until no interactive request is in flight (crawler backpressure)."""
    try:
        await asyncio.wait_for(_idle_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


def state() -> dict:
    """Snapshot for the fetch-health bus."""
    return {
        "proxy": current_proxy(),
        "current_ip": current_ip,
        "current_relay": current_relay,
        "rotations_total": rotations_total,
        "recent_bot_blocks": recent_bot_blocks(),
        "interactive_active": interactive_active(),
        "rotating": is_rotating(),
        "last_rotate_ts": _last_rotate_ts,
        "breaker_tripped": breaker_tripped(),
        "consecutive_yt_fails": _consecutive_yt_fails,
        "relay_stats": relay_stats_view(),
    }
