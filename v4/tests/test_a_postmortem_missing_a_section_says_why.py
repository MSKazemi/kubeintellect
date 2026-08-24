"""A postmortem that could not read something must not look like one with nothing to say.

THE DEFECT
----------
`build_postmortem` enriches the deterministic timeline from two best-effort sources, and both
returned `None` on failure — the same `None` that legitimately means "there was nothing to add":

- `_fetch_episode_meta` supplies **root cause and outcome** from the L1 episode store. It
  returned `None` for "no row for this episode" (the investigation never concluded — a real
  finding about the incident) and for "the query raised" (a revoked grant, schema drift — a fact
  about us). `render_markdown` prints the section under `if pm["root_cause"]:`, so both simply
  omitted it.
- `synthesize_narrative` returned `None` when the feature is off, when there is nothing to
  narrate, and when the model call failed.

Measured 2026-08-24 by driving `build_postmortem` against a `fetchrow` that raises, with "no
row" as the control: the two rendered documents were **byte-identical**, 528 bytes, and both
carried

    > ✅ Audit chain verified intact — every event below is tamper-evident.

That banner is a claim about the *records*. It sat above a document silently missing the section
a reader opens a postmortem for. This file's own predecessor established the distinction —
"Intact and complete are different claims" — for events the recorder lost; the same split had
never been applied to the enrichments the document itself failed to fetch.

WHAT IS ASSERTED
----------------
1. The two failure modes raise, and the two legitimate empties still return None.
2. A failed enrichment is recorded on the postmortem and rendered as a banner, naming what could
   not be read — in both directions, since a banner on every postmortem would be worse than none.
3. Failing an enrichment never costs the reader a recorded fact: the timeline still renders.
"""

from __future__ import annotations

import json
import time

import pytest
from app.core.config import settings
from app.db import flight_recorder
from app.digest import postmortem as P

_ROWS = [
    {"seq": 1, "kind": "finding", "at": time.time(),
     "payload": json.dumps({"playbook": "OOMKilled", "namespace": "prod",
                            "object": "web-1", "severity": "critical"})},
    {"seq": 2, "kind": "tool_call", "at": time.time(),
     "payload": json.dumps({"tool": "run_kubectl", "command": "kubectl describe pod web-1"})},
]

_META = {"summary": "s", "root_cause": "container memory limit too low (128Mi)",
         "outcome": "resolved", "verified": True}


