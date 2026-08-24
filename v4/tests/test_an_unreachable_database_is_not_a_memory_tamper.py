"""One function gave a database failure two opposite verdicts, and one of them was the alarm.

`verify_memory_chain` answered *"is this cluster's memory audit chain intact?"* with a `bool`,
so it had two values for three states. Measured 2026-08-24, against the real append path:

    database unreachable        -> False      # the value that means TAMPERED
    anchor read raised          -> True       # the value that means INTACT
    no pool at all              -> True       # the value that means INTACT
    unparseable anchor row      -> raised ValueError

The first line is the serious one. A tamper detector that reports tampering whenever its own
Postgres is unreachable is making a false accusation about the operator's own data, and the
cost is not the single wrong answer — it is that operators learn to ignore the alarm, which
takes the real one down with it. This file's sibling
(`test_truncating_the_memory_audit_leaves_a_trace.py`) already owns that rule for the anchor:
*"Tamper-evidence is worthless if operators learn to ignore it."* The fetch path had the
opposite reflex to the head path, in the same function.

The last line is its own defect: every other failure produced a verdict, and a head row this
build cannot parse produced an exception instead, out of a bare `int(head["seq"])`.

The fix is the vocabulary `flight_recorder` already owns (`ChainVerdict(valid, verified)`,
added 2026-08-24 for the chain the postmortem renders). `valid` keeps its meaning — *nothing
contradicted these rows* — and now travels with whether anything could have. Same question,
same type, both chains.

Deliberately unchanged: a **missing** head stays `verified`. The anchor was read and there is
none — a chain written before the anchor existed. That is a check that ran.
"""

from __future__ import annotations

import pytest

from app.db.flight_recorder import ChainVerdict
from app.memory import security


def chain(n: int, cluster_id: str = "c1") -> list[dict]:
    rows, prev = [], ""
    for seq in range(n):
        payload = {"reason": f"e{seq}"}
        h = security._compute_audit_hash(prev, cluster_id, seq, "episode_write", payload)
        rows.append({"seq": seq, "kind": "episode_write", "payload": payload,
                     "prev_hash": prev, "hash": h})
        prev = h
    return rows


class Pool:
    """Rows and anchor answered independently, so a failure can be isolated to one of them."""

    def __init__(self, rows, head=None, fetch_exc=None, head_exc=None):
        self.rows, self.head = rows, head
        self.fetch_exc, self.head_exc = fetch_exc, head_exc

    async def fetch(self, *_a):
        if self.fetch_exc:
            raise self.fetch_exc
        return self.rows

    async def fetchrow(self, *_a):
        if self.head_exc:
            raise self.head_exc
        return self.head


def anchor(rows: list[dict]) -> dict:
    return {"seq": rows[-1]["seq"], "hash": rows[-1]["hash"]}


ROWS = chain(3)
GOOD = anchor(ROWS)


# ── 1. nothing checked is neither intact nor tampered ─────────────────────────────────────────


class TestAFailureIsNotAFinding:
    async def test_an_unreachable_database_is_not_a_tamper(self):
        """The headline: this returned the value that means TAMPERED."""
        v = await security.verify_memory_chain(
            Pool(ROWS, GOOD, fetch_exc=OSError("connection refused to postgres:5432")), "c1")
        assert v.valid is True, "an infrastructure failure was reported as tampering"
        assert v.verified is False, "and it must not be reported as a completed check either"

    async def test_an_unreadable_anchor_is_not_verified(self):
        v = await security.verify_memory_chain(
            Pool(ROWS, GOOD, head_exc=RuntimeError("permission denied for memory_chain_head")),
            "c1")
        assert v == ChainVerdict(valid=True, verified=False)

    async def test_no_pool_is_not_verified(self):
        assert await security.verify_memory_chain(None, "c1") == ChainVerdict(True, False)

    async def test_an_unparseable_anchor_returns_a_verdict_instead_of_raising(self):
        """It escaped as a ValueError out of a bare `int(head["seq"])` — the one failure mode
        that reached the caller as an exception while every other one got an answer."""
        v = await security.verify_memory_chain(Pool(ROWS, {"seq": "not-an-int", "hash": None}),
                                               "c1")
        assert v == ChainVerdict(True, False)

    @pytest.mark.parametrize("head", [{}, {"seq": 2}, {"seq": None, "hash": "x"}])
    async def test_other_shapes_of_anchor_drift_do_not_raise_either(self, head):
        v = await security.verify_memory_chain(Pool(ROWS, head), "c1")
        assert v == ChainVerdict(True, False)


