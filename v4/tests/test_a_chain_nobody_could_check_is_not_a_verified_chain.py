"""An anchor read that failed reported the same verdict as an anchor that agreed.

`verify_episode` answers the question `kq replay` and the postmortem banner render. It has
three real answers — the records contradict something, they contradict nothing, or nobody
could look — and a `bool` can hold two. Every path that could not look returned the `True`
an intact chain returns:

* the head read raised (a permissions error, a dropped table, a dead connection);
* the head row was present but unparseable (schema drift, a partial migration);
* there was no pool to ask.

Measured 2026-08-24: in the middle case the module logs

    chain head for 'ep-1' is present but unreadable (…) — truncation of this episode is
    NOT currently detectable

and the *same call* returns True, so `replay_meta.chain_valid` is `true`, `kq replay` prints
`✓ chain intact` and exits **0**. The operator asking "was this record tampered with?" during
an incident is told no, on the strength of a check that did not run.

The third state was already spoken everywhere else. `kq replay` has owned exit `4` — "chain
NOT VERIFIED … do not treat this as proof they are untampered" — since before this fix, and
the postmortem payload has carried `chain_verified` alongside `chain_valid` for the same
reason. Only the function producing the verdict could not say it.

`valid` keeps its old meaning throughout: *nothing contradicted these records*. What is new
is that it now travels with whether anything could have.

Deliberately unchanged: a **missing** head stays verified. The anchor was read and there is
none — an episode written before the anchor existed. That is a check that ran, and treating
it as unverified would put a red banner on every legacy episode.
"""

from __future__ import annotations

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
                     "payload": json.dumps(payload), "hash": h, "prev_hash": prev,
                     "created_at": 0.0})
        prev = h
    return rows


class Head:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, *_a, **_k):
        return self.row


class HeadBoom:
    async def fetchrow(self, *_a, **_k):
        raise RuntimeError("permission denied for table decision_log_head")


class HeadGarbage:
    """A head row this build cannot interpret — schema drift, a partial migration."""

    async def fetchrow(self, *_a, **_k):
        return {"seq": "not-an-int", "hash": None}


def anchor(rows: list[dict]):
    return Head({"seq": rows[-1]["seq"], "hash": rows[-1]["hash"]})


@pytest.fixture(autouse=True)
def _restore_pool():
    """`fr._pool` is module state; every test here writes it. Leaking one of these fakes into
    the next file turns six unrelated tests red, which is how this fixture got written."""
    before = fr._pool
    yield
    fr._pool = before


# ── the verdict itself ───────────────────────────────────────────────────────


class TestTheVerdictSeparatesCheckedFromUncontradicted:
    pytestmark = pytest.mark.asyncio

    @pytest.mark.parametrize("pool", [HeadBoom(), HeadGarbage(), None])
    async def test_a_check_that_could_not_run_is_not_a_passed_check(self, pool):
        fr._pool = pool
        verdict = await fr.verify_episode("ep-1", chain(9))
        assert verdict.verified is False
        assert verdict.valid is True, "still not evidence of tampering, either"

    async def test_an_agreeing_anchor_is_verified(self):
        """Vacuity guard: a `verified` that is always False says nothing."""
        full = chain(9)
        fr._pool = anchor(full)
        verdict = await fr.verify_episode("ep-1", full)
        assert (verdict.valid, verdict.verified) == (True, True)

    async def test_a_missing_anchor_is_a_check_that_ran(self):
        """Read, found nothing to contradict these rows. Not the same as a failed read."""
        fr._pool = Head(None)
        verdict = await fr.verify_episode("ep-1", chain(9))
        assert (verdict.valid, verdict.verified) == (True, True)

    async def test_a_truncation_is_a_finding_not_a_failure(self):
        full = chain(9)
        fr._pool = anchor(full)
        verdict = await fr.verify_episode("ep-1", full[:6])
        assert (verdict.valid, verdict.verified) == (False, True)

    async def test_a_broken_link_is_verified_without_reading_the_anchor(self):
        """The records recompute to a different hash. That is a performed check."""
        full = chain(9)
        edited = [dict(r) for r in full]
        edited[3]["payload"] = json.dumps({"note": "edited"})
        fr._pool = HeadBoom()
        verdict = await fr.verify_episode("ep-1", edited)
        assert (verdict.valid, verdict.verified) == (False, True)

    async def test_head_agrees_still_answers_the_two_state_question(self):
        """The boolean face is kept, and it is still the old answer."""
        fr._pool = None
        assert await fr.head_agrees(HeadBoom(), "ep-1", chain(9)) is True
        full = chain(9)
        assert await fr.head_agrees(anchor(full), "ep-1", full[:6]) is False


# ── what the replay endpoint puts on the wire ────────────────────────────────


async def _replay_meta(monkeypatch, rows, pool):
    monkeypatch.setattr(episodes_api, "fetch_episode",
                        lambda _eid: _async(rows), raising=True)
    fr._pool = pool
    response = await episodes_api.replay_episode("ep-1")
    frames = []
    async for chunk in response.body_iterator:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                frames.append(json.loads(line[6:]))
    return frames[0]


