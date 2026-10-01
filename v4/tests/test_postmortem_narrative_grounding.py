"""The LLM postmortem narrative is checked claim by claim against the record (ADR-011 × ADR-009).

The narrative prompt asks the model to "invent nothing". A pre-registered field campaign
measured how far that request holds: 0.70 and 0.61 of narrative claims were supported by the
incident record, with 36 and 54 unsupported claims across two runs. Until this gate, every one
of those sentences reached the reader under a ✅ "audit chain verified intact" banner.

Each fake narrative below mixes claims the recorded events support with claims they do not —
an unrecorded pod, an invented image tag, a seq that does not exist, a causal story nobody
recorded. Before the gate the whole narrative was attached verbatim; these tests fail on that.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.db import flight_recorder
from app.digest import postmortem

_EVENTS = [
    ("status", {"message": "investigation started"}),
    ("finding", {"playbook": "OOMKilled", "namespace": "shop", "object": "web-1",
                 "severity": "warning"}),
    ("tool_call", {"tool": "run_kubectl", "command": "kubectl describe pod web-1 -n shop"}),
    ("tool_result", {"summary": "Last State.Reason=OOMKilled, limit 128Mi"}),
    ("rollback_point", {"rollback_id": "rb-abc123", "command": "kubectl delete pod web-1 -n shop"}),
    ("answer", {"text": "Root cause: container exceeded its 128Mi memory limit. Raised to 256Mi."}),
]

_GROUNDED = [
    "[#1] The OOMKilled detector fired on shop/web-1.",
    "[#3] The container hit its 128Mi memory limit.",
    "[#4] Rollback point rb-abc123 was armed before the delete.",
    "[#5] The pod restarted because the container exceeded its 128Mi memory limit.",
]
_UNGROUNDED = [
    "[#2] Pod web-7 in namespace payments was also restarted.",      # unrecorded resource
    "The outage was caused by a bad rollout of nginx:1.27.",         # invented image + cause
    "[#9] Memory was raised to 512Mi at 03:14.",                      # seq 9 does not exist
    "[#3] This happened because a noisy neighbour exhausted node memory.",  # unrecorded cause
]
# One token from each ungrounded claim that appears nowhere in the record.
_INVENTED = ("web-7", "nginx:1.27", "512Mi", "noisy neighbour")

_MIXED = "## Summary\n" + "\n".join(f"- {c}" for c in _GROUNDED + _UNGROUNDED)
_CLEAN = "## Summary\n" + "\n".join(f"- {c}" for c in _GROUNDED)


def _chain(episode_id: str, events: list[tuple[str, dict]]) -> list[dict]:
    rows, prev = [], ""
    for seq, (kind, payload) in enumerate(events):
        h = flight_recorder.compute_hash(prev, episode_id, seq, kind, payload)
        rows.append({"episode_id": episode_id, "seq": seq, "kind": kind,
                     "payload": json.dumps(payload), "prev_hash": prev, "hash": h,
                     "created_at": datetime.now(tz=timezone.utc)})
        prev = h
    return rows


class _NullPool:
    """Answers every lookup with "asked, and there is nothing"."""

    async def fetchrow(self, _sql, *_args):
        return None


@pytest.fixture(autouse=True)
def _recorder(mocker):
    mocker.patch.object(flight_recorder, "_pool", _NullPool())

    async def _fetch(_episode_id):
        return _chain("ep-1", _EVENTS)
    mocker.patch.object(flight_recorder, "fetch_episode", side_effect=_fetch)


def _llm_says(mocker, text: str) -> None:
    class FakeResp:
        content = text

    class FakeLLM:
        async def ainvoke(self, _messages):
            return FakeResp()

    mocker.patch.object(postmortem.settings, "POSTMORTEM_LLM_NARRATIVE", True)
    mocker.patch("app.cortex.models.get_synthesis_llm", return_value=FakeLLM())


async def _record_only(mocker) -> dict:
    mocker.patch.object(postmortem.settings, "POSTMORTEM_LLM_NARRATIVE", False)
    return await postmortem.build_postmortem("ep-1")


class TestClaimSplittingAndGrounding:
    async def test_mixed_narrative_is_counted_claim_by_claim(self, mocker):
        pm = await _record_only(mocker)
        kept, total, ungrounded = postmortem.ground_narrative(_MIXED, pm)
        assert (total, ungrounded) == (8, 4), (
            "every bullet is one claim and exactly the four invented ones are unsupported")
        for token in _INVENTED:
            assert token not in kept, f"an unsupported claim survived: {token!r}"
        for token in ("shop/web-1", "rb-abc123", "because the container exceeded"):
            assert token in kept, f"a supported claim was removed: {token!r}"

    async def test_sentences_inside_one_line_are_separate_claims(self, mocker):
        pm = await _record_only(mocker)
        line = f"{_GROUNDED[0]} {_UNGROUNDED[0]}"
        kept, total, ungrounded = postmortem.ground_narrative(line, pm)
        assert (total, ungrounded) == (2, 1)
        assert "shop/web-1" in kept and "web-7" not in kept

    async def test_a_claim_with_nothing_to_check_is_not_grounded(self, mocker):
        pm = await _record_only(mocker)
        _kept, total, ungrounded = postmortem.ground_narrative(
            "The team responded quickly and professionally.", pm)
        assert (total, ungrounded) == (1, 1), "an unverifiable sentence is not a verified one"

    async def test_numbers_match_whole_tokens_only(self, mocker):
        pm = await _record_only(mocker)
        # "123" occurs inside "rb-abc123" — a substring match would wrongly call this grounded.
        _kept, _total, ungrounded = postmortem.ground_narrative(
            "[#3] The limit was 123 units.", pm)
        assert ungrounded == 1
        # ...while a number followed by its unit in the record is the same number.
        _kept, _total, ungrounded = postmortem.ground_narrative(
            "[#3] The memory limit was 128 mebibytes.", pm)
        assert ungrounded == 0

    async def test_hyphenated_english_is_not_mistaken_for_a_resource(self, mocker):
        pm = await _record_only(mocker)
        _k, _t, english = postmortem.ground_narrative(
            "[#2] A follow-up read-only check was run.", pm)
        _k, _t, named = postmortem.ground_narrative(
            "[#2] The payment-api deployment was checked.", pm)
        assert english == 0, "ordinary hyphenated words must not fail the gate"
        assert named == 1, "a resource name beside its kind must be found in the record"


class TestTheGate:
    async def test_below_the_floor_the_narrative_is_withheld(self, mocker):
        _llm_says(mocker, _MIXED)
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["narrative"] is None, "a half-invented narrative was presented as fact"
        assert pm["claims_total"] == 8 and pm["claims_ungrounded"] == 4
        assert pm["grounding_rate"] == pytest.approx(0.5)
        assert "POSTMORTEM_MIN_GROUNDING" in pm["narrative_withheld"]
        md = postmortem.render_markdown(pm)
        assert "LLM NARRATIVE WITHHELD" in md
        for token in _INVENTED:
            assert token not in md
        assert "[#0]" in md, "the deterministic postmortem must still be returned"

    async def test_above_the_floor_unsupported_claims_are_removed_and_counted(self, mocker):
        _llm_says(mocker, _MIXED)
        mocker.patch.object(postmortem.settings, "POSTMORTEM_MIN_GROUNDING", 0.5)
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["narrative"] is not None and pm["narrative_withheld"] is None
        for token in _INVENTED:
            assert token not in pm["narrative"], f"unsupported claim shown as fact: {token!r}"
        assert "rb-abc123" in pm["narrative"]
        assert "4 of 8 narrative claim(s) were removed" in postmortem.render_markdown(pm)

    async def test_a_fully_grounded_narrative_passes_intact(self, mocker):
        _llm_says(mocker, _CLEAN)
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["narrative"] == _CLEAN
        assert (pm["claims_total"], pm["claims_ungrounded"]) == (4, 0)
        assert pm["grounding_rate"] == pytest.approx(1.0)
        assert "were removed" not in postmortem.render_markdown(pm)

    async def test_a_narrative_with_no_claims_is_withheld(self, mocker):
        _llm_says(mocker, "## Summary")
        pm = await postmortem.build_postmortem("ep-1")
        assert pm["narrative"] is None
        assert pm["grounding_rate"] is None, "zero claims is not a perfect score"
        assert pm["narrative_withheld"]

    async def test_no_narrative_means_not_measured(self, mocker):
        pm = await _record_only(mocker)
        assert pm["grounding_rate"] is None
        assert (pm["claims_total"], pm["claims_ungrounded"]) == (0, 0)
        assert pm["narrative_withheld"] is None, "the flag being off is not a gate refusal"

    def test_defaults_are_unchanged_and_the_floor_is_strict(self):
        fields = Settings.model_fields
        assert fields["POSTMORTEM_LLM_NARRATIVE"].default is False
        assert fields["POSTMORTEM_MIN_GROUNDING"].default == pytest.approx(0.9)


class TestTheRateIsObservable:
    async def test_markdown_endpoint_carries_the_grounding_fields(self, mocker):
        from app.main import app

        _llm_says(mocker, _MIXED)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            r = await client.get("/v1/episodes/ep-1/postmortem", params={"format": "markdown"})
        body = r.json()
        assert body["claims_total"] == 8 and body["claims_ungrounded"] == 4
        assert body["grounding_rate"] == pytest.approx(0.5)
        assert body["narrative_withheld"]
