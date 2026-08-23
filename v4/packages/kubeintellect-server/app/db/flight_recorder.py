"""
Flight recorder — append-only, hash-chained decision log (ADR-005).

Every typed event the server emits (except high-volume token frames) is
recorded as one row in the decision_log table, hash-chained per episode so
after-the-fact tampering is detectable. In V2-instrumentation mode an
*episode* is a session: episode_id == session_id.

Write path discipline (same as audit.py): record() never blocks and never
raises — events go onto an in-process queue and a background drain task
batches them into Postgres. A recorder outage degrades auditability, never
availability.

Chain format:
    canonical  = JSON({"episode_id", "seq", "kind", "payload"},
                      sort_keys=True, separators=(",", ":"))
    hash       = sha256(prev_hash + canonical).hexdigest()
    genesis prev_hash = ""

verify_chain() recomputes the chain over rows ordered by seq and is used by
the replay endpoint and tests.

What the chain does and does not prove
--------------------------------------
The chain proves that no persisted row was **modified, reordered or removed**.
It cannot, by itself, prove the log is **complete** — the write path is
fire-and-forget, so a recorder outage loses events that were never persisted.
Those are two different failures and they must not be conflated:

* A lost batch must NOT leave a seq gap. It used to: the in-process chain head
  advanced before the INSERT was known to have succeeded, so the next batch
  wrote a seq that skipped the lost rows and verify_chain reported the episode
  as tampered with. A database blip is not tampering. On failure the cached
  head is now dropped, so the next batch continues from the last *persisted*
  row (same remedy as app/memory/security.py uses for the memory audit chain).
* A lost batch must not be invisible either. Restoring contiguity alone would
  make loss undetectable — chain intact over a log with holes, which is exactly
  what the tamper-evidence claim promises cannot happen. So every loss is
  carried forward and written **into the chain** as a ``recorder_gap`` record
  on the next successful flush. The marker is chained like any other row, so it
  cannot be removed without breaking verification.

Proven against a real Postgres (2026-08-20): before this, an outage followed by
a process restart produced six rows, seq 0-5, ``chain_valid=True`` — with three
recorded events silently gone.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import asyncpg

from app.core.config import settings
from app.utils.logger import get_logger
from app.utils.redact import redact_secrets

logger = get_logger(__name__)

# Token frames are emitted per LLM token — recording them would bloat the
# table by orders of magnitude for no audit value (the checkpointer already
# stores final messages).
_SKIP_KINDS = frozenset({"token"})

# String payload fields longer than this get redacted + truncated.
_MAX_FIELD_CHARS = 1500

_pool: asyncpg.Pool | None = None
_queue: asyncio.Queue | None = None
_drain_task: asyncio.Task | None = None
# Per-episode chain state: episode_id -> (next_seq, last_hash).
_chains: dict[str, tuple[int, str]] = {}
# Unwritten loss, carried until it can be recorded: episode_id -> (events_lost, first_reason).
_pending_gaps: dict[str, tuple[int, str]] = {}

# The kind used to record loss inside the chain itself. A replay that contains one of
# these is intact *and* incomplete — the two are different answers.
GAP_KIND = "recorder_gap"

_BATCH_MAX = 50
_FLUSH_INTERVAL = 0.5  # seconds


# ── Hash chain ────────────────────────────────────────────────────────────────

def _canonical(episode_id: str, seq: int, kind: str, payload: dict) -> str:
    return json.dumps(
        {"episode_id": episode_id, "seq": seq, "kind": kind, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def compute_hash(prev_hash: str, episode_id: str, seq: int, kind: str, payload: dict) -> str:
    body = prev_hash + _canonical(episode_id, seq, kind, payload)
    return hashlib.sha256(body.encode()).hexdigest()


def verify_chain(rows: list[dict]) -> bool:
    """Recompute the hash chain over rows (ordered by seq). True iff intact."""
    prev = ""
    expected_seq = 0
    for row in rows:
        if row["seq"] != expected_seq or row["prev_hash"] != prev:
            return False
        payload = row["payload"]
        if isinstance(payload, str):  # asyncpg returns JSONB as str by default
            payload = json.loads(payload)
        if compute_hash(prev, row["episode_id"], row["seq"], row["kind"], payload) != row["hash"]:
            return False
        prev = row["hash"]
        expected_seq += 1
    return True


# ── Payload hygiene ───────────────────────────────────────────────────────────

# How deep `_scrub` will walk. Recorded payloads are shallow; the bound exists so a
# self-referential structure cannot hang the drain task, not because depth is expected.
_MAX_SCRUB_DEPTH = 6


def _scrub_value(value: Any, cap: int | None, depth: int = 0) -> Any:
    """Redact every string *anywhere* in a payload, not only the ones at the top."""
    if isinstance(value, str):
        return redact_secrets(value, max_chars=cap) if value else value
    if depth >= _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, dict):
        # Nested strings are redacted but NOT re-capped: `_MAX_FIELD_CHARS` is this module's
        # limit on one payload *field*, and a nested string arrives already capped by whoever
        # produced it (a `rollback_point` pre-state at 4000 chars, say). Shrinking it here
        # would quietly cost a capture its restorability to enforce a limit nothing asked for.
        return {k: _scrub_value(v, None, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v, None, depth + 1) for v in value]
    return value


def _scrub(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from string fields and cap their length (R8 discipline).

    **Until 2026-08-20 this walked one level.** It redacted `payload[key]` when the value was
    a string and returned everything else untouched — so a string nested inside a list or a
    dict passed through verbatim while the flag said secrets were being scrubbed before
    persisting. Measured, `{"attributes": {"ki.action": "kubectl … --token=AKIA…"}}` kept the
    token and `{"steps": [{"description": "…"}]}` kept whatever the model wrote there. The
    shapes that saved it were accidents of who wrote the call site: `rollback_point.pre_state`
    is a list of strings, and it is safe only because `kubectl_tool` redacts each capture
    itself before handing it over. A hygiene gate whose reach depends on the shape of the
    caller's dict is not a gate.
    """
    if not settings.REFLEXION_REDACT_SECRETS:
        return payload
    return {k: _scrub_value(v, _MAX_FIELD_CHARS) for k, v in payload.items()}


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def init_recorder() -> None:
    """Create the pool and start the drain task. Failure disables recording."""
    global _pool, _queue, _drain_task
    if not settings.FLIGHT_RECORDER_ENABLED:
        logger.info("flight_recorder: disabled by flag")
        return
    if settings.USE_SQLITE:
        logger.info("flight_recorder: SQLite mode — recording disabled")
        return
    try:
        _pool = await asyncpg.create_pool(
            settings.POSTGRES_DSN,
            min_size=1,
            max_size=3,
            command_timeout=5,
        )
    except Exception as exc:
        logger.warning(f"flight_recorder: could not connect — recording disabled ({exc})")
        _pool = None
        return
    _queue = asyncio.Queue()
    _drain_task = asyncio.get_running_loop().create_task(_drain())
    logger.info("flight_recorder: ready")


