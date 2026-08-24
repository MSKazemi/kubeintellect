"""The flight-recorder chain proved links, and the banner claimed completeness.

`verify_chain` walks `prev_hash`/`seq` and returns True iff every link recomputes. That
catches an edit, a reorder and an interior delete. It does **not** catch a truncation:
delete an episode's newest rows and what is left is a shorter, perfectly valid chain.
Measured 2026-08-24 before the fix — 9 rows, delete 3, `verify_chain` still True — while
`render_markdown` printed `✅ Audit chain verified intact — every event below is
tamper-evident` over the shortened record.

The codebase had already named this exact hole and closed it, one module over:
`memory_chain_head` exists precisely so a shorter memory-audit chain contradicts its
anchor. The chain that renders a banner to a human had no anchor at all.
"""
import json

import pytest

from app.api.v1.endpoints import episodes as episodes_api
from app.db import flight_recorder as fr
from app.digest import postmortem as pmod


def chain(n: int, eid: str = "ep-1") -> list[dict]:
    rows, prev = [], ""
    for seq in range(n):
        payload = {"note": f"event {seq}"}
        kind = "tool_call" if seq else "episode_start"
        h = fr.compute_hash(prev, eid, seq, kind, payload)
        rows.append({"episode_id": eid, "seq": seq, "kind": kind,
                     "payload": json.dumps(payload), "hash": h, "prev_hash": prev})
        prev = h
    return rows


class Head:
    """A pool that answers only the head read."""

    def __init__(self, row):
        self.row = row
        self.reads = 0

    async def fetchrow(self, *a, **k):
        self.reads += 1
        return self.row


class HeadBoom:
    async def fetchrow(self, *a, **k): raise RuntimeError("permission denied")


def anchor(rows: list[dict]) -> Head:
    return Head({"seq": rows[-1]["seq"], "hash": rows[-1]["hash"]})


@pytest.fixture(autouse=True)
def _restore_pool():
    before = fr._pool
    yield
    fr._pool = before


class TestTheLinkCheckAloneCannotSeeIt:
    def test_the_link_check_still_passes_a_truncated_chain(self):
        # Not a bug in verify_chain — the reason the anchor has to exist. If this ever
        # starts failing, the premise of this whole module changed.
        full = chain(9)
        assert fr.verify_chain(full) is True
        assert fr.verify_chain(full[:6]) is True

    def test_the_link_check_does_catch_the_things_it_claims_to(self):
        # Vacuity guard: a verifier that returned True for everything would also "pass"
        # the test above.
        full = chain(9)
        assert fr.verify_chain(full[1:]) is False, "front truncation must break a link"
        edited = [dict(r) for r in full]
        edited[3]["payload"] = json.dumps({"note": "edited"})
        assert fr.verify_chain(edited) is False, "an edited payload must break a link"
        reordered = full[:3] + [full[4], full[3]] + full[5:]
        assert fr.verify_chain(reordered) is False, "a reorder must break a link"


class TestTheAnchorCatchesTheTruncation:
    async def test_a_truncated_chain_contradicts_its_anchor(self):
        full = chain(9)
        fr._pool = anchor(full)
        assert (await fr.verify_episode("ep-1", full[:6])).valid is False

    async def test_an_intact_chain_agrees_with_its_anchor(self):
        # Vacuity guard in the other direction: a verdict that is always False catches
        # every truncation and is worthless.
        full = chain(9)
        fr._pool = anchor(full)
        assert (await fr.verify_episode("ep-1", full)).valid is True

    async def test_a_totally_emptied_episode_contradicts_its_anchor(self):
        full = chain(9)
        fr._pool = anchor(full)
        assert (await fr.verify_episode("ep-1", [])).valid is False

    async def test_the_anchor_is_actually_consulted(self):
        # Non-vacuity spy: proves the True above is not True-by-not-looking.
        full = chain(9)
        pool = anchor(full)
        fr._pool = pool
        await fr.verify_episode("ep-1", full)
        assert pool.reads == 1

    async def test_an_episode_older_than_the_anchor_is_not_called_tampered(self):
        fr._pool = Head(None)
        verdict = await fr.verify_episode("ep-1", chain(9)[:6])
        assert verdict.valid is True
        # The anchor was read and there was none. That is a performed check, not a failed
        # one — deliberately not the same state as the two cases below.
        assert verdict.verified is True

    async def test_an_unreadable_anchor_is_not_evidence_of_tampering(self):
        fr._pool = HeadBoom()
        verdict = await fr.verify_episode("ep-1", chain(9)[:6])
        assert verdict.valid is True
        # …and it is not evidence of intactness either. This is the state that used to be
        # indistinguishable from a verified chain.
        assert verdict.verified is False

    async def test_no_pool_is_not_evidence_of_tampering(self):
        fr._pool = None
        verdict = await fr.verify_episode("ep-1", chain(9)[:6])
        assert verdict.valid is True
        assert verdict.verified is False

    async def test_rows_ahead_of_a_lagging_anchor_are_not_called_tampered(self):
        # A head write that failed after the events landed. Warns, does not cry tamper.
        full = chain(9)
        fr._pool = Head({"seq": 5, "hash": full[5]["hash"]})
        assert (await fr.verify_episode("ep-1", full)).valid is True

    async def test_a_forged_head_hash_at_the_right_seq_still_fails(self):
        full = chain(9)
        fr._pool = Head({"seq": 8, "hash": "0" * 64})
        assert (await fr.verify_episode("ep-1", full)).valid is False

    async def test_broken_links_short_circuit_before_the_anchor_is_read(self):
        full = chain(9)
        edited = [dict(r) for r in full]
        edited[3]["payload"] = json.dumps({"note": "edited"})
        pool = anchor(full)
        fr._pool = pool
        assert (await fr.verify_episode("ep-1", edited)).valid is False
        assert pool.reads == 0