async def _async(value):
    return value


class TestTheWireCarriesTheThirdState:
    pytestmark = pytest.mark.asyncio

    async def test_replay_meta_reports_an_unreadable_anchor(self, monkeypatch):
        meta = await _replay_meta(monkeypatch, chain(4), HeadBoom())
        assert meta["chain_verified"] is False
        assert meta["chain_valid"] is True

    async def test_replay_meta_reports_a_real_verification(self, monkeypatch):
        full = chain(4)
        meta = await _replay_meta(monkeypatch, full, anchor(full))
        assert (meta["chain_valid"], meta["chain_verified"]) == (True, True)

    async def test_an_empty_episode_with_an_unreadable_anchor_is_not_a_404(self, monkeypatch):
        """404 claims it never existed; 409 claims it was emptied. Neither is known here."""
        from fastapi import HTTPException
        monkeypatch.setattr(episodes_api, "fetch_episode", lambda _eid: _async([]))
        fr._pool = HeadBoom()
        with pytest.raises(HTTPException) as excinfo:
            await episodes_api.replay_episode("ep-1")
        assert excinfo.value.status_code == 503
        assert "cannot be told apart" in excinfo.value.detail

    async def test_an_empty_episode_with_a_readable_anchor_still_404s(self, monkeypatch):
        from fastapi import HTTPException
        monkeypatch.setattr(episodes_api, "fetch_episode", lambda _eid: _async([]))
        fr._pool = Head(None)
        with pytest.raises(HTTPException) as excinfo:
            await episodes_api.replay_episode("ep-1")
        assert excinfo.value.status_code == 404

    async def test_a_totally_truncated_episode_still_409s(self, monkeypatch):
        from fastapi import HTTPException
        full = chain(9)
        monkeypatch.setattr(episodes_api, "fetch_episode", lambda _eid: _async([]))
        fr._pool = anchor(full)
        with pytest.raises(HTTPException) as excinfo:
            await episodes_api.replay_episode("ep-1")
        assert excinfo.value.status_code == 409


# ── what the postmortem renders ──────────────────────────────────────────────


class TestThePostmortemBannerFollowsTheVerdict:
    pytestmark = pytest.mark.asyncio

    async def test_an_unreadable_anchor_suppresses_the_verified_banner(self, monkeypatch):
        rows = chain(4)
        monkeypatch.setattr(pmod.flight_recorder, "fetch_episode", lambda _eid: _async(rows))
        fr._pool = HeadBoom()
        pm = await pmod.build_postmortem("ep-1")
        assert pm["chain_verified"] is False
        assert "✅" not in pmod.render_markdown(pm)

    async def test_a_verified_intact_chain_still_gets_its_banner(self, monkeypatch):
        rows = chain(4)
        monkeypatch.setattr(pmod.flight_recorder, "fetch_episode", lambda _eid: _async(rows))
        fr._pool = anchor(rows)
        pm = await pmod.build_postmortem("ep-1")
        assert (pm["chain_valid"], pm["chain_verified"]) == (True, True)

    async def test_an_empty_episode_with_an_unreadable_anchor_says_so(self, monkeypatch):
        monkeypatch.setattr(pmod.flight_recorder, "fetch_episode", lambda _eid: _async([]))
        fr._pool = HeadBoom()
        pm = await pmod.build_postmortem("ep-1")
        assert "could not be read" in pm["summary"]
        assert "No recorded events for this episode." != pm["summary"]


# ── what `kq replay` does with it ────────────────────────────────────────────


class TestTheClientBranchesOnIt:
    @pytest.mark.parametrize(
        ("meta", "expected"),
        [({"chain_valid": True, "chain_verified": True}, 0),
         ({"chain_valid": True, "chain_verified": False}, 4),
         ({"chain_valid": False, "chain_verified": True}, 3),
         ({"chain_valid": True}, 0)],   # older server: absent field reads as before
    )
    def test_the_documented_code_for_each_state(self, meta, expected, monkeypatch, tmp_path):
        """End to end through the real command, against a fake SSE stream."""
        import httpx

        from kube_q.cli import replay_cmd

        # `kq` merges the *current directory's* `.env` into `os.environ`, so running this from
        # the checkout leaks v4/.env into the process and the next test that reads config sees
        # values no fixture set. An empty directory is the only honest place to run a CLI from.
        monkeypatch.chdir(tmp_path)

        frames = [f"data: {json.dumps({'type': 'replay_meta', 'episode_id': 'ep-1', **meta})}\n\n",
                  f"data: {json.dumps({'type': 'tool_call', 'name': 'x'})}\n\n",
                  "data: [DONE]\n\n"]

        def handler(request):
            return httpx.Response(200, text="".join(frames),
                                  headers={"content-type": "text/event-stream"})

        monkeypatch.setattr(replay_cmd, "make_client",
                            lambda *_a, **_k: httpx.Client(
                                transport=httpx.MockTransport(handler)))
        assert replay_cmd.run(["ep-1"]) == expected
