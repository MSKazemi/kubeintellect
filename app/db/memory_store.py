"""
Async Postgres memory store — loads pinned context for the coordinator.

Reads (in priority order, total ≤500 tokens):
  1. user_prefs        — persistent user preferences
  2. failure_hint      — auto-seeded failure patterns (high-confidence recurring)
  3. session_notes     — notes from current session
  4. past_rca          — last 3 RCA summaries for this user
  5. runbook           — matching runbook snippets

All reads are non-blocking; missing tables return empty strings gracefully.
"""
from __future__ import annotations

import asyncpg

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_CONTEXT_CHARS = 1_800   # ~500 tokens at ~3.6 chars/token


async def _get_conn() -> asyncpg.Connection:
    return await asyncpg.connect(settings.POSTGRES_DSN)


async def load_memory_context(user_id: str, session_id: str) -> str:
    """Return a pinned context string ≤500 tokens for the coordinator SystemMessage."""
    if settings.USE_SQLITE:
        return ""   # memory store requires PostgreSQL; silently skip in SQLite mode

    parts: list[str] = []

    try:
        conn = await _get_conn()
        try:
            parts += await _load_user_prefs(conn, user_id)
            parts += await _load_failure_hints(conn)
            parts += await _load_session_notes(conn, session_id)
            parts += await _load_past_rca(conn, user_id)
        finally:
            await conn.close()
    except Exception as exc:
        logger.warning(f"memory_store: could not load context — {exc}")
        return ""

    combined = "\n\n".join(p for p in parts if p.strip())
    if len(combined) > _MAX_CONTEXT_CHARS:
        combined = combined[:_MAX_CONTEXT_CHARS] + "\n... [context truncated]"
    return combined


async def _load_user_prefs(conn: asyncpg.Connection, user_id: str) -> list[str]:
    try:
        rows = await conn.fetch(
            "SELECT key, value FROM user_prefs WHERE user_id = $1 ORDER BY key",
            user_id,
        )
        if not rows:
            return []
        lines = "\n".join(f"  {r['key']}: {r['value']}" for r in rows)
        return [f"## User Preferences\n{lines}"]
    except Exception:
        return []


async def _load_failure_hints(conn: asyncpg.Connection) -> list[str]:
    """Auto-seeded failure patterns with confidence ≥0.9 and occurrence_count ≥2."""
    try:
        rows = await conn.fetch(
            """
            SELECT pattern_name, description, recommended_fix
            FROM failure_patterns
            WHERE confidence >= 0.9 AND occurrence_count >= 2
            ORDER BY occurrence_count DESC
            LIMIT 5
            """,
        )
        if not rows:
            return []
        items = "\n".join(
            f"  - [{r['pattern_name']}] {r['description']} → Fix: {r['recommended_fix']}"
            for r in rows
        )
        return [f"## Known Failure Patterns\n{items}"]
    except Exception:
        return []


async def _load_session_notes(conn: asyncpg.Connection, session_id: str) -> list[str]:
    try:
        rows = await conn.fetch(
            "SELECT note FROM session_notes WHERE session_id = $1 ORDER BY created_at DESC LIMIT 3",
            session_id,
        )
        if not rows:
            return []
        notes = "\n".join(f"  - {r['note']}" for r in rows)
        return [f"## Session Notes\n{notes}"]
    except Exception:
        return []


async def _load_past_rca(conn: asyncpg.Connection, user_id: str) -> list[str]:
    try:
        rows = await conn.fetch(
            """
            SELECT root_cause, recommended_fix, created_at::date as date
            FROM rca_outcomes
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 3
            """,
            user_id,
        )
        if not rows:
            return []
        items = "\n".join(
            f"  - [{r['date']}] {r['root_cause']} → {r['recommended_fix']}"
            for r in rows
        )
        return [f"## Recent RCA History\n{items}"]
    except Exception:
        return []


# ── Outcome recorder (self-improvement loop) ─────────────────────────────────


async def record_rca_outcome(
    *,
    session_id: str,
    user_id: str,
    root_cause: str,
    confidence: float,
    recommended_fix: str,
    outcome_feedback: str | None = None,
) -> None:
    """Persist an RCA outcome; trigger pattern seeding if warranted."""
    try:
        conn = await _get_conn()
        try:
            await conn.execute(
                """
                INSERT INTO rca_outcomes
                  (session_id, user_id, root_cause, confidence, recommended_fix, outcome_feedback)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                session_id, user_id, root_cause, confidence, recommended_fix, outcome_feedback,
            )
            if confidence >= 0.9:
                await _maybe_seed_pattern(conn, root_cause, recommended_fix, confidence)
        finally:
            await conn.close()
    except Exception as exc:
        logger.warning(f"record_rca_outcome: failed — {exc}")


async def _maybe_seed_pattern(
    conn: asyncpg.Connection,
    root_cause: str,
    recommended_fix: str,
    confidence: float,
) -> None:
    """Upsert failure_patterns; only promote when occurrence_count reaches 2."""
    try:
        await conn.execute(
            """
            INSERT INTO failure_patterns
              (pattern_name, description, recommended_fix, confidence, occurrence_count)
            VALUES ($1, $1, $2, $3, 1)
            ON CONFLICT (pattern_name) DO UPDATE
              SET occurrence_count = failure_patterns.occurrence_count + 1,
                  confidence       = GREATEST(failure_patterns.confidence, EXCLUDED.confidence),
                  recommended_fix  = EXCLUDED.recommended_fix
            """,
            root_cause[:120],   # truncate for pattern_name
            recommended_fix,
            confidence,
        )
    except Exception as exc:
        logger.warning(f"_maybe_seed_pattern: failed — {exc}")
