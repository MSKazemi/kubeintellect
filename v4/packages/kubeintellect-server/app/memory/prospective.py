"""Prospective memory — "remember to re-check condition C at/after time T" (ADR-017).

The one memory the SoA study flagged that V4 lacked entirely: intending to act *later*.
Retrospective memory (episodes/KG) answers "what happened"; prospective memory answers
"what must I still do." The canonical ops need is post-fix verification — after the
watchtower applies an autonomous fix it schedules a re-check ("did the fix hold in 15m?");
the consolidation scheduler fires the due re-check and records the outcome (R6.4).

Design:
  - schedule_recheck()  → upsert a pending row, deduped by (cluster_id, dedup_key) so a
    flapping fix refreshes one re-check instead of piling up a backlog.
  - run_prospective_once() → the scheduler pass (called from the consolidation loop):
    claim due pending rows, FIRE each through the autonomy ladder, record the outcome.
  - Firing routes through the SAME ladder the watchtower uses (ADR-003): A0 namespaces
    never fire (recorded 'skipped_a0'); A1+ dispatches an investigation. The dispatch is
    pluggable (set_dispatch) so a re-check can open a real investigation in production while
    tests inject a stub — the default logs the intent (never mutates).

Pool ownership: uses the memory service's pool (`app.memory.service._pool`), like
consolidation/promotion. Gated by `MEMORY_PROSPECTIVE` (default off).
Failure discipline: every function catches, logs, and returns a harmless value — a
prospective failure must never break a request or stall the consolidation loop.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.autonomy.ladder import at_least, level_for_namespace
from app.core.config import settings
from app.memory import service
from app.utils.logger import get_logger

logger = get_logger(__name__)

# How many due re-checks one scheduler pass will fire (keeps a pass bounded).
_FIRE_BATCH = 50

# Dispatch hook: given a due re-check row + its resolved autonomy level, perform the
# actual re-verification and return an outcome string. Injectable so production can wire
# a real investigation and tests can stub it. Default = log-only (never mutates).
DispatchFn = Callable[[dict[str, Any], str], Awaitable[str]]
_dispatch: DispatchFn | None = None


def set_dispatch(fn: DispatchFn | None) -> None:
    """Install the re-check dispatcher (production) or reset it (tests)."""
    global _dispatch
    _dispatch = fn


async def _default_dispatch(row: dict[str, Any], level: str) -> str:
    """A1+ default: record the intent to re-verify. Real investigation is wired by the
    watchtower via set_dispatch; the default never mutates the cluster."""
    logger.info(
        f"prospective_recheck_due cluster={row.get('cluster_id')} "
        f"ns={row.get('namespace')} level={level} condition={row.get('condition')!r}"
    )
    return "rechecked"


_SQL_SCHEDULE = """
    INSERT INTO prospective_memory
      (cluster_id, namespace, condition, check_query, dedup_key, due_at,
       source_episode_id, created_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
    ON CONFLICT (cluster_id, dedup_key) DO UPDATE SET
        condition   = EXCLUDED.condition,
        check_query = EXCLUDED.check_query,
        due_at      = EXCLUDED.due_at,
        status      = 'pending',          -- reopen a re-check that already fired
        outcome     = NULL,
        fired_at    = NULL
    RETURNING id
"""

# Claim due pending rows atomically: flip to 'fired' in the same statement that reads
# them, so two concurrent passes (startup + loop) never fire the same row twice.
_SQL_CLAIM_DUE = """
    UPDATE prospective_memory
    SET status = 'fired', fired_at = now()
    WHERE id IN (
        SELECT id FROM prospective_memory
        WHERE status = 'pending' AND due_at <= now()
        ORDER BY due_at
        LIMIT $1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, cluster_id, namespace, condition, check_query, source_episode_id
"""

_SQL_RECORD = """
    UPDATE prospective_memory
    SET outcome = $2, status = $3
    WHERE id = $1
"""


async def schedule_recheck(
    *,
    cluster_id: str,
    condition: str,
    due_at: float,
    namespace: str = "",
    check_query: str | None = None,
    dedup_key: str | None = None,
    source_episode_id: str | None = None,
    created_by: str | None = None,
) -> str | None:
    """Record a re-check to fire at/after ``due_at`` (unix seconds). Returns its id.

    ``dedup_key`` collapses repeat re-checks for the same intent (defaults to the
    condition text). Idempotent: re-scheduling refreshes the due time and reopens the row.
    """
    pool = service._pool
    if pool is None or not cluster_id or not condition.strip():
        return None
    try:
        row = await pool.fetchrow(
            _SQL_SCHEDULE,
            cluster_id,
            namespace or "",
            condition.strip()[:1000],
            (check_query or None),
            (dedup_key or condition).strip()[:500],
            _ts(due_at),
            source_episode_id,
            created_by,
        )
        return str(row["id"]) if row else None
    except Exception as exc:
        logger.warning(f"prospective: schedule failed: {exc}")
        return None


async def run_prospective_once() -> int:
    """Scheduler pass: fire every due pending re-check and record its outcome (R6.4).

    Returns the number fired. Gated by ``MEMORY_PROSPECTIVE`` — a no-op when off.
    """
    if not settings.MEMORY_PROSPECTIVE:
        return 0
    pool = service._pool
    if pool is None:
        return 0
    try:
        due = await pool.fetch(_SQL_CLAIM_DUE, _FIRE_BATCH)
    except Exception as exc:
        logger.warning(f"prospective: claim pass failed: {exc}")
        return 0

    fired = 0
    dispatch = _dispatch or _default_dispatch
    for row in due:
        rec = dict(row)
        # Route the re-check through the autonomy ladder, exactly like the watchtower:
        # a re-check in an A0 (observe-only / protected) namespace never fires.
        level = level_for_namespace(rec.get("namespace") or "")
        try:
            if not at_least(level, "A1"):
                outcome = "skipped_a0"
            else:
                outcome = await dispatch(rec, level)
        except Exception as exc:
            logger.warning(f"prospective: dispatch failed id={rec.get('id')}: {exc}")
            outcome = "error"
        await _record_outcome(pool, rec["id"], outcome)
        fired += 1
    if fired:
        logger.info(f"prospective: fired {fired} due re-check(s)")
    return fired


# Outcome → terminal status. A re-check that ran is 'done'; one that can never fire in this
# namespace (A0/observe-only) is 'cancelled' (no point retrying); a transient dispatch error
# goes back to 'pending' so the next pass retries it. Either way the outcome is recorded (R6.4).
_TERMINAL = {"rechecked": "done", "resolved": "done", "still_broken": "done",
             "skipped_a0": "cancelled"}


async def _record_outcome(pool, recheck_id, outcome: str) -> None:
    status = _TERMINAL.get(outcome, "pending")
    try:
        await pool.execute(_SQL_RECORD, recheck_id, outcome, status)
    except Exception as exc:
        logger.warning(f"prospective: record outcome failed id={recheck_id}: {exc}")


def _ts(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)
