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
* That invariant used to have one hole, and it was the largest loss mode of all:
  a recorder that **never started**. ``init_recorder`` gave up after one failed
  connect, so there was no queue, no drain and no flush — and the entire gap
  ledger hung off a flush. Every event was dropped by a bare ``return`` with no
  marker, no counter and no retry, for the life of the process. Measured: three
  events recorded against a never-started recorder left ``_pending_gaps`` empty,
  where the same loss mid-flight leaves ``{'ep': (1, reason)}``. The pool is now
  retried, and a loss with no queue is carried exactly like a failed flush, so
  the outage is written into the chain once recording resumes.

Proven against a real Postgres (2026-08-20): before this, an outage followed by
a process restart produced six rows, seq 0-5, ``chain_valid=True`` — with three
recorded events silently gone.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, NamedTuple

import asyncpg

from app.core.config import settings
from app.db import chain_truncation
from app.utils.logger import get_logger
from app.utils.redact import redact_identifier, redact_secrets

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
_reconnect_task: asyncio.Task | None = None
# Per-episode chain state: episode_id -> (next_seq, last_hash).
_chains: dict[str, tuple[int, str]] = {}
# Unwritten loss, carried until it can be recorded: episode_id -> (events_lost, first_reason).
_pending_gaps: dict[str, tuple[int, str]] = {}

# The kind used to record loss inside the chain itself. A replay that contains one of
# these is intact *and* incomplete — the two are different answers.
GAP_KIND = "recorder_gap"

_BATCH_MAX = 50
_FLUSH_INTERVAL = 0.5  # seconds

# "starting" until init runs; "flag"/"sqlite" when recording is off by configuration;
# "ready" once the pool and drain task are live; "unavailable" while Postgres refuses us.
_state: str = "starting"
_reason: str = ""
#: Events lost while the recorder was not running, in total. `_pending_gaps` holds the same
#: loss per episode so it can be written *into* the chain; this is the number an operator asks
#: for, and it survives the flush that clears them.
_lost_while_down: int = 0
#: Ceiling on how many distinct episodes carry an unwritten gap. A long outage must not turn
#: the gap ledger into an unbounded leak; overflow is still counted in `_lost_while_down`.
_MAX_TRACKED_EPISODES = 1000

#: Seconds between reconnect attempts while the pool is down.
_RETRY_INTERVAL_S = 30.0


def recorder_status() -> dict:
    """Shape reported on ``/healthz``. An operator must be able to see that nothing is recorded.

    ``lost_while_down`` is the count of events that were never persisted because the recorder
    was not running. It is the answer to "is this episode's replay the whole story?" *before*
    anyone asks for a specific episode.
    """
    return {
        "enabled": _state == "ready",
        "state": _state,
        "reason": _reason,
        "lost_while_down": _lost_while_down,
    }


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


_SQL_HEAD_UPSERT = """
    INSERT INTO decision_log_head (episode_id, seq, hash, updated_at)
    VALUES ($1, $2, $3, now())
    ON CONFLICT (episode_id) DO UPDATE SET seq = $2, hash = $3, updated_at = now()
"""
_SQL_HEAD_READ = "SELECT seq, hash FROM decision_log_head WHERE episode_id = $1"


def verify_chain(rows: list[dict], *, start_seq: int = 0, start_prev_hash: str = "") -> bool:
    """Recompute the hash chain over rows (ordered by seq). True iff every link verifies.

    **This is the link check only, and it cannot see a truncation.** Deleting an episode's
    newest rows leaves a shorter chain in which every link still verifies — measured
    2026-08-24, not assumed. Callers that present a tamper verdict to a human must also call
    `head_agrees`, which compares the surviving chain against the persisted anchor.

    `start_seq` / `start_prev_hash` say where the chain is *expected* to begin. The defaults
    are the whole chain from its origin, so every existing caller keeps its exact behaviour —
    including the one that matters most: a chain whose first rows are gone still fails. They
    are non-default only for a chain whose front was removed **deliberately**, where the
    seq and prev_hash come from a `chain_truncation` record rather than from the caller's
    opinion (see `app/db/chain_truncation.py`). A caller that passes these from anywhere else
    is not verifying a chain, it is agreeing with one.
    """
    prev = start_prev_hash
    expected_seq = start_seq
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


