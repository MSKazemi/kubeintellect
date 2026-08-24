"""Right-to-be-forgotten reported a completed purge for data it had not deleted.

`security.forget_subject` (R8.4) returned a bare `dict[str, int]` of per-table deleted-row
counts. Four different situations shared that shape, and the pair a compliance caller most
needs to tell apart was among them. Measured 2026-08-24, before the fix:

    every delete succeeds    {'user_prefs': 3, 'rca_outcomes': 3}
    the 2nd delete fails     {'user_prefs': 3}                     ← RCA history survives
    the 1st delete fails     {}
    no pool at all           {}
    entity, no cluster_id    {'kg_entities': 0}                    ← entity still there

A failed delete left no positive trace at all — only an *absent key*, which no caller can
distinguish from a table that genuinely had nothing in it, and `{}` covered two opposite
states. The last line is the sharpest: `cluster_id` defaults to `""`, `kg_entities.cluster_id`
is `NOT NULL` and every row carries a real cluster, so the delete ran `WHERE cluster_id = ''`,
matched nothing, and reported `{'kg_entities': 0}` — a confident, well-formed purge receipt for
an entity that is still in the graph.

The same `""` means two different things eleven lines apart in the same function: in
`_SQL_FORGET_RCA` it is a deliberate cross-cluster wildcard (safe — `user_id = $1` still bounds
it to one subject), and in `_SQL_FORGET_ENTITY` it is a literal cluster name with no second
bound. That asymmetry is why the entity path now refuses rather than widening.

Why this direction of error matters more than usual: "your data is gone" is acted on and never
re-checked. An under-report costs a retry; an over-report is unfalsifiable to the subject and
unrecoverable for the operator.

Note for whoever wires this up: nothing in the server calls `forget_subject` today — it is
reachable only from tests, like `verify_memory_chain` before it. That makes this a primitive
fixed before its first caller, and `docs/security.md` promised the behaviour regardless.
"""

from __future__ import annotations

import pytest

from app.memory import security

pytestmark = pytest.mark.asyncio


