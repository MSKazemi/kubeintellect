"""A hash chain cannot see its own tail being cut off. This is where that was fixed.

`security.md` promises that *"a silent edit, delete, or reorder of learned memory is
detectable via `verify_memory_chain`"*. Three of those four words held. Measured against the
real append path before this change:

    intact chain          -> True
    middle payload edited -> False      # link breaks
    middle row deleted    -> False      # seq gap
    first row deleted     -> False      # chain no longer starts at 0
    last 2 rows deleted   -> True       # <-- a delete, reported as intact

Truncation is the one deletion a chain is structurally blind to: what remains is a shorter,
perfectly valid chain. Worse, the next legitimate append used to continue from the surviving
tail, so the loss became invisible *permanently* — and an attacker deleting the newest
entries is deleting exactly the evidence of what they just did.

The fix is an external anchor: `memory_chain_head` records how far the chain got, so a
shorter chain contradicts something. It is tamper-EVIDENCE, not prevention — an attacker with
full database write can forge the head too. What it removes is the tamper that needs no
second edit, and it refuses to heal itself afterwards.
"""

from __future__ import annotations

import pytest
from app.memory import security


class ChainPool:
    """`memory_audit` + `memory_chain_head`, dispatching on SQL as two real tables would."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.head: dict[str, dict] = {}
        self.head_reads = 0

    async def fetchrow(self, sql, *a):
        cid = a[0]
        if "memory_chain_head" in sql:
            self.head_reads += 1
            return self.head.get(cid)
        rows = [r for r in self.rows if r["cluster_id"] == cid]
        return max(rows, key=lambda r: r["seq"]) if rows else None

    async def execute(self, sql, *a):
        if "memory_chain_head" in sql:
            self.head[a[0]] = {"seq": a[1], "hash": a[2]}
            return "OK"
        self.rows.append({
            "cluster_id": a[0], "seq": a[1], "kind": a[2], "ref_id": a[3],
            "payload": a[4], "prev_hash": a[5], "hash": a[6],
        })
        return "OK"

    async def fetch(self, sql, *a):
        return sorted((r for r in self.rows if r["cluster_id"] == a[0]),
                      key=lambda r: r["seq"])


@pytest.fixture
def chain():
    security.reset_audit_chains()
    yield ChainPool()
    security.reset_audit_chains()


async def append(pool, cluster="c1", n=1, kind="episode_write"):
    for i in range(n):
        await security.record_memory_audit(
            pool, cluster_id=cluster, kind=kind, ref_id=f"ep-{i}",
            payload={"summary": f"fact {i}"},
        )


class TestTruncationIsNoLongerFree:

    async def test_cutting_the_newest_entries_is_detected(self, chain):
        await append(chain, n=5)
        assert (await security.verify_memory_chain(chain, "c1")).valid is True

        chain.rows = chain.rows[:3]          # the attacker removes what they just did
        assert (await security.verify_memory_chain(chain, "c1")).valid is False

    async def test_cutting_every_entry_is_detected(self, chain):
        """Deleting the whole table leaves a chain that is 'empty', which used to be
        trivially valid — the most complete tamper looked the same as a fresh install."""
        await append(chain, n=3)
        chain.rows.clear()

        assert (await security.verify_memory_chain(chain, "c1")).valid is False

    async def test_a_later_append_does_not_heal_it(self, chain):
        """The damaging half. Re-anchoring on the next legitimate write would erase the only
        evidence the truncation ever happened."""
        await append(chain, n=5)
        chain.rows = chain.rows[:3]
        security.reset_audit_chains()        # as a restart would

        await append(chain, n=1, kind="quarantine")
        assert (await security.verify_memory_chain(chain, "c1")).valid is False

    async def test_the_gap_is_not_reused_so_the_evidence_persists(self, chain):
        """Concretely: the new entry continues *past* the head rather than filling the hole."""
        await append(chain, n=5)
        chain.rows = chain.rows[:3]          # surviving seqs are 0,1,2; head still says 4
        security.reset_audit_chains()

        await append(chain, n=1, kind="quarantine")
        assert [r["seq"] for r in chain.rows] == [0, 1, 2, 5]

    async def test_truncating_one_cluster_does_not_accuse_another(self, chain):
        await append(chain, cluster="c1", n=3)
        await append(chain, cluster="c2", n=3)
        chain.rows = [r for r in chain.rows if not (r["cluster_id"] == "c1" and r["seq"] > 0)]

        assert (await security.verify_memory_chain(chain, "c1")).valid is False
        assert (await security.verify_memory_chain(chain, "c2")).valid is True


class TestTheOriginalDetectionStillHolds:
    """A fix that traded one blind spot for another would pass everything above."""

    async def test_an_intact_chain_still_verifies(self, chain):
        await append(chain, n=4)
        assert (await security.verify_memory_chain(chain, "c1")).valid is True

    async def test_an_edited_entry_is_still_detected(self, chain):
        await append(chain, n=3)
        chain.rows[1]["kind"] = "forget"
        assert (await security.verify_memory_chain(chain, "c1")).valid is False

    async def test_an_interior_deletion_is_still_detected(self, chain):
        await append(chain, n=4)
        del chain.rows[1]
        assert (await security.verify_memory_chain(chain, "c1")).valid is False

    async def test_a_reorder_is_still_detected(self, chain):
        await append(chain, n=4)
        chain.rows[1], chain.rows[2] = chain.rows[2], chain.rows[1]
        for i, r in enumerate(chain.rows):
            r["seq"] = i                     # renumber, so only the links can betray it
        assert (await security.verify_memory_chain(chain, "c1")).valid is False


class TestTheAnchorDoesNotManufactureAlarms:
    """Tamper-evidence is worthless if operators learn to ignore it."""

    async def test_a_cluster_that_never_wrote_anything_is_intact(self, chain):
        assert (await security.verify_memory_chain(chain, "never-seen")).valid is True

    async def test_a_chain_written_before_the_anchor_existed_is_not_accused(self, chain):
        """Upgrade case: rows exist, no head row. Nothing contradicts them."""
        await append(chain, n=3)
        chain.head.clear()

        assert (await security.verify_memory_chain(chain, "c1")).valid is True

    async def test_a_head_write_lost_to_a_crash_is_not_a_tamper(self, chain):
        """The head is written after the row it describes. A crash between the two leaves the
        head *behind* — extra rows, not missing ones. That is not evidence of deletion."""
        await append(chain, n=3)
        chain.head["c1"] = {"seq": 1, "hash": chain.rows[1]["hash"]}

        assert (await security.verify_memory_chain(chain, "c1")).valid is True

    async def test_an_unreadable_head_falls_back_instead_of_crying_wolf(self, chain):
        """A missing table or a permissions error is an infrastructure problem. Reporting it
        as tampering would be a false accusation about the operator's own data."""
        await append(chain, n=3)

        async def boom(sql, *a):
            if "memory_chain_head" in sql:
                raise RuntimeError("relation does not exist")
            rows = [r for r in chain.rows if r["cluster_id"] == a[0]]
            return max(rows, key=lambda r: r["seq"]) if rows else None

        chain.fetchrow = boom                                    # type: ignore[assignment]
        assert (await security.verify_memory_chain(chain, "c1")).valid is True

    async def test_no_pool_is_still_safe(self, chain):
        assert (await security.verify_memory_chain(None, "c1")).valid is True


class TestTheAnchorIsActuallyMaintained:
    """If the head lags the chain, every verification becomes a false alarm instead."""

    async def test_every_append_advances_the_head(self, chain):
        for expected in range(4):
            await append(chain, n=1)
            assert chain.head["c1"]["seq"] == expected
            assert chain.head["c1"]["hash"] == chain.rows[-1]["hash"]

    async def test_the_head_is_per_cluster(self, chain):
        await append(chain, cluster="c1", n=2)
        await append(chain, cluster="c2", n=1)

        assert chain.head["c1"]["seq"] == 1
        assert chain.head["c2"]["seq"] == 0
