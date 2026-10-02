"""
Effect log — the exactly-once ledger of irreversible tool calls (ADR-008).

The flight recorder (`app/db/flight_recorder.py`) answers *what happened*, after the fact, and
its write path is deliberately fire-and-forget: a recorder outage degrades auditability, never
availability. That is the wrong discipline for the question this module answers — *has this
irreversible call already run at this rollback point?* — because the answer has to be read
**before** the call executes, and a write that might not have landed cannot be the thing that
stops a second `kubectl delete pvc`. So this ledger is synchronous: every read and append is a
committed Postgres transaction, serialised per session by an advisory lock, and any failure
raises :class:`EffectLogUnavailable` so the caller can fall back to HITL re-approval instead of
guessing (ADR-008: fail-closed on the irreversible path).

Tamper evidence is the flight recorder's own format, not a second one — including its head
anchor: `effect_log_head` (the `decision_log_head` pattern) is advanced in the same transaction
as every append, and every transaction checks the ledger against it, so truncating the newest
rows fails closed to HITL as "effect log tampered/truncated". Rows are hash-chained per
session with :func:`app.db.flight_recorder.compute_hash` (``episode_id`` = ``session_id``), the
binding columns are projections of fields that also live in the hashed ``payload``, and the
table refuses UPDATE/DELETE at the database (`schema.sql`). Every append is also mirrored into
the decision log as an ``effect_log`` event, so a replay of the episode shows the same story.

The decision logic lives in `app/tools/effect_guard.py`. This module only stores rows and hands
the guard a locked, consistent view of one session's ledger (:class:`EffectTxn`).
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from app.core.config import settings
from app.db.flight_recorder import compute_hash
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Row kinds. A token is *issued* before a human is asked, *consumed* exactly once when the
#: approval is used; an *intent* is the claim written before execution, an *effect* the result
#: written after it. An intent with no effect is an outcome nobody knows — never replayed,
#: always re-approved.
APPROVAL_ISSUED = "approval_issued"
APPROVAL_CONSUMED = "approval_consumed"
INTENT = "intent"
EFFECT = "effect"
FORK = "fork"
REFUSED = "refused"
KINDS = frozenset({APPROVAL_ISSUED, APPROVAL_CONSUMED, INTENT, EFFECT, FORK, REFUSED})

#: Name of the decision-log event every append is mirrored as.
MIRROR_KIND = "effect_log"


class EffectLogUnavailable(RuntimeError):
    """The effect log could not be read or written. Never the same as "no prior effect"."""


class EffectTxn(Protocol):
    """One session's ledger, locked for the duration of the transaction."""

    rows: list[dict[str, Any]]

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one row (binding fields are read from ``payload``) and return it."""
        ...


class EffectStore(Protocol):
    def transaction(self, session_id: str) -> Any:   # a context manager yielding an EffectTxn
        ...


def _binding_columns(payload: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(payload.get("rollback_point", "-")),
        str(payload.get("branch_id", "main")),
        str(payload.get("tool", "")),
        str(payload.get("canonical_key", "")),
    )


_SQL_HEAD_UPSERT = """
    INSERT INTO effect_log_head (session_id, seq, hash, updated_at)
    VALUES (%s, %s, %s, now())
    ON CONFLICT (session_id) DO UPDATE
        SET seq = EXCLUDED.seq, hash = EXCLUDED.hash, updated_at = now()