class ChainVerdict(NamedTuple):
    """A tamper verdict with the third state the booleans could not hold.

    ``valid`` answers *did anything contradict the records* — a broken link, or an anchor that
    remembers events these rows do not have. ``verified`` answers *was the question actually
    asked*. They are separate because the anchor read can fail — a permissions error, a partial
    migration, a head row this build cannot parse — and until 2026-08-24 all three of those
    returned the same ``True`` an intact chain returns. This module logged
    "truncation of this episode is NOT currently detectable" and handed its caller the value
    that renders ``✓ chain intact``.

    ``valid`` keeps its meaning when ``verified`` is False: nothing contradicted the records —
    but nothing could have, so it is not evidence. Both renderers already own a three-state
    vocabulary for this (`kq replay` exit ``4``, the postmortem banner's `chain_verified`);
    only the function producing the verdict did not.
    """

    valid: bool
    verified: bool


async def head_verdict(pool, episode_id: str, rows: list[dict]) -> ChainVerdict:
    """Compare an episode's surviving chain against its persisted head.

    Mirrors `app.memory.security._head_agrees` — the same anchor, for the same reason, on the
    chain that renders a banner to a human.

    A **missing** head is `verified`: the anchor was read, and there is none to contradict
    these rows (an episode written before this anchor existed). That is a performed check with
    a benign answer, and it is deliberately not the same as the head read *failing*.
    """
    if pool is None:
        # No store to ask. The link check still ran, but truncation is undetectable without
        # the anchor, so this is not a verification.
        return ChainVerdict(True, False)
    try:
        head = await pool.fetchrow(_SQL_HEAD_READ, episode_id)
    except Exception as exc:
        logger.warning(f"flight_recorder: chain head read failed for {episode_id!r}: {exc}")
        return ChainVerdict(True, False)
    if head is None:
        return ChainVerdict(True, True)
    try:
        head_seq, head_hash = int(head["seq"]), str(head["hash"])
    except (KeyError, TypeError, ValueError) as exc:
        # Schema drift, a partial migration, a row we cannot interpret. Same doctrine as an
        # unreadable head: not evidence of tampering — but never silent, because a verifier
        # that quietly answers "intact" whenever it fails to parse its own anchor is the exact
        # failure this anchor exists to prevent.
        logger.warning(
            f"flight_recorder: chain head for {episode_id!r} is present but unreadable "
            f"({exc!r}) — truncation of this episode is NOT currently detectable"
        )
        return ChainVerdict(True, False)
    if not rows:
        logger.warning(
            f"flight_recorder: episode {episode_id!r} has no events but its head records "
            f"seq={head_seq} — every event has been removed"
        )
        return ChainVerdict(False, True)
    last = rows[-1]
    if int(last["seq"]) < head_seq:
        logger.warning(
            f"flight_recorder: episode {episode_id!r} ends at seq={last['seq']} but its head "
            f"records seq={head_seq} — newest events have been removed"
        )
        return ChainVerdict(False, True)
    if int(last["seq"]) > head_seq:
        # Rows the head never saw: a crash between the two writes, or an append that bypassed
        # this module. Not truncation — do not cry tamper, but do not stay silent either.
        logger.warning(
            f"flight_recorder: episode {episode_id!r} is ahead of its head "
            f"({last['seq']} > {head_seq}) — head write lost, or an append bypassed it"
        )
        return ChainVerdict(True, True)
    return ChainVerdict(str(last["hash"]) == head_hash, True)


async def head_agrees(pool, episode_id: str, rows: list[dict]) -> bool:
    """The boolean face of :func:`head_verdict` — *did anything contradict these rows*.

    Kept for callers that genuinely want two states. It cannot tell a verified agreement from
    an anchor nobody could read, so nothing that renders a verdict to a human should use it.
    """
    return (await head_verdict(pool, episode_id, rows)).valid