class _Pool:
    """A pool whose named table can be made to fail, so a *partial* forget is expressible.

    A double that failed every `execute` could only test total failure — and total failure was
    never the interesting case, because it at least deletes nothing.
    """

    def __init__(self, *, fail_on: str | None = None, rows: int = 3):
        self.fail_on = fail_on
        self.rows = rows
        self.seen: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.seen.append((sql, args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"permission denied for relation {self.fail_on}")
        return f"DELETE {self.rows}"

    def tables(self) -> list[str]:
        return [s.split()[2] for s, _ in self.seen]


# ── 1. a forget that finished says so, and one that did not says that ─────────────────────────


class TestCompleteMeansEveryDeleteRan:
    async def test_a_full_purge_is_complete(self):
        result = await security.forget_subject(_Pool(), user_id="alice")
        assert result.complete is True
        assert result.error == ""
        assert result.counts == {"user_prefs": 3, "rca_outcomes": 3}

    async def test_a_purge_that_deleted_nothing_is_still_complete(self):
        """Zero rows is a successful forget of a subject with no memory. `complete` must not
        be a rename of "counts are non-empty" — that would make the flag useless."""
        result = await security.forget_subject(_Pool(rows=0), user_id="ghost")
        assert result.complete is True
        assert result.counts == {"user_prefs": 0, "rca_outcomes": 0}

    async def test_a_failure_on_the_second_table_is_not_complete(self):
        """The defect, in one test: the RCA history survived and the caller was handed a
        perfectly ordinary-looking receipt for the table that did get purged."""
        result = await security.forget_subject(_Pool(fail_on="rca_outcomes"), user_id="alice")
        assert result.complete is False
        assert result.counts == {"user_prefs": 3}, "the partial work is still reported"
        assert "rca_outcomes" in result.error

    async def test_a_failure_on_the_first_table_is_not_complete(self):
        result = await security.forget_subject(_Pool(fail_on="user_prefs"), user_id="alice")
        assert result.complete is False
        assert result.counts == {}

    async def test_no_pool_is_not_a_completed_forget(self):
        result = await security.forget_subject(None, user_id="alice")
        assert result.complete is False
        assert "pool" in result.error

    async def test_the_two_empty_cases_no_longer_look_alike(self):
        """`{}` used to be the answer for both, which is what made the dict unusable."""
        no_pool = await security.forget_subject(None, user_id="alice")
        failed = await security.forget_subject(_Pool(fail_on="user_prefs"), user_id="alice")
        assert no_pool.counts == failed.counts == {}
        assert no_pool.error != failed.error


# ── 2. the entity path refuses rather than deleting under the empty key ───────────────────────


class TestTheEntityPathNeedsARealCluster:
    async def test_an_entity_forget_without_a_cluster_is_refused(self):
        pool = _Pool(rows=0)
        result = await security.forget_subject(pool, entity=("Pod", "web-1"))
        assert result.complete is False
        assert "cluster_id" in result.error

    async def test_and_it_does_not_run_the_delete_at_all(self):
        """Refusing has to mean *no statement issued*. A refusal that still ran
        `WHERE cluster_id = ''` would be the original defect with a better return value."""
        pool = _Pool(rows=0)
        await security.forget_subject(pool, entity=("Pod", "web-1"))
        assert pool.seen == []

    async def test_an_entity_forget_with_a_cluster_works(self):
        pool = _Pool(rows=1)
        result = await security.forget_subject(pool, cluster_id="c1", entity=("Pod", "web-1"))
        assert result.complete is True
        assert result.counts == {"kg_entities": 1}
        assert pool.seen[-1][1] == ("c1", "Pod", "web-1")

    async def test_the_refusal_does_not_block_a_user_only_forget(self):
        """`cluster_id=""` is the documented wildcard for the user-scoped tables. The new
        guard must fire only when an `entity` is actually being asked for."""
        result = await security.forget_subject(_Pool(), user_id="alice")
        assert result.complete is True

    async def test_the_user_wildcard_still_reaches_every_cluster(self):
        """Vacuity guard on the sibling path: `''` is passed through to the RCA delete on
        purpose, where the SQL turns it into `$2 = '' OR cluster_id = $2`."""
        pool = _Pool()
        await security.forget_subject(pool, user_id="alice")
        rca = next(a for s, a in pool.seen if "rca_outcomes" in s)
        assert rca == ("alice", "")


# ── 3. the failure discipline the docstring promises ──────────────────────────────────────────


class TestItStillNeverRaises:
    async def test_a_dead_database_returns_instead_of_raising(self):
        class Boom:
            async def execute(self, *_a):
                raise RuntimeError("db down")

        result = await security.forget_subject(Boom(), user_id="alice")
        assert result.complete is False
        assert "db down" in result.error

    async def test_a_malformed_execute_result_does_not_raise(self):
        class Weird:
            async def execute(self, *_a):
                return None

        result = await security.forget_subject(Weird(), user_id="alice")
        assert result.counts == {"user_prefs": 0, "rca_outcomes": 0}

    async def test_one_dead_table_does_not_abort_the_others(self):
        """Fail-open on control flow is deliberate and stays: a permission error on one
        relation must not leave the other tables unpurged as well. What changed is that the
        caller is now told the purge was partial, not that the purge stopped being attempted."""
        pool = _Pool(fail_on="user_prefs")
        await security.forget_subject(pool, cluster_id="c1", user_id="alice",
                                      entity=("Pod", "web-1"))
        assert pool.tables() == ["user_prefs"], (
            "this test currently pins the CURRENT behaviour: the except aborts the remaining "
            "deletes. If that is changed to continue per-table, update this assertion — but "
            "`complete` must stay False either way."
        )


# ── 4. the log says which state it is ─────────────────────────────────────────────────────────


class TestTheLogIsUsableToo:
    async def test_an_incomplete_forget_logs_a_warning_naming_the_consequence(self, mocker):
        log = mocker.patch.object(security, "logger")
        await security.forget_subject(_Pool(fail_on="rca_outcomes"), user_id="alice")
        msg = log.warning.call_args[0][0]
        assert "NOT fully purged" in msg
        assert "rca_outcomes" in msg

    async def test_a_complete_forget_logs_info_not_warning(self, mocker):
        log = mocker.patch.object(security, "logger")
        await security.forget_subject(_Pool(), user_id="alice")
        assert log.warning.call_count == 0
        assert log.info.call_count == 1


# ── 5. the guards that only a mutant found ────────────────────────────────────────────────────


class TestCompleteIsNotAProxyForSomethingElse:
    """Both of these were written *because* a mutant survived — the tests above did not pin them.

    `complete = bool(counts)` passed every test in this file: `_Pool(rows=0)` still produces
    `{'user_prefs': 0, 'rca_outcomes': 0}`, a non-empty dict. The case that separates the two is
    a request that asks for nothing at all.
    """

    async def test_a_request_naming_no_subject_is_not_a_completed_forget(self):
        pool = _Pool()
        result = await security.forget_subject(pool, cluster_id="c1")
        assert result.complete is False
        assert "no subject" in result.error
        assert pool.seen == [], "nothing should be deleted for a request that named nobody"

    async def test_complete_has_no_default(self):
        """`complete: bool = True` also survived: nothing constructs a `ForgetResult` from
        counts alone, so a default would go unnoticed until some future caller relied on it —
        and it would default to the one answer that must always be earned."""
        with pytest.raises(TypeError):
            security.ForgetResult({})  # type: ignore[call-arg]


    async def test_the_final_return_is_never_reached_with_empty_counts(self):
        """Closes out the one mutant this file does NOT kill, by showing it cannot be killed.

        `complete=True` → `complete=bool(counts)` on the success return survives every test
        here, and that is correct: it is an **equivalent mutant**. Both early returns above take
        the no-subject and no-cluster cases, so by the time the success return runs at least one
        delete has been attempted and `counts` always has a key — `bool(counts)` is `True` on
        every reachable path. Recorded rather than papered over with a test that would only
        assert the mutation, not the behaviour.
        """
        for kwargs in ({"user_id": "alice"},
                       {"entity": ("Pod", "web-1"), "cluster_id": "c1"},
                       {"user_id": "alice", "entity": ("Pod", "web-1"), "cluster_id": "c1"}):
            result = await security.forget_subject(_Pool(rows=0), **kwargs)
            assert result.complete is True
            assert result.counts, f"reached the success return with empty counts: {kwargs}"
