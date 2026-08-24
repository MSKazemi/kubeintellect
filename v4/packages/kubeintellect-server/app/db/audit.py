"""
Audit log — writes API request records to the request_log table.

This uses KubeIntellect's own Postgres (settings.POSTGRES_DSN), the same
database that stores LangGraph checkpoints and RCA outcomes.  It is NOT
Langfuse's Postgres (which is a separate StatefulSet in the monitoring
namespace).

The pool is initialised once at app startup (init_audit_pool) and closed
at shutdown (close_audit_pool).  log_request is fire-and-forget: failures
are logged as warnings and never propagate to the caller.

WHY THE POOL RETRIES, AND WHY THE OUTAGE IS REPORTED
----------------------------------------------------
A failed connect at startup is the *expected* case on Kubernetes: the API pod is routinely
scheduled before Postgres accepts connections. Leaving `_pool = None` after that one attempt
disabled the audit log for the entire life of the process — every subsequent request, including
every privileged one, was dropped in silence, and nothing anywhere said so. One WARNING at
startup is not a signal an operator can query later; `/healthz` still answered `"ok"`, and an
empty `request_log` is indistinguishable from a server that took no traffic.

So two things hold here now. The connect is retried lazily from the write path (at most once
every `_RETRY_INTERVAL_S`, off the request path — `log_request` is always called inside
`asyncio.create_task`), so a startup race heals itself instead of costing a restart. And the
state is *reportable*: `audit_status()` is the machine-readable answer, surfaced on `/healthz`
next to `leader`, and dropped rows are counted so the number is not merely "unknown".
"""
from __future__ import annotations

import time

import asyncpg

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None

# "starting" until init runs; "sqlite" when there is no Postgres to write to by configuration;
# "ready" with a live pool; "unavailable" when Postgres refused us and we are retrying.
_state: str = "starting"
_reason: str = ""
_dropped: int = 0
_last_attempt: float = 0.0

#: Seconds between reconnect attempts once the pool is down. Short enough that a pod scheduled
#: ahead of Postgres starts auditing within a rollout, long enough that a genuine outage does
#: not turn every request into a connect attempt.
_RETRY_INTERVAL_S = 30.0


def audit_status() -> dict:
    """Shape reported on ``/healthz``. An operator must be able to see that nothing is audited.

    ``enabled`` is the only field a probe needs; ``state``/``reason`` say *why*, and ``dropped``
    is the count of requests that went unrecorded since the process started — the number that
    turns "the table looks empty" into a fact rather than a guess.
    """
    return {
        "enabled": _state == "ready",
        "state": _state,
        "reason": _reason,
        "dropped": _dropped,
    }


async def _open_pool() -> None:
    """Try once to build the pool. Records the outcome in the module state; never raises."""
    global _pool, _state, _reason, _last_attempt
    _last_attempt = time.monotonic()
    try:
        _pool = await asyncpg.create_pool(
            settings.POSTGRES_DSN,
            min_size=1,
            max_size=3,
            command_timeout=5,
        )
        _state, _reason = "ready", ""
        logger.info("audit: pool ready")
    except Exception as exc:
        _pool = None
        _state, _reason = "unavailable", str(exc)
        logger.warning(
            f"audit: could not connect to Postgres — requests are NOT being audited; "
            f"retrying every {_RETRY_INTERVAL_S:.0f}s ({exc})"
        )


async def init_audit_pool() -> None:
    global _state, _reason
    if settings.USE_SQLITE:
        _state, _reason = "sqlite", "USE_SQLITE=true — no Postgres to write request_log to"
        logger.info("audit: SQLite mode — audit logging disabled")
        return
    await _open_pool()


async def close_audit_pool() -> None:
    global _pool, _state, _reason
    if _pool:
        await _pool.close()
        _pool = None
    _state, _reason = "starting", ""


def _drop(quiet: bool = False) -> None:
    """Count an unrecorded request, and break the silence at a cadence that cannot flood.

    ``quiet`` is for the write-failure path, which has already logged its own reason for this
    exact row — the count still has to move, or `dropped` understates the loss.
    """
    global _dropped
    _dropped += 1
    if quiet:
        return
    if _dropped == 1 or _dropped % 100 == 0:
        logger.warning(
            f"audit: {_dropped} request(s) not recorded in request_log — {_state}: {_reason}"
        )


async def log_request(
    *,
    request_id: str,
    session_id: str,
    user_id: str,
    user_role: str,
    path: str,
    method: str,
    status_code: int,
    duration_ms: float,
) -> None:
    """Insert one row into request_log.  Never raises — failures are warnings only."""
    if _pool is None:
        # SQLite mode is a configuration, not an outage: it is already reported by audit_status()
        # and nothing is meant to be written, so it neither retries nor counts as a drop.
        if _state == "sqlite":
            return
        if time.monotonic() - _last_attempt >= _RETRY_INTERVAL_S:
            await _open_pool()
        if _pool is None:
            _drop()
            return
    try:
        await _pool.execute(
            """
            INSERT INTO request_log
              (request_id, session_id, user_id, user_role,
               path, method, status_code, duration_ms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            request_id, session_id, user_id, user_role,
            path, method, status_code, duration_ms,
        )
    except Exception as exc:
        msg = str(exc)
        if "request_log" in msg and "does not exist" in msg:
            logger.warning("audit: 'request_log' table missing — run: kubeintellect db-init")
        else:
            logger.warning(f"audit: failed to write request_log row: {exc}")
        # A row that was accepted by the pool and then rejected by the database is just as
        # unrecorded as one that never had a pool. Count it, so `dropped` is the true total.
        _drop(quiet=True)