async def verify_episode(episode_id: str, rows: list[dict]) -> ChainVerdict:
    """The full tamper verdict for one episode: links **and** the truncation anchor.

    `verify_chain` alone answers a narrower question than every caller was asking it. Anything
    that renders a verdict should call this — and must render ``verified`` as well as
    ``valid``, because a chain nobody could check is not a chain that checked out.
    """
    if verify_chain(rows):
        return await head_verdict(_pool, episode_id, rows)
    # The links did not recompute from the origin. That is a performed check with a positive
    # finding *unless* the front of this chain was removed on purpose — which is only possible
    # to say because a truncation has to be declared, in a second place, with the hash of an
    # archive holding the removed rows. Note what is NOT reached here: a chain that still
    # starts at seq 0 takes no lookup at all, so the ordinary verdict path is untouched and
    # cannot be weakened by this table being absent, unreadable, or empty.
    if not rows or int(rows[0]["seq"]) == 0:
        return ChainVerdict(False, True)
    declared = await chain_truncation.declared_start(
        _pool, chain="decision_log", scope_id=episode_id)
    if not declared.read:
        logger.warning(
            f"flight_recorder: episode {episode_id!r} does not start at seq 0 and the "
            f"truncation record could not be read — this chain is NOT verified; that is "
            f"neither an accusation nor an all-clear"
        )
        return ChainVerdict(True, False)
    if not declared.found:
        logger.warning(
            f"flight_recorder: episode {episode_id!r} starts at seq={rows[0]['seq']} with no "
            f"recorded truncation — its earliest events were removed"
        )
        return ChainVerdict(False, True)
    if int(rows[0]["seq"]) != declared.seq or str(rows[0]["prev_hash"]) != declared.prev_hash:
        # A record exists but does not describe these rows. Worse than no record: something
        # claims this gap is accounted for and the rows say otherwise.
        logger.warning(
            f"flight_recorder: episode {episode_id!r} has a truncation record resuming at "
            f"seq={declared.seq} but the surviving chain starts at seq={rows[0]['seq']} — "
            f"the record does not describe these rows"
        )
        return ChainVerdict(False, True)
    if not verify_chain(rows, start_seq=declared.seq, start_prev_hash=declared.prev_hash):
        return ChainVerdict(False, True)
    return await head_verdict(_pool, episode_id, rows)


# ── Payload hygiene ───────────────────────────────────────────────────────────

# How deep `_scrub` will walk. Recorded payloads are shallow; the bound exists so a
# self-referential structure cannot hang the drain task, not because depth is expected.
_MAX_SCRUB_DEPTH = 6


# What replaces a subtree the walk refuses to descend into. A depth bound that returns the
# subtree *unredacted* is a bound that fails open, which is the direction this module must
# never fail in — the payload is persisted, and permanently.
_TOO_DEEP = "<redacted-unscannable-depth>"


def _scrub_key(key: Any, seen: set) -> Any:
    """Redact a dict key, keeping distinct keys distinct.

    Two different keys can redact to the same string — two bearer tokens both become
    `<redacted-token>` — and a dict comprehension would then silently drop one of them. Losing a
    field from a tamper-evident record to *hide* something in it is the wrong trade, so a
    collision is disambiguated rather than merged.
    """
    if not isinstance(key, str):
        return key
    scrubbed = redact_identifier(key)
    if scrubbed == key:
        return key
    candidate, n = scrubbed, 1
    while candidate in seen:
        n += 1
        candidate = f"{scrubbed}#{n}"
    seen.add(candidate)
    return candidate


