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

def _scrub(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact secrets from string fields and cap their length (R8 discipline)."""
    if not settings.REFLEXION_REDACT_SECRETS:
        return payload
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str) and value:
            out[key] = redact_secrets(value, max_chars=_MAX_FIELD_CHARS)
        else:
            out[key] = value
    return out


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
    """Return (next_seq, last_hash), loading from the DB on first encounter."""
    if episode_id in _chains:
        return _chains[episode_id]
    next_seq, last_hash = 0, ""
    if _pool is not None:
        try:
            row = await _pool.fetchrow(
                "SELECT seq, hash FROM decision_log"
                " WHERE episode_id = $1 ORDER BY seq DESC LIMIT 1",
                episode_id,
            )
            if row:
                next_seq, last_hash = row["seq"] + 1, row["hash"]
        except Exception as exc:
            logger.warning(f"flight_recorder: chain-state lookup failed: {exc}")
    _chains[episode_id] = (next_seq, last_hash)
    return next_seq, last_hash


async def _flush(items: list[tuple[str, str, dict]]) -> None:
    if not items or _pool is None:
        return
    rows = []
    for episode_id, kind, payload in items:
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
        rows.append((episode_id, seq, kind, json.dumps(payload, default=str), prev_hash, digest,
                     trace_id, span_id, parent_span_id))
    try:
        await _pool.executemany(
            """
            INSERT INTO decision_log
                (episode_id, seq, kind, payload, prev_hash, hash, trace_id, span_id, parent_span_id)
            VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9)
            """,
            rows,
        )
    except Exception as exc:
        msg = str(exc)
        if "decision_log" in msg and "does not exist" in msg:
            logger.warning("flight_recorder: 'decision_log' table missing — run: kubeintellect db-init")
        else:
            logger.warning(f"flight_recorder: batch insert failed ({len(rows)} rows): {exc}")


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
            except asyncio.TimeoutError:
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
