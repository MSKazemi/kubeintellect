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
    never fire (recorded 'skipped_a0'); A1+ re-reads the cluster and grades the condition.
    The dispatch is pluggable (set_dispatch) so a richer re-check (an LLM investigation)
    can be injected, and so tests can stub it — but the DEFAULT is the real verifier, not
    a placeholder, because nothing was ever wiring one (see `_default_dispatch`).

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
from app.memory import pass_health
from app.utils.logger import get_logger

logger = get_logger(__name__)

# How many due re-checks one scheduler pass will fire (keeps a pass bounded).
_FIRE_BATCH = 50

# Dispatch hook: given a due re-check row + its resolved autonomy level, perform the
# actual re-verification and return an outcome string. Injectable so a richer re-check
# (an LLM investigation that reads logs and explains *why*) can replace the default, and
# so tests can stub it. Read-only in every form — a re-check never mutates the cluster.
DispatchFn = Callable[[dict[str, Any], str], Awaitable[str]]
_dispatch: DispatchFn | None = None


def set_dispatch(fn: DispatchFn | None) -> None:
    """Install a richer re-check dispatcher, or reset to the built-in verifier (``None``)."""
    global _dispatch
    _dispatch = fn


async def _default_dispatch(row: dict[str, Any], level: str) -> str:
    """A1+ default: actually re-read the cluster and grade the condition.

    Until 2026-08-28 this logged the row and returned ``"rechecked"`` without looking at
    anything, and ``_TERMINAL`` mapped that to ``status='done'``. Nothing outside the tests
    ever called :func:`set_dispatch`, so *every* scheduled post-fix re-check in production
    closed as a completed verification that had verified nothing — the one row an operator
    would read to answer "did the fix hold?" said yes by construction. The fix is to make the
    default the verifier rather than to keep waiting for a caller that was never written.

    Deliberately model-free and read-only: two ``kubectl get`` reads, scanned by the same
    :func:`_scan_snapshot` the coordinator's post-fix check uses, so the two graders cannot
    disagree about what "resolved" means. Lingering Warning events with healthy pods are not
    a failure, matching ``coordinator._verify_resolution``.

    Returns ``"resolved"`` (pods healthy), ``"still_broken"``, or ``"unverified"`` when the
    read itself failed — which is NOT a grade, and :data:`_TERMINAL` leaves it ``pending`` so
    the next pass tries again rather than closing the row on an answer nobody obtained.
    """
    import asyncio

    from app.agent.nodes.context_fetcher import (
        _kubectl_snapshot,
        _scan_snapshot,
        _unavailable_reason,
    )

    namespace = (row.get("namespace") or "").strip()
    ns_arg = ["-n", namespace] if namespace else ["--all-namespaces"]
    # `_kubectl_snapshot` is a blocking subprocess call and this runs inside the
    # consolidation loop — off-thread so one slow cluster read cannot stall the loop.
    pods_ok, pods_out = await asyncio.to_thread(_kubectl_snapshot, ["get", "pods", *ns_arg])
    if not pods_ok:
        logger.warning(
            f"prospective: cannot verify re-check id={row.get('id')} ns={namespace or '*'} — "
            f"the cluster read failed ({_unavailable_reason(pods_out)}); "
            f"recording 'unverified', not a result"
        )
        return "unverified"
    events_ok, events_out = await asyncio.to_thread(_kubectl_snapshot, [
        "get", "events", *ns_arg,
        "--sort-by=.lastTimestamp",
        "--field-selector=type=Warning",
    ])
    has_issues, _has_warnings, pod_count = _scan_snapshot(
        pods_out, events_out, pods_ok=pods_ok, events_ok=events_ok)
    outcome = "still_broken" if has_issues else "resolved"
    logger.info(
        f"prospective_recheck cluster={row.get('cluster_id')} ns={namespace} level={level} "
        f"pods={pod_count} outcome={outcome} condition={row.get('condition')!r}"
    )
    return outcome


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
        pass_health.record_failure("prospective_fired", exc)
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


# Outcome → terminal status. A re-check that produced a GRADE is 'done'; one that can never
# fire in this namespace (A0/observe-only) is 'cancelled' (no point retrying); anything else —
# a dispatch exception ('error'), or a cluster read that failed ('unverified') — falls through
# to 'pending' so the next pass retries it. Either way the outcome is recorded (R6.4).
#
# 'unverified' must never be listed here. It is the answer "we could not look", and closing a
# row on it is the exact failure this table existed to avoid.
#
# 'rechecked' is kept only to interpret rows written before 2026-08-28, when the default
# dispatcher returned it without reading anything. Those rows are `done` in the table and
# cannot be re-graded after the fact; the label is retained so they still read as terminal
# rather than silently re-firing. Nothing produces it any more.
_TERMINAL = {"resolved": "done", "still_broken": "done",
             "skipped_a0": "cancelled", "rechecked": "done"}


async def _record_outcome(pool, recheck_id, outcome: str) -> None:
    # A non-terminal outcome returns the row to 'pending' WITHOUT advancing `due_at`, so it
    # re-fires on the next consolidation pass. That is the intended retry, and it is bounded
    # only by `_FIRE_BATCH` — an unreadable cluster means up to 50 re-reads per pass until it
    # comes back. Pre-existing behaviour for 'error'; 'unverified' now joins it.
    status = _TERMINAL.get(outcome, "pending")
    try:
        await pool.execute(_SQL_RECORD, recheck_id, outcome, status)
    except Exception as exc:
        logger.warning(f"prospective: record outcome failed id={recheck_id}: {exc}")


def _ts(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)
