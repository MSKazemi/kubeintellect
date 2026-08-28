"""Data retention — the pass that keeps this database bounded, and the tables it refuses.

Until 2026-08-28 nothing in this tree deleted a row on a clock. Twenty tables in
`db/schema.sql`, every one of them append-only in practice, and the only `DELETE`s anywhere
were the deliberate ones: a user forgetting a preference, and RTBF (`security.forget_subject`).
A long-lived install therefore grew `request_log` by one row per chat completion and
`decision_log` by one row per recorded step, for ever, with no documented RPO, no prune, and
no setting to ask for one. That is the enterprise-readiness gap recorded as **A10**.

What this module does NOT do is as important as what it does, so both are data here:

* :data:`_RULES` — tables that may age out, each with the column time is measured on and the
  reason it is safe. These are telemetry and completed work items: nothing reads them back
  after the window, and nothing else's correctness depends on a row surviving.
* :data:`REFUSED` — tables this pass will never touch, each with a dated written reason.
  The hash-chained ledgers are the sharp case: `decision_log` and `memory_audit` are
  tamper-EVIDENCE, and their own schema comments record that deleting the newest rows breaks
  no link and so is invisible to `verify_chain`. `decision_log_head` / `memory_chain_head`
  exist precisely to make such a truncation contradict the record. A retention pass that
  pruned them would be a tamper-evidence bypass shipped as a feature — it would leave the
  head anchor pointing past the end of the chain, and every subsequent postmortem would
  report the install's own housekeeping as evidence of tampering. Pruning an audit ledger
  needs a verified archive and a declared gap first (`kubeintellect chain-truncate`), which is
  a deliberate per-chain act and stays deliberately outside this scheduled pass.

Bounded by construction: one pass deletes at most :data:`_PRUNE_BATCH` rows per table, so a
first run against years of history takes many passes instead of one lock-holding statement.
Gated by ``MEMORY_RETENTION_DAYS`` (0 = keep everything, the default — a data-deleting
default would be the wrong way round).

Failure discipline matches every other consolidation pass: catch, log, register, return.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.autonomy.promotion_stats import WINDOW_MAX_DAYS
from app.core.config import settings
from app.memory import pass_health, service
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Rows deleted per table per pass. A retention pass runs inside the consolidation loop and
#: shares its connection pool with live requests, so it never issues an unbounded DELETE.
_PRUNE_BATCH = 5000


@dataclass(frozen=True)
class RetentionRule:
    """One prunable table: what ages out, measured on which column, and why it is safe."""

    table: str
    ts_column: str
    why: str
    #: Extra predicate — only rows matching it are eligible (e.g. finished work items).
    where: str | None = None
    #: A retention floor this table's age is never allowed below, and the reason. A shorter
    #: ``MEMORY_RETENTION_DAYS`` is clamped up to it rather than honoured.
    floor_days: int | None = None
    floor_why: str = ""


_RULES: tuple[RetentionRule, ...] = (
    RetentionRule(
        "request_log", "created_at",
        "API access telemetry (who called what, when). Read by nothing after the fact; it is "
        "the fastest-growing table in the schema — one row per chat completion.",
    ),
    RetentionRule(
        "session_notes", "created_at",
        "Free-text scratch notes attached to a session. Nothing recalls them across sessions.",
    ),
    RetentionRule(
        "fleet_signals", "created_at",
        "Per-cluster detector/runaway signals pooled for fleet-wide pattern detection. Pooling "
        "is a recency question; an old signal is noise, not history.",
    ),
    RetentionRule(
        "prospective_memory", "created_at",
        "Re-checks that already reached a terminal state. A 'did the fix hold?' intent is "
        "spent once it has been graded; the grade itself lives on in the episode.",
        where="status IN ('done', 'cancelled')",
    ),
    RetentionRule(
        "promotion_outcomes", "created_at",
        "Statistical samples for the ADR-102 promotion engine — explicitly NOT an audit ledger "
        "(its schema comment says so), and the engine only ever reads a rolling window.",
        floor_days=WINDOW_MAX_DAYS,
        floor_why=(
            "the A3 statistical brake reads a rolling window of WINDOW_MAX_DAYS; pruning inside "
            "it would delete the failures the brake demotes on, so a short retention setting "
            "would quietly widen what the watchtower may do unattended"
        ),
    ),
)

#: Tables this pass will never prune, and the dated reason. Asserted by the test suite, so a
#: later `_RULES` entry cannot silently start deleting one of them.
REFUSED: dict[str, str] = {
    "decision_log":
        "2026-08-28: hash-chained tamper-evidence (ADR-005). Deleting the newest rows of an "
        "episode breaks no link, so `verify_chain` cannot see it — which is exactly why "
        "`decision_log_head` exists. Pruning here would make the install's own housekeeping "
        "indistinguishable from tampering, and would contradict the head anchor for ever. "
        "Needs an export-then-truncate flow, not a clock — and that flow now exists as a "
        "MANUAL, per-chain command: `kubeintellect chain-export` writes a self-verifying "
        "archive and `kubeintellect chain-truncate` removes exactly those rows after "
        "declaring the gap in `chain_truncation`. It stays out of this scheduled pass on "
        "purpose: deciding a ledger may be shortened is a human act with an archive in hand, "
        "and a clock cannot hold the archive.",
    "memory_audit":
        "2026-08-28: the same hash chain over the memory write path (ADR-018 R8.2), anchored by "
        "`memory_chain_head`. Same reasoning as `decision_log`, and the same manual path — "
        "`kubeintellect chain-export memory_audit <cluster-id>` then `chain-truncate`.",
    "decision_log_head":
        "2026-08-28: one row per episode, and it is the anchor a truncation is checked against. "
        "Deleting it deletes the evidence, not the data.",
    "memory_chain_head":
        "2026-08-28: the same anchor, one row per cluster, for the memory write chain. It is "
        "the only record of how far that chain got, so deleting it is what makes a truncation "
        "of `memory_audit` undetectable rather than merely unlogged.",
    "episodes":
        "2026-08-28: L1 episodic memory — the thing the product recalls — and the row "
        "`decision_log.episode_id` and `prospective_memory.source_episode_id` point at. Ageing "
        "it out on a clock would delete learned operational history and orphan a live chain. "
        "Deliberate deletion has a path already: `security.forget_subject` (RTBF, R8.4).",
}

_SQL = """
    DELETE FROM {table} WHERE ctid IN (
        SELECT ctid FROM {table}
        WHERE {ts} < now() - make_interval(days => $1){extra}
        LIMIT {batch}
    )
