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


class MemoryStoreUnavailable(RuntimeError):
    """The pinned V4 memory context could not be loaded — not the same as having none.

    `load_memory_context` returned `""` for both, and `""` is exactly what a brand-new user with no
    preferences, no failure hints, no session notes and no past RCA produces. So a Postgres outage
    reached the coordinator as a clean slate: no preferences to honour, no prior RCA to build on,
    and nothing anywhere saying the lookup had failed. Sibling of `episodes.MemoryUnavailable`
    (pass 46) — this is the other half of the same SystemMessage.
    """


async def load_memory_context(user_id: str, session_id: str) -> str:
    """Return a pinned context string ≤500 tokens for the coordinator SystemMessage."""
    if settings.USE_SQLITE:
        return ""   # memory store requires PostgreSQL; silently skip in SQLite mode

    parts: list[str] = []
    failed: list[str] = []

    try:
        conn = await _get_conn()
        try:
            for label, load in (
                ("operator preferences", lambda: _load_user_prefs(conn, user_id)),
                ("failure hints", lambda: _load_failure_hints(conn)),
                ("session notes", lambda: _load_session_notes(conn, session_id)),
                ("past RCA", lambda: _load_past_rca(conn, user_id)),
            ):
                # Per-section, so one failing query does not cost the other three — but never
                # silently. Each loader used to swallow its own exception and return `[]`, so a
                # query error in exactly one section produced a context that reads as complete:
                # `MemoryStoreUnavailable` was not raised, nothing above INFO was logged, and the
                # model was left to conclude the user has no stored preferences. The whole point
                # of that exception is "could not load ≠ has none"; a partial failure defeats it
                # exactly as well as a total one.
                try:
                    parts += await load()
                except Exception as exc:      # noqa: BLE001 — any failure is still a failure
                    logger.warning(f"memory_store: section {label!r} failed — {exc}")
                    failed.append(label)
        finally:
            await conn.close()
    except Exception as exc:
        logger.warning(f"memory_store: could not load context — {exc}")
        raise MemoryStoreUnavailable(f"pinned memory context unavailable: {exc}") from exc

    combined = "\n\n".join(p for p in parts if p.strip())
    if failed:
        # Prepended, not appended: the tail is what `_MAX_CONTEXT_CHARS` removes, and a notice
        # the model never sees is the same as no notice at all.
        notice = _partial_failure_notice(failed)
        combined = f"{notice}\n\n{combined}" if combined else notice
    if len(combined) > _MAX_CONTEXT_CHARS:
        combined = combined[:_MAX_CONTEXT_CHARS] + "\n... [context truncated]"
    return combined


def _partial_failure_notice(failed: list[str]) -> str:
    """Name the memory that could not be read, in the same terms `memory_loader` uses for a
    total failure — a missing section must not read as an empty one."""
    return (
        "## Memory partially unavailable\n"
        f"Stored {', '.join(failed)} could NOT be read — this is not the same as there being "
        "none. Do not assume the user has no preferences or that this issue has no precedent; "
        "say that part of the stored history could not be checked."
    )


# ── Section caps ──────────────────────────────────────────────────────────────
# Every loader below caps its rows so the whole block stays inside the ~500-token budget. The
# cap is correct; presenting the result as the operator's COMPLETE remembered state is not.
# Measured 2026-08-24: an operator with 12 explicit preferences got 8 in the prompt, and
# "NEVER drain node-07, it hosts the license server" was one of the 4 dropped — silently, under
# a header reading "(remembered)". The model's only honest reading of that block is "these are
# the preferences", so a cap without a notice makes it infer an instruction does not exist.
#
# This module already refuses the same mistake one axis over: `_partial_failure_notice` exists
# because "a missing section must not read as an empty one". A capped section must not read as
# a complete one, for exactly the same reason.
#
# Each loader asks for `cap + 1` rows and renders `cap`. Getting the extra row back is proof
# that more exist; not getting it is proof that they do not. That is one query, not two, and it
# is why the notice never claims a count it did not measure.


def _split_capped(rows: list, cap: int) -> tuple[list, bool]:
    """Return the rows to render and whether the query proved more exist beyond them."""
    return list(rows[:cap]), len(rows) > cap


def _more_notice(noun: str) -> str:
    """One line, appended to a section whose query proved it is showing a subset."""
    return (
        f"  … MORE {noun} are stored than the ones listed above and are NOT shown here "
        f"(oldest/lowest-ranked omitted for space). Absence from this list is NOT evidence "
        f"that none exists — ask, or say you only checked the most relevant."
    )


