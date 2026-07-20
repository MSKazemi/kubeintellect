"""
Audit log — writes API request records to the request_log table.

This uses KubeIntellect's own Postgres (settings.POSTGRES_DSN), the same
database that stores LangGraph checkpoints and RCA outcomes.  It is NOT
Langfuse's Postgres (which is a separate StatefulSet in the monitoring
namespace).

The pool is initialised once at app startup (init_audit_pool) and closed
at shutdown (close_audit_pool).  log_request is fire-and-forget: failures
are logged as warnings and never propagate to the caller.
"""
from __future__ import annotations

import asyncpg

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def init_audit_pool() -> None:
    global _pool
    if settings.USE_SQLITE:
        logger.info("audit: SQLite mode — audit logging disabled")
        return
    try:
        _pool = await asyncpg.create_pool(
            settings.POSTGRES_DSN,
            min_size=1,
            max_size=3,
            command_timeout=5,
        )
        logger.info("audit: pool ready")
    except Exception as exc:
        logger.warning(f"audit: could not connect to Postgres — audit logging disabled ({exc})")
        _pool = None


async def close_audit_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


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
