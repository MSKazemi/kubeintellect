"""A lost batch must never read as an intact, complete record.

`docs/flight-recorder.md` promises: "there is no way to edit, insert, or delete a record
without the chain failing verification afterwards." The same page states that during a
recorder outage "events are dropped, not buffered to disk". Both cannot be true unless the
recorder records its own losses — so it does, as `GAP_KIND` rows inside the chain.

Proven against a real Postgres 16 first (2026-08-20), then pinned here. Before the fix:

  same process, outage mid-episode → seqs [0,1,2,6,7,8]  chain_valid=False
      (a database blip permanently on the record as suspected tampering)
  outage, then a restart          → seqs [0,1,2,3,4,5]   chain_valid=True
      (three recorded events silently gone, reported as verified and complete)

The fake pool below is a stand-in for Postgres only in storage; every assertion drives the
real `_flush` / `_chain_state` / `_carry_loss` / `verify_chain`, and it reproduces the two
behaviours the chain depends on: `fetchrow` returns the last *persisted* row, and a failed
`executemany` persists nothing (asyncpg's executemany is atomic — verified against
postgres:16-alpine, a UNIQUE violation on row 2 of 3 left the table unchanged).
"""
from __future__ import annotations

import json

import pytest

from app.db import flight_recorder as fr

pytestmark = pytest.mark.asyncio