def _scrub_value(value: Any, cap: int | None, depth: int = 0) -> Any:
    """Redact every string *anywhere* in a payload, not only the ones at the top.

    "Anywhere" is meant literally, and until 2026-08-24 it was not. Three shapes passed through
    verbatim into the persisted record while the flag said secrets were being scrubbed:

    * **a dict key** — `{"attributes": {"kubectl … --token=AKIA…": "ok"}}` kept the token at any
      depth, because only values were walked;
    * **anything nested past the depth bound** — at 8 levels the whole remaining subtree was
      returned untouched, so the bound that exists to stop a cycle also stopped the redaction;
    * **a set or frozenset** — no branch matched it, and `json.dumps(..., default=str)` then
      wrote `str(the_set)` — the secret — straight into the column.

    Keys use `redact_identifier`, not `redact_secrets`: the latter turns the ordinary field
    names `token` and `password` into the *same* drop marker, which would merge two fields of an
    audit record into one.
    """
    if isinstance(value, str):
        return redact_secrets(value, max_chars=cap) if value else value
    if depth >= _MAX_SCRUB_DEPTH:
        # Not `return value`. Anything still nested here has never been looked at.
        return _TOO_DEEP if isinstance(value, (dict, list, tuple, set, frozenset)) else value
    if isinstance(value, dict):
        # Nested strings are redacted but NOT re-capped: `_MAX_FIELD_CHARS` is this module's
        # limit on one payload *field*, and a nested string arrives already capped by whoever
        # produced it (a `rollback_point` pre-state at 4000 chars, say). Shrinking it here
        # would quietly cost a capture its restorability to enforce a limit nothing asked for.
        seen: set = set()
        return {_scrub_key(k, seen): _scrub_value(v, None, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v, None, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        # Serialised by `default=str` downstream, so an unscrubbed set reaches the column as
        # its repr. Sorted so the same set does not produce two different chain hashes.
        return sorted(
            (_scrub_value(v, None, depth + 1) for v in value), key=str
        )
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
    # The top-level keys go through `_scrub_key` for the same reason the nested ones do. They
    # are usually this module's own field names (`attributes`, `steps`, `rollback_point`), which
    # `redact_identifier` leaves untouched — but `record()` takes a caller's dict, and "usually"
    # is not a property the redaction gate may depend on.
    seen: set = set()
    return {_scrub_key(k, seen): _scrub_value(v, _MAX_FIELD_CHARS) for k, v in payload.items()}


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def init_recorder() -> None:
    """Create the pool and start the drain task. A failed connect retries; it does not give up."""
    global _state, _reason, _reconnect_task
    if not settings.FLIGHT_RECORDER_ENABLED:
        _state, _reason = "flag", "FLIGHT_RECORDER_ENABLED=false"
        logger.info("flight_recorder: disabled by flag")
        return
    if settings.USE_SQLITE:
        _state, _reason = "sqlite", "USE_SQLITE=true — recording needs Postgres"
        logger.info("flight_recorder: SQLite mode — recording disabled")
        return
    if await _try_connect():
        return
    # Not final: keep one task alive whose only job is to try again. Until it succeeds every
    # `record()` is carried as a pending gap, so the outage lands *in* the chain afterwards.
    _reconnect_task = asyncio.get_running_loop().create_task(_reconnect_loop())


async def _try_connect() -> bool:
    """One connect attempt; on success the queue and drain task start. Never raises."""
    global _pool, _queue, _drain_task, _state, _reason
    try:
        _pool = await asyncpg.create_pool(
            settings.POSTGRES_DSN,
            min_size=1,
            max_size=3,
            command_timeout=5,
        )
    except Exception as exc:
        _pool = None
        _state, _reason = "unavailable", str(exc)
        logger.warning(
            f"flight_recorder: could not connect — nothing is being recorded; retrying every "
            f"{_RETRY_INTERVAL_S:.0f}s ({exc})"
        )
        return False
    _queue = asyncio.Queue()
    _drain_task = asyncio.get_running_loop().create_task(_drain())
    _state, _reason = "ready", ""
    logger.info("flight_recorder: ready")
    return True


async def _reconnect_loop() -> None:
    """Retry until Postgres accepts us, then stop. A rollout race must not cost a restart."""
    while True:
        try:
            await asyncio.sleep(_RETRY_INTERVAL_S)
            if await _try_connect():
                if _lost_while_down:
                    logger.warning(
                        f"flight_recorder: recovered; {_lost_while_down} event(s) were lost "
                        f"while it was down and are written into the chain as "
                        f"{GAP_KIND!r} record(s)"
                    )
                return
        except asyncio.CancelledError:
            break
        except Exception as exc:      # the retry loop itself must never die
            logger.warning(f"flight_recorder: reconnect attempt failed: {exc}")


async def close_recorder() -> None:
    global _pool, _queue, _drain_task, _reconnect_task, _state, _reason
    _state, _reason = "starting", ""
    if _reconnect_task:
        _reconnect_task.cancel()
        _reconnect_task = None
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
    if kind in _SKIP_KINDS:
        return
    if _queue is None:
        # The recorder is not running. Off by flag or in SQLite mode there is no chain to be
        # honest about, so there is nothing to carry. But a *failed* recorder losing events is
        # exactly the case this module's invariant exists for — "every loss is carried forward
        # and written into the chain" — and it used to be the one loss mode that produced no
        # evidence at all, because the whole gap ledger hung off a flush that never happened.
        if _state == "unavailable":
            _note_loss(episode_id, kind, payload, _reason or "the recorder was not running")
        return
    try:
        _queue.put_nowait((episode_id, kind, payload))
    except Exception as exc:
        # Dropping rather than blocking the request path is correct; dropping *silently* is
        # not — this bypasses the flush, so it is the same hole one level down.
        _note_loss(episode_id, kind, payload, _drop_reason(exc))


def _note_loss(episode_id: str, kind: str, payload: dict[str, Any], reason: str) -> None:
    """Carry an event that never reached the queue, so the next successful flush writes it
    into the chain as a gap. Bounded: overflow is counted but not tracked per episode."""
    global _lost_while_down
    _lost_while_down += 1
    if episode_id in _pending_gaps or len(_pending_gaps) < _MAX_TRACKED_EPISODES:
        _carry_loss([(episode_id, kind, payload)], reason)
    if _lost_while_down == 1 or _lost_while_down % 1000 == 0:
        logger.warning(
            f"flight_recorder: {_lost_while_down} event(s) not recorded — {_state}: {_reason}"
        )


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
        # If the head is ahead of the surviving rows, events were removed since the last
        # append. Continue *past* the head rather than reusing consumed numbers: re-anchoring
        # would heal the chain and erase the only evidence the truncation ever happened.
        # Same remedy, same reasoning as app/memory/security.py::_audit_state.
        try:
            head = await _pool.fetchrow(_SQL_HEAD_READ, episode_id)
        except Exception as exc:
            logger.warning(f"flight_recorder: chain head read failed for {episode_id!r}: {exc}")
            head = None
        if head is not None and int(head["seq"]) + 1 > next_seq:
            logger.warning(
                f"flight_recorder: episode {episode_id!r} resumes at seq={next_seq} but its "
                f"head records seq={head['seq']} — events were removed; continuing past the "
                f"head so the gap stays visible to verify_chain"
            )
            next_seq = int(head["seq"]) + 1
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
        # Anchor each episode's newest row. Separate try: the events are already durable, and
        # a head we failed to write must never turn into a lost batch. A lagging head reads as
        # "ahead of its head" on verify, which warns and does NOT cry tamper.
        try:
            heads: dict[str, tuple[int, str]] = {}
            for r in rows:
                episode_id, seq, digest = r[0], r[1], r[5]
                if episode_id not in heads or seq > heads[episode_id][0]:
                    heads[episode_id] = (seq, digest)
            await _pool.executemany(
                _SQL_HEAD_UPSERT, [(eid, seq, h) for eid, (seq, h) in heads.items()]
            )
        except Exception as exc:
            logger.warning(
                f"flight_recorder: chain-head anchor write failed for {len(heads)} episode(s) "
                f"({exc}); the events themselves are persisted, but a truncation of them would "
                f"not be detectable until the next successful flush re-anchors"
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

class RecorderUnavailable(RuntimeError):
    """The decision log could not be read — not the same as an episode having no rows.

    `fetch_episode` returned `[]` for three unrelated states: the episode really has no rows,
    the recorder was never started (`_pool is None`), and the query failed. The API turned all
    three into `404 no recorded episode '<id>'` — the one answer that is a positive claim about
    the world. An operator asking for the audit trail of an incident they are living through was
    told the episode does not exist, when the truth was that the recorder was off.

    Sibling of `memory_store.MemoryStoreUnavailable` and `episodes.MemoryUnavailable`: the
    unreadable case gets its own exception so a caller cannot render it as absence by accident.
    """


async def fetch_episode(episode_id: str) -> list[dict]:
    """Return all decision_log rows for an episode, ordered by seq.

    Raises `RecorderUnavailable` if the log could not be read at all. An empty list means the
    episode has no recorded rows, and nothing else.
    """
    if _pool is None:
        raise RecorderUnavailable(
            "the flight recorder is not running — no decision log to read"
        )
    try:
        records = await _pool.fetch(
            "SELECT episode_id, seq, kind, payload, prev_hash, hash, created_at"
            " FROM decision_log WHERE episode_id = $1 ORDER BY seq",
            episode_id,
        )
    except Exception as exc:
        logger.warning(f"flight_recorder: fetch failed: {exc}")
        raise RecorderUnavailable(f"the decision log could not be read: {exc}") from exc
    return [dict(r) for r in records]