class _Pool:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0

    async def fetchrow(self, sql, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        # `mode` describes the L1 *episode* lookup — the enrichment this file is about. The
        # postmortem also reads the chain anchor through this same pool, and a double that
        # failed both could not express "only the enrichment failed", which is the premise of
        # every test below.
        if "decision_log_head" in sql:
            return None
        self.calls += 1
        if self.mode == "boom":
            raise RuntimeError("permission denied for relation episodes")
        return None if self.mode == "none" else _META


@pytest.fixture
def recorder(mocker):
    """A readable recorder with two events and an intact chain."""

    async def _fetch(_id):
        return list(_ROWS)

    mocker.patch.object(flight_recorder, "fetch_episode", _fetch)
    mocker.patch.object(flight_recorder, "verify_chain", lambda rows: True)
    mocker.patch.object(settings, "POSTMORTEM_LLM_NARRATIVE", False)

    def _with(mode: str) -> _Pool:
        pool = _Pool(mode)
        mocker.patch.object(flight_recorder, "_pool", pool)
        return pool

    return _with


def _banner(md: str) -> str | None:
    hits = [line for line in md.splitlines() if "POSTMORTEM INCOMPLETE" in line]
    return hits[0] if hits else None


# ── 1. the lookup separates its three answers ────────────────────────────────────────────────


class TestTheEpisodeLookup:
    async def test_a_failed_query_raises(self, recorder):
        pool = recorder("boom")
        with pytest.raises(P._EpisodeLookupFailed):
            await P._fetch_episode_meta("ep-1")
        assert pool.calls == 1, "vacuity guard: the query was never attempted"

    async def test_no_row_is_still_none(self, recorder):
        recorder("none")
        assert await P._fetch_episode_meta("ep-1") is None

    async def test_no_pool_is_still_none(self, mocker):
        mocker.patch.object(flight_recorder, "_pool", None)
        assert await P._fetch_episode_meta("ep-1") is None

    async def test_a_row_is_returned(self, recorder):
        recorder("row")
        assert (await P._fetch_episode_meta("ep-1"))["root_cause"] == _META["root_cause"]


# ── 2. the document says what it could not read ──────────────────────────────────────────────


class TestTheDocumentSaysWhatItMissed:
    async def test_a_failed_lookup_is_recorded_and_bannered(self, recorder):
        recorder("boom")
        pm = await P.build_postmortem("ep-1")
        assert pm["enrichment_failed"], "the failure left no trace on the postmortem"
        md = P.render_markdown(pm)
        line = _banner(md)
        assert line is not None
        assert "root cause" in line and "permission denied" in line
        assert "NOT evidence that it was empty" in line

    async def test_no_row_renders_no_banner(self, recorder):
        # The other direction. An investigation that never concluded is a finding, not a fault.
        recorder("none")
        pm = await P.build_postmortem("ep-1")
        assert pm["enrichment_failed"] == []
        assert _banner(P.render_markdown(pm)) is None

    async def test_a_healthy_postmortem_renders_no_banner(self, recorder):
        recorder("row")
        pm = await P.build_postmortem("ep-1")
        md = P.render_markdown(pm)
        assert _banner(md) is None
        assert "## Root cause" in md, "vacuity guard: the section never rendered at all"

    async def test_the_two_cases_are_no_longer_the_same_document(self, recorder):
        recorder("none")
        clean = P.render_markdown(await P.build_postmortem("ep-1"))
        recorder("boom")
        broken = P.render_markdown(await P.build_postmortem("ep-1"))
        assert clean != broken, "a failed lookup still renders exactly what an empty one renders"

    async def test_the_banner_sits_with_the_chain_banners(self, recorder):
        recorder("boom")
        md = P.render_markdown(await P.build_postmortem("ep-1"))
        lines = md.splitlines()
        chain = next(i for i, line in enumerate(lines) if "Audit chain" in line)
        incomplete = next(i for i, line in enumerate(lines) if "POSTMORTEM INCOMPLETE" in line)
        assert incomplete == chain + 1, "the two claims about this document must be read together"


# ── 3. a failed enrichment never costs a recorded fact ───────────────────────────────────────


class TestTheTimelineSurvives:
    async def test_the_timeline_still_renders(self, recorder):
        recorder("boom")
        md = P.render_markdown(await P.build_postmortem("ep-1"))
        assert "## Timeline" in md and "OOMKilled" in md

    async def test_the_chain_verdict_is_unaffected(self, recorder):
        recorder("boom")
        pm = await P.build_postmortem("ep-1")
        assert pm["chain_verified"] is True and pm["chain_valid"] is True

    async def test_a_failed_lookup_does_not_invent_a_root_cause(self, recorder):
        recorder("boom")
        pm = await P.build_postmortem("ep-1")
        assert pm["root_cause"] is None
        assert "## Root cause" not in P.render_markdown(pm)


# ── 4. the narrative, same split ─────────────────────────────────────────────────────────────


class TestTheNarrative:
    async def test_disabled_is_not_a_failure(self, recorder, mocker):
        recorder("row")
        mocker.patch.object(settings, "POSTMORTEM_LLM_NARRATIVE", False)
        pm = await P.build_postmortem("ep-1")
        assert pm["narrative"] is None and pm["enrichment_failed"] == []

    async def test_an_empty_timeline_is_not_a_failure(self, mocker):
        mocker.patch.object(settings, "POSTMORTEM_LLM_NARRATIVE", True)
        assert await P.synthesize_narrative({"timeline": []}) is None

    async def test_a_failed_call_raises_and_is_bannered(self, recorder, mocker):
        recorder("row")
        mocker.patch.object(settings, "POSTMORTEM_LLM_NARRATIVE", True)
        import app.cortex.models as models

        def _boom(*a, **k):
            raise RuntimeError("no API key configured")

        mocker.patch.object(models, "get_synthesis_llm", _boom)
        with pytest.raises(P._NarrativeFailed):
            await P.synthesize_narrative({"timeline": _ROWS, "episode_id": "ep-1"})

        pm = await P.build_postmortem("ep-1")
        assert any("narrative" in x for x in pm["enrichment_failed"])
        line = _banner(P.render_markdown(pm))
        assert line is not None and "no API key configured" in line
        # and the rest of the document is untouched
        assert "## Root cause" in P.render_markdown(pm)

    async def test_both_failures_are_named_together(self, recorder, mocker):
        recorder("boom")
        mocker.patch.object(settings, "POSTMORTEM_LLM_NARRATIVE", True)
        import app.cortex.models as models

        def _boom(*a, **k):
            raise RuntimeError("no API key configured")

        mocker.patch.object(models, "get_synthesis_llm", _boom)
        pm = await P.build_postmortem("ep-1")
        assert len(pm["enrichment_failed"]) == 2
        line = _banner(P.render_markdown(pm))
        assert "root cause" in line and "narrative" in line