class FakePool:
    """Stores rows like decision_log does; can be switched to fail like a DB outage."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []
        self.fail: str | None = None
        self.fail_fetch: str | None = None      # only the head lookup fails (e.g. a timeout)

    async def executemany(self, _sql, rows):
        if self.fail:
            raise RuntimeError(self.fail)          # atomic: nothing persists
        for r in rows:
            if any(x[0] == r[0] and x[1] == r[1] for x in self.rows):
                raise RuntimeError(
                    'duplicate key value violates unique constraint "decision_log_episode_id_seq_key"'
                )
        self.rows.extend(rows)

    async def fetchrow(self, _sql, episode_id):
        if self.fail or self.fail_fetch:
            raise RuntimeError(self.fail_fetch or self.fail)
        mine = [r for r in self.rows if r[0] == episode_id]
        if not mine:
            return None
        last = max(mine, key=lambda r: r[1])
        return {"seq": last[1], "hash": last[5]}

    def chain(self, episode_id: str) -> list[dict]:
        return [
            {"episode_id": r[0], "seq": r[1], "kind": r[2], "payload": json.loads(r[3]),
             "prev_hash": r[4], "hash": r[5]}
            for r in sorted((r for r in self.rows if r[0] == episode_id), key=lambda r: r[1])
        ]


@pytest.fixture
def pool(mocker):
    p = FakePool()
    mocker.patch.object(fr, "_pool", p)
    fr._chains.clear()
    fr._pending_gaps.clear()
    yield p
    fr._chains.clear()
    fr._pending_gaps.clear()
    fr._pool = None


def _ev(episode: str, n: int, tag: str) -> list[tuple[str, str, dict]]:
    return [(episode, "status", {"type": "status", "message": f"{tag}-{i}"}) for i in range(n)]


def _messages(rows: list[dict]) -> list[str]:
    return [r["payload"].get("message", "") for r in rows]


class TestALostBatchDoesNotFakeTampering:
    async def test_same_process_outage_leaves_a_verifiable_chain(self, pool):
        await fr._flush(_ev("ep", 3, "before"))
        pool.fail = "connection refused"
        await fr._flush(_ev("ep", 3, "lost"))
        pool.fail = None
        await fr._flush(_ev("ep", 3, "after"))

        rows = pool.chain("ep")
        assert [r["seq"] for r in rows] == list(range(len(rows))), "a lost batch left a seq gap"
        assert fr.verify_chain(rows) is True, "a database blip was reported as tampering"

    async def test_the_chain_head_is_not_advanced_by_a_failed_write(self, pool):
        await fr._flush(_ev("ep", 2, "ok"))
        head_before = fr._chains["ep"]
        pool.fail = "connection refused"
        await fr._flush(_ev("ep", 5, "lost"))
        # The cached head is dropped, not advanced — the next flush re-reads the DB.
        assert "ep" not in fr._chains
        pool.fail = None
        await fr._flush(_ev("ep", 1, "after"))
        assert fr._chains["ep"][0] > head_before[0]
        assert fr.verify_chain(pool.chain("ep")) is True

    async def test_a_restart_after_an_outage_does_not_hide_the_loss(self, pool):
        await fr._flush(_ev("ep", 3, "before"))
        pool.fail = "connection refused"
        await fr._flush(_ev("ep", 3, "lost"))
        pool.fail = None
        fr._chains.clear()                     # the process restarts; only the DB survives
        await fr._flush(_ev("ep", 3, "after"))

        rows = pool.chain("ep")
        assert fr.verify_chain(rows) is True
        kinds = [r["kind"] for r in rows]
        assert fr.GAP_KIND in kinds, "the loss was invisible — chain intact over a hole"


class TestTheLossIsInsideTheChain:
    async def test_the_gap_row_states_how_many_were_lost_and_why(self, pool):
        await fr._flush(_ev("ep", 1, "before"))
        pool.fail = "connection refused"
        await fr._flush(_ev("ep", 4, "lost"))
        pool.fail = None
        await fr._flush(_ev("ep", 1, "after"))

        gaps = [r for r in pool.chain("ep") if r["kind"] == fr.GAP_KIND]
        assert len(gaps) == 1
        assert gaps[0]["payload"]["dropped"] == 4
        assert "connection refused" in gaps[0]["payload"]["reason"]
        assert gaps[0]["payload"]["type"] == fr.GAP_KIND      # the SSE frame carries only payload

    async def test_the_gap_lands_before_the_events_that_followed_it(self, pool):
        await fr._flush(_ev("ep", 1, "before"))
        pool.fail = "boom"
        await fr._flush(_ev("ep", 2, "lost"))
        pool.fail = None
        await fr._flush(_ev("ep", 1, "after"))

        rows = pool.chain("ep")
        kinds = [r["kind"] for r in rows]
        assert kinds == ["status", fr.GAP_KIND, "status"]

    async def test_the_gap_row_cannot_be_removed_without_breaking_the_chain(self, pool):
        await fr._flush(_ev("ep", 1, "before"))
        pool.fail = "boom"
        await fr._flush(_ev("ep", 2, "lost"))
        pool.fail = None
        await fr._flush(_ev("ep", 1, "after"))

        rows = pool.chain("ep")
        assert fr.verify_chain(rows) is True
        without_gap = [r for r in rows if r["kind"] != fr.GAP_KIND]
        assert len(without_gap) == len(rows) - 1
        assert fr.verify_chain(without_gap) is False

    async def test_repeated_outages_accumulate_into_one_honest_total(self, pool):
        pool.fail = "boom"
        await fr._flush(_ev("ep", 2, "lost1"))
        await fr._flush(_ev("ep", 3, "lost2"))
        pool.fail = None
        await fr._flush(_ev("ep", 1, "after"))

        gaps = [r for r in pool.chain("ep") if r["kind"] == fr.GAP_KIND]
        assert len(gaps) == 1
        assert gaps[0]["payload"]["dropped"] == 5

    async def test_a_failed_gap_row_is_not_itself_lost(self, pool):
        pool.fail = "boom"
        await fr._flush(_ev("ep", 2, "lost"))
        await fr._flush([])                      # a gap-only flush that also fails
        pool.fail = None
        await fr._flush(_ev("ep", 1, "after"))

        gaps = [r for r in pool.chain("ep") if r["kind"] == fr.GAP_KIND]
        assert sum(g["payload"]["dropped"] for g in gaps) == 2

    async def test_losses_are_tracked_per_episode(self, pool):
        pool.fail = "boom"
        await fr._flush(_ev("a", 2, "lost") + _ev("b", 3, "lost"))
        pool.fail = None
        await fr._flush(_ev("a", 1, "after") + _ev("b", 1, "after"))

        assert [r["payload"]["dropped"] for r in pool.chain("a") if r["kind"] == fr.GAP_KIND] == [2]
        assert [r["payload"]["dropped"] for r in pool.chain("b") if r["kind"] == fr.GAP_KIND] == [3]
        assert fr.verify_chain(pool.chain("a")) is True
        assert fr.verify_chain(pool.chain("b")) is True

    async def test_a_healthy_recorder_writes_no_gap_rows(self, pool):
        await fr._flush(_ev("ep", 3, "ok"))
        await fr._flush(_ev("ep", 2, "ok2"))
        rows = pool.chain("ep")
        assert [r["kind"] for r in rows] == ["status"] * 5
        assert fr.verify_chain(rows) is True
        assert fr._pending_gaps == {}


class TestChainStateLookupFailure:
    async def test_a_failed_head_lookup_does_not_restart_the_chain_at_zero(self, pool):
        await fr._flush(_ev("ep", 3, "before"))
        fr._chains.clear()                       # restart: the head must come from the DB
        pool.fail = "connection refused"
        await fr._flush(_ev("ep", 1, "during"))  # the SELECT fails, not just the INSERT
        assert "ep" not in fr._chains, "an unknown head was cached as a genesis head"
        pool.fail = None
        await fr._flush(_ev("ep", 1, "after"))

        rows = pool.chain("ep")
        assert [r["seq"] for r in rows] == list(range(len(rows)))
        assert fr.verify_chain(rows) is True

    async def test_the_recorded_reason_is_the_real_cause_not_a_downstream_symptom(self, pool):
        # Swallowing a failed lookup and caching (0, "") made the next INSERT collide with
        # UNIQUE (episode_id, seq). The recorder then wrote "duplicate key ..." into the log
        # as the reason it lost data — a symptom of its own retry, not what actually happened.
        await fr._flush(_ev("ep", 2, "before"))
        fr._chains.clear()
        pool.fail_fetch = "canceling statement due to statement timeout"
        await fr._flush(_ev("ep", 2, "lost"))
        pool.fail_fetch = None
        await fr._flush(_ev("ep", 1, "after"))

        gaps = [r for r in pool.chain("ep") if r["kind"] == fr.GAP_KIND]
        assert len(gaps) == 1
        assert "statement timeout" in gaps[0]["payload"]["reason"]
        assert "duplicate key" not in gaps[0]["payload"]["reason"]

    async def test_the_uniqueness_constraint_is_never_hit_after_an_outage(self, pool):
        # Restarting the chain at 0 over an existing episode used to collide with
        # UNIQUE (episode_id, seq) on every subsequent batch — silent, permanent loss.
        await fr._flush(_ev("ep", 2, "before"))
        fr._chains.clear()
        pool.fail = "connection refused"
        await fr._flush(_ev("ep", 2, "lost"))
        pool.fail = None
        for i in range(3):
            await fr._flush(_ev("ep", 1, f"after{i}"))
        rows = pool.chain("ep")
        assert len(rows) == 2 + 1 + 3            # before + gap + three later batches
        assert fr.verify_chain(rows) is True


class TestNothingToWriteTo:
    async def test_no_pool_means_no_state_is_mutated(self, mocker):
        mocker.patch.object(fr, "_pool", None)
        fr._chains.clear()
        fr._pending_gaps.clear()
        await fr._flush(_ev("ep", 3, "x"))
        assert fr._chains == {} and fr._pending_gaps == {}

    async def test_an_idle_flush_with_no_debt_does_nothing(self, pool):
        await fr._flush([])
        assert pool.rows == []


class TestThePostmortemSaysTheRecordIsIncomplete:
    """`kq postmortem` prints "✅ Audit chain verified intact" from `chain_valid` alone.
    Over a chain that contains a gap that sentence is true and misleading at once."""

    @staticmethod
    def _rows(episode_id: str, events: list[tuple[str, dict]]) -> list[dict]:
        rows, prev = [], ""
        for seq, (kind, payload) in enumerate(events):
            h = fr.compute_hash(prev, episode_id, seq, kind, payload)
            rows.append({"episode_id": episode_id, "seq": seq, "kind": kind,
                         "payload": json.dumps(payload), "prev_hash": prev, "hash": h,
                         "created_at": None})
            prev = h
        return rows

    @pytest.fixture
    def gapped(self, mocker):
        from app.digest import postmortem

        rows = self._rows("ep", [
            ("status", {"type": "status", "message": "started"}),
            (fr.GAP_KIND, fr._gap_payload(7, "the decision_log table was missing")),
            ("answer", {"type": "answer", "text": "raised the memory limit"}),
        ])

        async def _fetch(_episode_id):
            return rows
        mocker.patch.object(fr, "fetch_episode", side_effect=_fetch)
        return postmortem

    async def test_the_loss_is_counted(self, gapped):
        pm = await gapped.build_postmortem("ep")
        assert pm["chain_valid"] is True
        assert pm["events_lost"] == 7
        assert pm["gaps"][0]["reason"] == "the decision_log table was missing"

    async def test_the_render_does_not_read_as_an_all_clear(self, gapped):
        md = gapped.render_markdown(await gapped.build_postmortem("ep"))
        assert "RECORD INCOMPLETE" in md
        assert "7 event(s) were never written" in md
        assert "the decision_log table was missing" in md

    async def test_the_timeline_names_the_hole(self, gapped):
        pm = await gapped.build_postmortem("ep")
        gap_line = [e for e in pm["timeline"] if e["kind"] == fr.GAP_KIND][0]
        assert "7 event(s) LOST" in gap_line["summary"]

    async def test_a_complete_episode_carries_no_incomplete_banner(self, mocker):
        from app.digest import postmortem

        rows = self._rows("ep", [("status", {"type": "status", "message": "started"}),
                                 ("answer", {"type": "answer", "text": "done"})])

        async def _fetch(_episode_id):
            return rows
        mocker.patch.object(fr, "fetch_episode", side_effect=_fetch)
        pm = await postmortem.build_postmortem("ep")
        assert pm["events_lost"] == 0
        assert "RECORD INCOMPLETE" not in postmortem.render_markdown(pm)