async def close_recorder() -> None:
    global _pool, _queue, _drain_task
    if _drain_task:
        await _flush(_collect_pending())
        _drain_task.cancel()
        _drain_task = None
    if _pool:
        await _pool.close()
        _pool = None
    _queue = None
    _chains.clear()
    _pending_gaps.clear()


# ── Write path ────────────────────────────────────────────────────────────────

def record(episode_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Queue one record. Non-blocking, never raises. Token frames are skipped."""
    if _queue is None or kind in _SKIP_KINDS:
        return
    try:
        _queue.put_nowait((episode_id, kind, payload))
    except Exception:
        pass  # full/closed queue — drop rather than block the request path


def _collect_pending() -> list[tuple[str, str, dict]]:
    items: list[tuple[str, str, dict]] = []
    if _queue is None:
        return items
    while not _queue.empty():
        try:
            items.append(_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return items


async def _chain_state(episode_id: str) -> tuple[int, str]:
    """Return (next_seq, last_hash), loading from the DB on first encounter.

    Raises if the lookup fails. It used to swallow the error and cache ``(0, "")``, which
    restarted the chain at seq 0 for an episode that already had rows — every later batch
    then collided with ``UNIQUE (episode_id, seq)`` and was lost for good. An unknown head
    is not a genesis head; the caller treats the batch as dropped and retries against
    authoritative state.
    """
    if episode_id in _chains:
        return _chains[episode_id]
    next_seq, last_hash = 0, ""
    if _pool is not None:
        row = await _pool.fetchrow(
            "SELECT seq, hash FROM decision_log"
            " WHERE episode_id = $1 ORDER BY seq DESC LIMIT 1",
            episode_id,
        )
        if row:
            next_seq, last_hash = row["seq"] + 1, row["hash"]
    _chains[episode_id] = (next_seq, last_hash)
    return next_seq, last_hash


def _drop_reason(exc: Exception) -> str:
    """One short, human-readable cause, safe to store in the log itself."""
    msg = " ".join(str(exc).split())
    if "decision_log" in msg and "does not exist" in msg:
        return "the decision_log table was missing"
    return msg[:200] or exc.__class__.__name__


def _gap_payload(count: int, reason: str) -> dict[str, Any]:
    return {
        "type": GAP_KIND,
        "dropped": count,
        "reason": reason,
        "message": (
            f"{count} recorded event(s) were LOST at this point — the flight recorder could "
            f"not write them ({reason}). This episode is incomplete; the chain below is still "
            f"verifiable, but it is not the whole story."
        ),
    }


def _carry_loss(batch: list[tuple[str, str, dict]], reason: str) -> None:
    """A batch was not persisted. Keep the chain honest about it.

    Two things must happen, and neither is optional:

    1. Drop the cached chain head for every episode in the batch, so the next flush
       continues from the last *persisted* row. Leaving the head advanced writes a seq
       gap, and ``verify_chain`` reports a seq gap as a broken chain — a database blip
       would be permanently on the record as suspected tampering.
    2. Carry the count forward so the next successful flush writes a ``recorder_gap``
       row into the chain. Without this, step 1 alone would make loss *undetectable*.
    """
    for episode_id, kind, payload in batch:
        _chains.pop(episode_id, None)
        lost = int(payload.get("dropped", 1)) if kind == GAP_KIND else 1
        prev_count, prev_reason = _pending_gaps.get(episode_id, (0, reason))
        _pending_gaps[episode_id] = (prev_count + lost, prev_reason)


async def _flush(items: list[tuple[str, str, dict]]) -> None:
    if _pool is None or (not items and not _pending_gaps):
        return
    # Loss from an earlier flush is written first, so a replay shows the hole in the
    # right place rather than at the end.
    batch: list[tuple[str, str, dict]] = [
        (episode_id, GAP_KIND, _gap_payload(count, reason))
        for episode_id, (count, reason) in _pending_gaps.items()
    ]
    _pending_gaps.clear()
    batch.extend(items)

    rows = []
    try:
        for episode_id, kind, payload in batch:
            payload = _scrub(payload)
            seq, prev_hash = await _chain_state(episode_id)
            digest = compute_hash(prev_hash, episode_id, seq, kind, payload)
            _chains[episode_id] = (seq + 1, digest)
            # v5 OTel projection (specs/02): mirror span identity into query columns.
            # Excluded from the canonical hash form, so chains stay byte-valid.
            trace_id = span_id = parent_span_id = None
            if kind == "ki_otel_span":
                trace_id = payload.get("trace_id")
                span_id = payload.get("span_id")
                parent_span_id = payload.get("parent_span_id")
            rows.append((episode_id, seq, kind, json.dumps(payload, default=str), prev_hash,
                         digest, trace_id, span_id, parent_span_id))
        await _pool.executemany(
            """
            INSERT INTO decision_log
                (episode_id, seq, kind, payload, prev_hash, hash, trace_id, span_id, parent_span_id)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
            """,
            rows,
        )
    except Exception as exc:
        reason = _drop_reason(exc)
        _carry_loss(batch, reason)
        if "the decision_log table was missing" == reason:
            logger.warning(
                "flight_recorder: 'decision_log' table missing — run: kubeintellect db-init. "
                f"{len(batch)} event(s) lost; the gap will be recorded in the chain."
            )
        else:
            logger.warning(
                f"flight_recorder: batch insert failed ({len(batch)} events lost, "
                f"recorded as a gap once writes recover): {exc}"
            )


async def _drain() -> None:
    """Background loop: batch queued records into Postgres."""
    assert _queue is not None
    batch: list[tuple[str, str, dict]] = []
    while True:
        try:
            try:
                item = await asyncio.wait_for(_queue.get(), timeout=_FLUSH_INTERVAL)
                batch.append(item)
                while len(batch) < _BATCH_MAX and not _queue.empty():
                    batch.append(_queue.get_nowait())
            except TimeoutError:
                pass
            if batch:
                await _flush(batch)
                batch = []
        except asyncio.CancelledError:
            break
        except Exception as exc:  # the drain loop must survive anything
            logger.warning(f"flight_recorder: drain error: {exc}")
            batch = []


# ── Read path (replay) ────────────────────────────────────────────────────────

async def fetch_episode(episode_id: str) -> list[dict]:
    """Return all decision_log rows for an episode, ordered by seq."""
    if _pool is None:
        return []
    try:
        records = await _pool.fetch(
            "SELECT episode_id, seq, kind, payload, prev_hash, hash, created_at"
            " FROM decision_log WHERE episode_id = $1 ORDER BY seq",
            episode_id,
        )
    except Exception as exc:
        logger.warning(f"flight_recorder: fetch failed: {exc}")
        return []
    return [dict(r) for r in records]