"""
_SQL_HEAD_READ = "SELECT seq, hash FROM effect_log_head WHERE session_id = %s"

TAMPERED = "effect log tampered/truncated"


def head_problem(rows: list[dict[str, Any]], head: tuple[int, str] | None) -> str | None:
    """Why a session's ledger contradicts its anchor, or None when they agree.

    `verify` proves links; it cannot see rows removed from the END of the chain, because the
    surviving prefix still hashes correctly. The head is written in the same transaction as
    every append, so — unlike the decision-log head — there is no legitimate lag: *any*
    disagreement (no head for a non-empty ledger, a head for an empty one, a different seq or
    hash) is tampering or a bypassed writer, and the caller must not trust the ledger.
    """
    if head is None:
        return f"{TAMPERED}: {len(rows)} ledger row(s) but no head anchor" if rows else None
    head_seq, head_hash = head
    if not rows:
        return f"{TAMPERED}: head records seq={head_seq} but the ledger is empty"
    last = rows[-1]
    if int(last["seq"]) != head_seq or str(last["hash"]) != head_hash:
        return (f"{TAMPERED}: ledger ends at seq={last['seq']} but its head records "
                f"seq={head_seq}")
    return None


class _PgTxn:
    def __init__(self, cur, session_id: str, rows: list[dict[str, Any]]):
        self._cur = cur
        self._session_id = session_id
        self.rows = rows

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"unknown effect_log kind {kind!r}")
        # Round-trip through JSON first so the hash is computed over exactly what is stored.
        payload = json.loads(json.dumps(payload, default=str))
        last = self.rows[-1] if self.rows else None
        seq = int(last["seq"]) + 1 if last else 0
        prev_hash = str(last["hash"]) if last else ""
        digest = compute_hash(prev_hash, self._session_id, seq, kind, payload)
        rp, branch, tool, key = _binding_columns(payload)
        try:
            self._cur.execute(
                """
                INSERT INTO effect_log
                    (session_id, seq, kind, rollback_point, branch_id, tool, canonical_key,
                     payload, prev_hash, hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                RETURNING created_at
                """,
                (self._session_id, seq, kind, rp, branch, tool, key,
                 json.dumps(payload), prev_hash, digest),
            )
            created = self._cur.fetchone()
        except Exception as exc:
            raise EffectLogUnavailable(f"effect_log append failed: {exc}") from exc
        row = {
            "session_id": self._session_id, "seq": seq, "kind": kind, "payload": payload,
            "prev_hash": prev_hash, "hash": digest,
            "created_at": str(created[0]) if created else "",
        }
        try:
            # Same transaction as the row: the anchor advances atomically with the ledger.
            self._cur.execute(_SQL_HEAD_UPSERT, (self._session_id, seq, digest))
        except Exception as exc:
            raise EffectLogUnavailable(f"effect_log head write failed: {exc}") from exc
        self.rows.append(row)
        _mirror(self._session_id, row)
        return row


class PostgresEffectStore:
    """Synchronous store: the tool path is sync and must know the answer before it executes."""

    def __init__(self, dsn: str, *, connect_timeout: int = 3):
        self._dsn = dsn
        self._connect_timeout = connect_timeout

    @contextmanager
    def transaction(self, session_id: str) -> Iterator[_PgTxn]:
        try:
            import psycopg  # type: ignore[import-untyped]
            conn = psycopg.connect(self._dsn, connect_timeout=self._connect_timeout)
        except Exception as exc:
            raise EffectLogUnavailable(f"effect_log: could not connect: {exc}") from exc
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    try:
                        # Serialise every decision about one session: two parallel tool calls
                        # must not both read "no prior effect" and both execute.
                        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                    (f"effect_log:{session_id}",))
                        cur.execute(
                            "SELECT seq, kind, payload, prev_hash, hash, created_at"
                            " FROM effect_log WHERE session_id = %s ORDER BY seq",
                            (session_id,),
                        )
                        rows = []
                        for seq, kind, payload, prev_hash, digest, created_at in cur.fetchall():
                            if isinstance(payload, str):
                                payload = json.loads(payload)
                            rows.append({
                                "session_id": session_id, "seq": int(seq), "kind": kind,
                                "payload": payload, "prev_hash": prev_hash, "hash": digest,
                                "created_at": str(created_at),
                            })
                    except Exception as exc:
                        raise EffectLogUnavailable(f"effect_log read failed: {exc}") from exc
                    try:
                        cur.execute(_SQL_HEAD_READ, (session_id,))
                        head_row = cur.fetchone()
                    except Exception as exc:
                        # An anchor that cannot be read cannot clear the ledger: fail closed.
                        raise EffectLogUnavailable(f"effect_log head read failed: {exc}") from exc
                    problem = head_problem(
                        rows, (int(head_row[0]), str(head_row[1])) if head_row else None)
                    if problem:
                        logger.error(f"effect_log: session {session_id!r}: {problem}")
                        raise EffectLogUnavailable(f"session {session_id!r}: {problem}")
                    if not verify(rows):
                        # A ledger that does not verify cannot be the evidence that an
                        # irreversible call already ran — or that it did not.
                        raise EffectLogUnavailable(
                            f"effect_log chain for session {session_id!r} does not verify"
                        )
                    yield _PgTxn(cur, session_id, rows)
        except EffectLogUnavailable:
            raise
        except Exception as exc:
            raise EffectLogUnavailable(f"effect_log transaction failed: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:
                pass


def verify(rows: list[dict[str, Any]]) -> bool:
    """Recompute the per-session chain (the flight recorder's link check, same format)."""
    prev, expected = "", 0
    for row in rows:
        if int(row["seq"]) != expected or str(row["prev_hash"]) != prev:
            return False
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if compute_hash(prev, row["session_id"], int(row["seq"]), row["kind"], payload) != row["hash"]:
            return False
        prev = str(row["hash"])
        expected += 1
    return True


def get_store() -> EffectStore | None:
    """The configured store, or None when there is nowhere durable to keep the ledger.

    None is not "nothing has run": the guard treats it as unavailable and falls back to HITL
    re-approval. The ledger lives beside the flight recorder, so it needs the same two things
    the recorder does — the recorder switched on, and Postgres.
    """
    if not settings.FLIGHT_RECORDER_ENABLED or settings.USE_SQLITE:
        return None
    dsn = settings.POSTGRES_DSN
    return PostgresEffectStore(dsn) if dsn else None


def _mirror(session_id: str, row: dict[str, Any]) -> None:
    """Copy one ledger row into the decision log. Best-effort: the ledger is authoritative."""
    try:
        from app.db import flight_recorder
        flight_recorder.record(session_id, MIRROR_KIND, {
            "type": MIRROR_KIND,
            "effect_kind": row["kind"],
            "effect_seq": row["seq"],
            "effect_hash": row["hash"],
            **{k: v for k, v in row["payload"].items() if k != "result"},
        })
    except Exception as exc:
        logger.warning(f"effect_log: decision-log mirror failed (non-fatal): {exc}")
