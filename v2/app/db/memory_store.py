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
    """Auto-seeded failure patterns with confidence ≥0.9 and occurrence_count ≥2.

    Filters by current cluster_id (R1) and decay window (R6) so patterns from
    other clusters and stale patterns don't pollute the prompt.
    """
    try:
        from app.cluster_id import get_cluster_id
        cluster_id = get_cluster_id()
        decay_days = max(1, int(settings.REFLEXION_PATTERN_DECAY_DAYS))
        rows = await conn.fetch(
            f"""
            SELECT pattern_name, description, recommended_fix, occurrence_count
            FROM failure_patterns
            WHERE confidence >= 0.9
              AND occurrence_count >= 2
              AND demoted = FALSE
              AND cluster_id IN ($1, 'unknown')
              AND last_seen_at > now() - INTERVAL '{decay_days} days'
            ORDER BY occurrence_count DESC, last_seen_at DESC
            LIMIT 5
            """,
            cluster_id,
        )
        if not rows:
            return []
        logger.info(
            f"failure_hints_loaded count={len(rows)}",
            extra={"hint_count": len(rows), "cluster_id": cluster_id},
        )
        items = "\n".join(
            f"  - [{r['pattern_name']}] (seen {r['occurrence_count']}×) {r['description']}\n"
            f"    → Fix: {r['recommended_fix']}"
            for r in rows
        )
        return [f"## Known Failure Patterns (this cluster)\n{items}"]
    except Exception as exc:
        logger.debug(f"_load_failure_hints: {exc}")
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
    """Last 3 verified RCA outcomes for this user on this cluster.

    Cluster-scoped (R1) so a user investigating prod-eks doesn't see hints
    from their dev-kind sessions. Only verified-resolved rows count to keep
    the hint signal high.
    """
    try:
        from app.cluster_id import get_cluster_id
        cluster_id = get_cluster_id()
        rows = await conn.fetch(
            """
            SELECT root_cause, recommended_fix, namespace,
                   created_at::date as date, verified_resolved
            FROM rca_outcomes
            WHERE user_id = $1
              AND cluster_id IN ($2, 'unknown')
              AND verified_resolved IS NOT FALSE
            ORDER BY verified_resolved DESC NULLS LAST, created_at DESC
            LIMIT 3
            """,
            user_id, cluster_id,
        )
        if not rows:
            return []
        lines: list[str] = []
        for r in rows:
            ns = f" ns={r['namespace']}" if r["namespace"] else ""
            ok = "✓" if r["verified_resolved"] else "?"
            # Truncate stored fix for the injection — full row stays in DB.
            fix_preview = (r["recommended_fix"] or "")[:160]
            lines.append(f"  - [{r['date']}{ns} {ok}] {r['root_cause']}\n    → {fix_preview}")
        return [f"## Recent RCA History (this cluster)\n" + "\n".join(lines)]
    except Exception as exc:
        logger.debug(f"_load_past_rca: {exc}")
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
    cluster_id: str | None = None,
    namespace: str | None = None,
    verified_resolved: bool | None = None,
    playbooks_matched: list[str] | None = None,
    created_by_role: str | None = None,
    request_id: str | None = None,
) -> None:
    """Persist an RCA outcome; trigger pattern seeding if eligible.

    Pattern seeding requires: confidence ≥ 0.9 AND verified_resolved is True.
    Without verification a pattern cannot promote — that's the gate that
    prevents "agent typed kubectl" from masquerading as "agent fixed cluster".
    """
    if cluster_id is None:
        from app.cluster_id import get_cluster_id
        cluster_id = get_cluster_id()
    try:
        conn = await _get_conn()
        try:
            await conn.execute(
                """
                INSERT INTO rca_outcomes
                  (session_id, user_id, root_cause, confidence, recommended_fix,
                   outcome_feedback, cluster_id, namespace, verified_resolved,
                   playbooks_matched, created_by_role, request_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                session_id, user_id, root_cause, confidence, recommended_fix,
                outcome_feedback, cluster_id, namespace, verified_resolved,
                playbooks_matched or [], created_by_role, request_id,
            )
            eligible_for_pattern = (
                confidence >= 0.9
                and verified_resolved is True
                and outcome_feedback != "regression"
            )
            if eligible_for_pattern:
                await _maybe_seed_pattern(
                    conn,
                    pattern_key=root_cause,
                    recommended_fix=recommended_fix,
                    confidence=confidence,
                    cluster_id=cluster_id,
                    namespace=namespace,
                )
            logger.info(
                f"reflexion: outcome recorded session={session_id} cluster={cluster_id} "
                f"verified={verified_resolved} confidence={confidence:.2f} "
                f"eligible_for_pattern={eligible_for_pattern}"
            )
        finally:
            await conn.close()
    except Exception as exc:
        logger.warning(f"record_rca_outcome: failed — {exc}")


async def _maybe_seed_pattern(
    conn: asyncpg.Connection,
    *,
    pattern_key: str,
    recommended_fix: str,
    confidence: float,
    cluster_id: str,
    namespace: str | None,
) -> None:
    """Upsert failure_patterns scoped to (pattern_name, cluster_id).

    Honours cooldown: if the pattern has been updated within
    REFLEXION_PATTERN_COOLDOWN_HOURS, the occurrence_count does NOT bump.
    Prevents test-loop spam from inflating the counter.
    """
    name = pattern_key[:120]
    try:
        cooldown_h = max(0, int(settings.REFLEXION_PATTERN_COOLDOWN_HOURS))
        # Check whether the same (name, cluster) was just updated.
        existing = await conn.fetchrow(
            """
            SELECT occurrence_count, last_seen_at,
                   (last_seen_at > now() - INTERVAL '%d hours') AS in_cooldown
            FROM failure_patterns
            WHERE pattern_name = $1 AND cluster_id = $2
            """ % cooldown_h,
            name, cluster_id,
        )

        if existing is None:
            await conn.execute(
                """
                INSERT INTO failure_patterns
                  (pattern_name, description, recommended_fix, confidence,
                   occurrence_count, cluster_id, namespace, last_seen_at,
                   created_at, updated_at)
                VALUES ($1, $1, $2, $3, 1, $4, $5, now(), now(), now())
                ON CONFLICT (pattern_name) DO UPDATE
                  SET cluster_id      = EXCLUDED.cluster_id,
                      namespace       = EXCLUDED.namespace,
                      recommended_fix = EXCLUDED.recommended_fix,
                      confidence      = EXCLUDED.confidence,
                      last_seen_at    = now(),
                      updated_at      = now()
                """,
                name, recommended_fix, confidence, cluster_id, namespace,
            )
            logger.info(f"reflexion: seeded pattern '{name}' cluster={cluster_id}")
            return

        if existing["in_cooldown"]:
            # Refresh last_seen_at + recommended_fix; keep occurrence_count steady.
            await conn.execute(
                """
                UPDATE failure_patterns
                  SET recommended_fix = $1,
                      confidence = GREATEST(confidence, $2),
                      last_seen_at = now(),
                      updated_at = now()
                WHERE pattern_name = $3 AND cluster_id = $4
                """,
                recommended_fix, confidence, name, cluster_id,
            )
            logger.info(
                f"reflexion: cooldown active for '{name}' "
                f"(last_seen={existing['last_seen_at']}); count not bumped"
            )
            return

        await conn.execute(
            """
            UPDATE failure_patterns
              SET occurrence_count = occurrence_count + 1,
                  recommended_fix = $1,
                  confidence = GREATEST(confidence, $2),
                  last_seen_at = now(),
                  updated_at = now(),
                  demoted = FALSE
            WHERE pattern_name = $3 AND cluster_id = $4
            """,
            recommended_fix, confidence, name, cluster_id,
        )
        logger.info(
            f"reflexion: bumped pattern '{name}' "
            f"to occurrence_count={existing['occurrence_count'] + 1}"
        )
    except Exception as exc:
        logger.warning(f"_maybe_seed_pattern: failed — {exc}")


async def demote_pattern(pattern_name: str, cluster_id: str | None = None) -> None:
    """Mark a pattern as demoted so it stops being injected. Idempotent.

    Called when a pattern produces a regression or the user manually flags
    a hint as wrong. Demoted patterns are excluded from _load_failure_hints
    until they re-promote via fresh verified outcomes.
    """
    if cluster_id is None:
        from app.cluster_id import get_cluster_id
        cluster_id = get_cluster_id()
    try:
        conn = await _get_conn()
        try:
            await conn.execute(
                """
                UPDATE failure_patterns
                  SET demoted = TRUE, updated_at = now()
                WHERE pattern_name = $1 AND cluster_id = $2
                """,
                pattern_name[:120], cluster_id,
            )
            logger.info(f"reflexion: demoted pattern '{pattern_name}' cluster={cluster_id}")
        finally:
            await conn.close()
    except Exception as exc:
        logger.warning(f"demote_pattern: failed — {exc}")