async def _load_user_prefs(conn: asyncpg.Connection, user_id: str) -> list[str]:
    """Active operator preferences — explicit first, then confident/recent inferred.

    Inferred preferences that fell below PREFERENCE_MIN_CONFIDENCE or went stale
    past PREFERENCE_DECAY_DAYS are excluded (they are being forgotten). Explicit
    preferences are always shown. Backward-compatible with pre-preference-memory
    rows (source defaults to 'explicit', confidence 1.0).
    """
    if not settings.PREFERENCE_MEMORY_ENABLED:
        return []
    decay_days = max(1, int(settings.PREFERENCE_DECAY_DAYS))
    min_conf = float(settings.PREFERENCE_MIN_CONFIDENCE)
    rows = await conn.fetch(
        f"""
        SELECT key, value, source, confidence
        FROM user_prefs
        WHERE user_id = $1
          AND (
            source = 'explicit'
            OR (confidence >= $2
                AND last_seen_at > now() - INTERVAL '{decay_days} days')
          )
        ORDER BY (source = 'explicit') DESC, confidence DESC, key
        LIMIT 9
        """,
        user_id, min_conf,
    )
    if not rows:
        return []
    rows, more = _split_capped(rows, 8)
    lines = []
    for r in rows:
        if r["source"] == "inferred":
            lines.append(f"  {r['key']}: {r['value']}  (inferred, confidence {float(r['confidence']):.0%})")
        else:
            lines.append(f"  {r['key']}: {r['value']}")
    if more:
        lines.append(_more_notice("operator preferences"))
    return ["## Operator Preferences (remembered)\n" + "\n".join(lines)]


async def _load_failure_hints(conn: asyncpg.Connection) -> list[str]:
    """Auto-seeded failure patterns with confidence ≥0.9 and occurrence_count ≥2.

    Filters by current cluster_id (R1) and decay window (R6) so patterns from
    other clusters and stale patterns don't pollute the prompt.
    """
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
        LIMIT 6
        """,
        cluster_id,
    )
    if not rows:
        return []
    rows, more = _split_capped(rows, 5)
    logger.info(
        f"failure_hints_loaded count={len(rows)}",
        extra={"hint_count": len(rows), "cluster_id": cluster_id},
    )
    items = "\n".join(
        f"  - [{r['pattern_name']}] (seen {r['occurrence_count']}×) {r['description']}\n"
        f"    → Fix: {r['recommended_fix']}"
        for r in rows
    )
    if more:
        items += "\n" + _more_notice("known failure patterns for this cluster")
    return [f"## Known Failure Patterns (this cluster)\n{items}"]


async def _load_session_notes(conn: asyncpg.Connection, session_id: str) -> list[str]:
    rows = await conn.fetch(
        "SELECT note FROM session_notes WHERE session_id = $1 ORDER BY created_at DESC LIMIT 4",
        session_id,
    )
    if not rows:
        return []
    rows, more = _split_capped(rows, 3)
    notes = "\n".join(f"  - {r['note']}" for r in rows)
    if more:
        notes += "\n" + _more_notice("notes from this session")
    return [f"## Session Notes\n{notes}"]


async def _load_past_rca(conn: asyncpg.Connection, user_id: str) -> list[str]:
    """Last 3 verified RCA outcomes for this user on this cluster.

    Cluster-scoped (R1) so a user investigating prod-eks doesn't see hints
    from their dev-kind sessions. Only verified-resolved rows count to keep
    the hint signal high.
    """
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
        LIMIT 4
        """,
        user_id, cluster_id,
    )
    if not rows:
        return []
    rows, more = _split_capped(rows, 3)
    lines: list[str] = []
    for r in rows:
        ns = f" ns={r['namespace']}" if r["namespace"] else ""
        ok = "✓" if r["verified_resolved"] else "?"
        # Truncate the stored fix for the injection — the full row stays in the DB. Say so when
        # it happens: a remediation cut mid-command reads as a complete command, and the model
        # has no way to tell 160 characters of a fix from all of it.
        full_fix = r["recommended_fix"] or ""
        fix_preview = full_fix[:160]
        if len(full_fix) > 160:
            fix_preview += " …[fix truncated, not the whole command]"
        lines.append(f"  - [{r['date']}{ns} {ok}] {r['root_cause']}\n    → {fix_preview}")
    if more:
        lines.append(_more_notice("past RCA outcomes for this cluster"))
    return ["## Recent RCA History (this cluster)\n" + "\n".join(lines)]


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