class TestTheBannerStopsLying:
    async def _banner(self, rows, monkeypatch):
        async def fake(eid, *a, **k): return rows
        monkeypatch.setattr(fr, "fetch_episode", fake)
        pm = await pmod.build_postmortem("ep-1")
        return pmod.render_markdown(pm).split("\n")[2], pm

    async def test_a_truncated_episode_no_longer_renders_the_tick(self, monkeypatch):
        full = chain(9)
        fr._pool = anchor(full)
        line, pm = await self._banner(full[:6], monkeypatch)
        assert pm["chain_valid"] is False
        assert "✅" not in line
        assert "AUDIT CHAIN BROKEN" in line
        assert "truncated" in line

    async def test_an_intact_episode_still_renders_the_tick(self, monkeypatch):
        # Vacuity guard: a banner that never says ✅ would pass the test above.
        full = chain(9)
        fr._pool = anchor(full)
        line, pm = await self._banner(full, monkeypatch)
        assert pm["chain_valid"] is True
        assert "✅ Audit chain verified intact" in line

    async def test_a_wiped_episode_is_not_described_as_one_where_nothing_happened(
        self, monkeypatch
    ):
        full = chain(9)
        fr._pool = anchor(full)
        line, pm = await self._banner([], monkeypatch)
        assert pm["chain_verified"] is True
        assert pm["chain_valid"] is False
        assert "removed" in pm["summary"]
        assert "NOT an episode in which nothing happened" in pm["summary"]
        assert "AUDIT CHAIN BROKEN" in line

    async def test_a_genuinely_empty_episode_still_says_so(self, monkeypatch):
        # Vacuity guard: the fix must not turn every empty episode into a tamper alarm.
        fr._pool = Head(None)
        line, pm = await self._banner([], monkeypatch)
        assert pm["summary"] == "No recorded events for this episode."
        assert "removed" not in pm["summary"]
        assert "NOT VERIFIED" in line


class TestTheReplayEndpointStopsLaunderingIt:
    async def test_a_wiped_episode_is_409_not_404(self, monkeypatch):
        full = chain(9)
        fr._pool = anchor(full)

        async def fake(eid, *a, **k): return []
        monkeypatch.setattr(episodes_api, "fetch_episode", fake)
        with pytest.raises(episodes_api.HTTPException) as ei:
            await episodes_api.replay_episode("ep-1")
        assert ei.value.status_code == 409
        assert "removed" in ei.value.detail
        assert "never existing" in ei.value.detail

    async def test_an_episode_that_really_never_existed_is_still_404(self, monkeypatch):
        # Vacuity guard: 409-for-everything would pass the test above.
        fr._pool = Head(None)

        async def fake(eid, *a, **k): return []
        monkeypatch.setattr(episodes_api, "fetch_episode", fake)
        with pytest.raises(episodes_api.HTTPException) as ei:
            await episodes_api.replay_episode("ep-1")
        assert ei.value.status_code == 404


class TestAnAppendMustNotHealTheTruncation:
    async def test_the_next_seq_continues_past_the_head_not_past_the_rows(self):
        # The other half: if an append after a truncation re-anchored at the surviving tail,
        # the chain would close over the hole and the evidence would be gone for good.
        fr._chains.clear()
        full = chain(9)

        class P:
            def __init__(self): self.q = []

            async def fetchrow(self, sql, *a):
                self.q.append(sql)
                if "decision_log_head" in sql:
                    return {"seq": 8, "hash": full[8]["hash"]}
                return {"seq": 5, "hash": full[5]["hash"]}  # rows truncated to seq 5

        fr._pool = P()
        try:
            next_seq, _ = await fr._chain_state("ep-1")
        finally:
            fr._chains.clear()
        assert next_seq == 9, "an append re-anchored at the surviving tail and healed the hole"

    async def test_an_untruncated_episode_continues_from_its_rows(self):
        # Vacuity guard: always jumping to head+1 would also pass the test above.
        fr._chains.clear()
        full = chain(9)

        class P:
            async def fetchrow(self, sql, *a):
                if "decision_log_head" in sql:
                    return {"seq": 8, "hash": full[8]["hash"]}
                return {"seq": 8, "hash": full[8]["hash"]}

        fr._pool = P()
        try:
            next_seq, last = await fr._chain_state("ep-1")
        finally:
            fr._chains.clear()
        assert next_seq == 9
        assert last == full[8]["hash"]