"""


def effective_days(rule: RetentionRule, retention_days: int) -> int:
    """The age this rule actually prunes at — the setting, clamped up by any floor."""
    if rule.floor_days is not None:
        return max(retention_days, rule.floor_days)
    return retention_days


def _deleted(status: Any) -> int:
    """asyncpg returns the command tag (``"DELETE 12"``); anything else counts as zero."""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


async def prune_once() -> int:
    """Delete rows past the retention window, at most ``_PRUNE_BATCH`` per table. Returns total.

    A no-op unless ``MEMORY_RETENTION_DAYS`` is positive. Every table is a module constant, never
    caller input, so the table/column interpolation below cannot carry anything from a request.
    """
    days = settings.MEMORY_RETENTION_DAYS
    if days <= 0:
        return 0
    pool = service._pool
    if pool is None:
        return 0

    total = 0
    for rule in _RULES:
        sql = _SQL.format(
            table=rule.table, ts=rule.ts_column, batch=_PRUNE_BATCH,
            extra=f" AND {rule.where}" if rule.where else "",
        )
        try:
            deleted = _deleted(await pool.execute(sql, effective_days(rule, days)))
        except Exception as exc:
            logger.warning(f"retention: prune of {rule.table} failed: {exc}")
            pass_health.record_failure("retention_pruned", exc)
            continue
        if deleted:
            logger.info(
                f"retention: pruned {deleted} row(s) from {rule.table} older than "
                f"{effective_days(rule, days)}d"
                + (" (clamped by floor)" if effective_days(rule, days) != days else "")
            )
            total += deleted
    return total