# ── 2. the alarms still fire ──────────────────────────────────────────────────────────────────


class TestTheAlarmsStillFire:
    """Vacuity guard: a verifier that never reports a tamper passes every test above."""

    async def test_an_intact_chain_is_valid_and_verified(self):
        assert await security.verify_memory_chain(Pool(ROWS, GOOD), "c1") == ChainVerdict(True, True)

    async def test_newest_entries_removed_is_still_a_tamper(self):
        v = await security.verify_memory_chain(Pool(ROWS[:1], GOOD), "c1")
        assert v == ChainVerdict(valid=False, verified=True)

    async def test_every_entry_removed_is_still_a_tamper(self):
        v = await security.verify_memory_chain(Pool([], GOOD), "c1")
        assert v == ChainVerdict(valid=False, verified=True)

    async def test_a_broken_link_is_still_a_tamper(self):
        rows = [dict(r) for r in ROWS]
        rows[1]["kind"] = "forget"
        v = await security.verify_memory_chain(Pool(rows, GOOD), "c1")
        assert v == ChainVerdict(valid=False, verified=True)

    async def test_a_forged_head_hash_is_still_a_tamper(self):
        v = await security.verify_memory_chain(
            Pool(ROWS, {"seq": ROWS[-1]["seq"], "hash": "0" * 64}), "c1")
        assert v == ChainVerdict(valid=False, verified=True)


# ── 3. the two states that are checks, not failures ───────────────────────────────────────────


class TestACheckThatRanStaysVerified:
    async def test_a_missing_anchor_is_a_performed_check(self):
        """A chain written before the anchor existed. Reporting it unverified would put the
        third state on every legacy cluster and make it meaningless."""
        assert await security.verify_memory_chain(Pool(ROWS, None), "c1") == ChainVerdict(True, True)

    async def test_an_empty_cluster_with_no_anchor_is_a_performed_check(self):
        assert await security.verify_memory_chain(Pool([], None), "c1") == ChainVerdict(True, True)

    async def test_rows_ahead_of_the_anchor_are_not_a_tamper(self):
        """A crash between the row write and the head write leaves the head behind. Extra
        rows, not missing ones — and both reads succeeded."""
        v = await security.verify_memory_chain(Pool(ROWS, {"seq": 0, "hash": ROWS[0]["hash"]}), "c1")
        assert v == ChainVerdict(valid=True, verified=True)


# ── 4. what the operator is told ──────────────────────────────────────────────────────────────


class TestTheLogSaysWhichStateItIs:
    async def test_the_fetch_failure_denies_the_tamper_reading(self, caplog):
        with caplog.at_level("WARNING"):
            await security.verify_memory_chain(Pool(ROWS, GOOD, fetch_exc=OSError("refused")), "c1")
        assert "NOT verified" in caplog.text
        assert "not the same as tampered" in caplog.text

    async def test_the_unparseable_anchor_names_what_is_undetectable(self, caplog):
        with caplog.at_level("WARNING"):
            await security.verify_memory_chain(Pool(ROWS, {"seq": "x", "hash": None}), "c1")
        assert "truncation of this chain is NOT currently detectable" in caplog.text


# ── 5. one vocabulary for one question ────────────────────────────────────────────────────────


class TestBothChainsAnswerInTheSameType:
    async def test_the_verdict_is_the_flight_recorders_own_type(self):
        v = await security.verify_memory_chain(Pool(ROWS, GOOD), "c1")
        assert isinstance(v, ChainVerdict)

    async def test_the_verdict_is_not_a_bool(self):
        """`ChainVerdict(True, False)` is truthy as a tuple. Any caller left doing
        `if verify_memory_chain(...)` reads an unverified chain as a good one, so the type
        must not quietly pass for the bool it replaced."""
        assert await security.verify_memory_chain(None, "c1") is not True
        assert (await security.verify_memory_chain(None, "c1")).valid is True
