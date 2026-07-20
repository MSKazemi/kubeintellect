# app/services/reflection_memory_service.py
"""
Persistent reflection memory service.

Stores per-user routing-mistake notes in PostgreSQL so that the supervisor
can avoid repeating the same errors across sessions.  Entries are written
manually (admin endpoint) or automatically when the HITL flow detects a
repeated routing failure.

Table DDL (created automatically on first call to setup_schema):

    CREATE TABLE IF NOT EXISTS reflection_memories (
        id        SERIAL PRIMARY KEY,
        user_id   TEXT        NOT NULL,
        memory    TEXT        NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.utils.logger_config import setup_logging

if TYPE_CHECKING:
    pass  # psycopg_pool.AsyncConnectionPool imported lazily to avoid hard dep at import time

logger = setup_logging(app_name="kubeintellect")

_CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS reflection_memories ("
    "id SERIAL PRIMARY KEY, "
    "user_id TEXT NOT NULL, "
    "memory TEXT NOT NULL, "
    "created_at TIMESTAMPTZ DEFAULT NOW()"
    ")"
)
_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_reflection_memories_user_id "
    "ON reflection_memories (user_id)"
)


async def setup_schema(pool) -> None:
    """Create the reflection_memories table if it doesn't already exist."""
    try:
        async with pool.connection() as conn:
            await conn.execute(_CREATE_TABLE_SQL)
            await conn.execute(_CREATE_INDEX_SQL)
        logger.info("reflection_memories schema verified/created.")
    except Exception as exc:
        logger.warning("Could not create reflection_memories schema: %s", exc)


async def load_reflection_memories(user_id: str, pool, limit: int = 10) -> list[str]:
    """
    Return the most recent *limit* reflection memories for *user_id*.

    Returns an empty list on any error so the caller can proceed without
    reflection context rather than failing the whole workflow.
    """
    if not user_id or not pool:
        return []
    try:
        async with pool.connection() as conn:
            rows = await conn.execute(
                "SELECT memory FROM reflection_memories "
                "WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
            return [row[0] for row in await rows.fetchall()]
    except Exception as exc:
        logger.debug("Could not load reflection memories for user %s: %s", user_id, exc)
        return []


async def save_reflection_memory(user_id: str, memory: str, pool) -> bool:
    """
    Persist a new reflection memory entry for *user_id*.

    Returns True on success, False on failure.  Callers should treat
    failures as non-fatal (reflection memory is advisory, not critical).
    """
    if not user_id or not memory or not pool:
        return False
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO reflection_memories (user_id, memory) VALUES (%s, %s)",
                (user_id, memory),
            )
        logger.debug("Saved reflection memory for user %s: %.80s", user_id, memory)
        return True
    except Exception as exc:
        logger.warning("Could not save reflection memory for user %s: %s", user_id, exc)
        return False


async def delete_reflection_memories(user_id: str, pool) -> int:
    """
    Delete all reflection memories for *user_id*.  Returns number of rows deleted.
    Useful for admin/GDPR erasure requests.
    """
    if not user_id or not pool:
        return 0
    try:
        async with pool.connection() as conn:
            result = await conn.execute(
                "DELETE FROM reflection_memories WHERE user_id = %s",
                (user_id,),
            )
            count = result.rowcount if hasattr(result, "rowcount") else 0
        logger.info("Deleted %d reflection memories for user %s", count, user_id)
        return count
    except Exception as exc:
        logger.warning("Could not delete reflection memories for user %s: %s", user_id, exc)
        return 0
