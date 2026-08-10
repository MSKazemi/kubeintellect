"""Verification ladder, read side (v5 P2) — goal evaluator + adversarial RCA reviewer.

Fake-LLM unit tests (no network) plus the synthesize wiring behind KI_V5_VERIFY_LADDER.
"""
from __future__ import annotations

from app.cortex import graph as cx
from app.cortex import verify
from app.cortex.verify import (
    GoalVerdict,
    RcaReview,
    evaluate_goal,
    render_review_note,
    review_rca,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class _LLM:
    """One-shot fake chat model returning a preset content string."""
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, messages, config=None):
        return AIMessage(content=self._content)


class _BoomLLM:
    async def ainvoke(self, messages, config=None):
        raise RuntimeError("llm down")


class TestEvaluateGoal:
    async def test_parses_sufficient(self):
        v = await evaluate_goal("why down?", "pod OOMKilled", llm=_LLM('{"sufficient": true, "missing": []}'))
        assert v.sufficient is True and v.missing == []

    async def test_parses_insufficient_with_missing(self):
        v = await evaluate_goal("why?", "little", llm=_LLM('{"sufficient": false, "missing": ["logs", "events"]}'))
        assert v.sufficient is False and v.missing == ["logs", "events"]

    async def test_garbage_fails_open_to_sufficient(self):
        v = await evaluate_goal("q", "e", llm=_LLM("not json at all"))
        assert v.sufficient is True

    async def test_exception_fails_open(self):
        v = await evaluate_goal("q", "e", llm=_BoomLLM())
        assert isinstance(v, GoalVerdict) and v.sufficient is True


class TestReviewRca:
    async def test_supported(self):
        r = await review_rca("OOMKilled", "evidence", llm=_LLM('{"supported": true, "confidence": 0.9, "unsupported": []}'))
        assert r.supported is True and r.confidence == 0.9 and r.errored is False

    async def test_unsupported_list_overrides_supported_bool(self):
        # Model says supported=true but lists an unsupported claim → treat as NOT supported.
        r = await review_rca("claim", "ev", llm=_LLM('{"supported": true, "confidence": 0.4, "unsupported": ["the DNS theory"]}'))
        assert r.supported is False and r.unsupported == ["the DNS theory"]

    async def test_confidence_clamped(self):
        r = await review_rca("c", "e", llm=_LLM('{"supported": true, "confidence": 5, "unsupported": []}'))
        assert r.confidence == 1.0

    async def test_bad_confidence_defaults_zero(self):
        r = await review_rca("c", "e", llm=_LLM('{"supported": true, "confidence": "high", "unsupported": []}'))
        assert r.confidence == 0.0

    async def test_exception_fails_open_errored(self):
        r = await review_rca("c", "e", llm=_BoomLLM())
        assert r.supported is True and r.errored is True


class TestRenderNote:
    def test_supported_renders_empty(self):
        assert render_review_note(RcaReview(supported=True, confidence=0.9)) == ""

    def test_errored_renders_empty(self):
        assert render_review_note(RcaReview(supported=False, confidence=0.0, unsupported=["x"], errored=True)) == ""

    def test_unsupported_renders_caveat(self):
        note = render_review_note(RcaReview(supported=False, confidence=0.3, unsupported=["theory A", "theory B"]))
        assert "⚠ Verification" in note and "theory A" in note and "theory B" in note
        assert "30%" in note


def _syn_state(**over):
    base = {
        "messages": [HumanMessage(content="why is web down?"), ToolMessage(tool_call_id="t1", content="pod web OOMKilled")],
        "session_id": "s1",
        "investigation_plan": [],
        "turn_start_index": 0,
    }
    base.update(over)
    return base


class TestSynthesizeWiring:
    async def test_flag_off_no_review(self, mocker):
        mocker.patch.object(cx, "emit", new=mocker.AsyncMock())
        mocker.patch.object(cx.settings, "CORTEX_V5_ENABLED", False)
        mocker.patch.object(cx.settings, "KI_V5_VERIFY_LADDER", True)
        mocker.patch.object(cx, "get_synthesis_llm", create=True)
        # stream a fixed answer
        async def _astream(messages, config=None):
            yield AIMessage(content="Root cause: OOM.")
        fake = mocker.Mock()
        fake.astream = _astream
        mocker.patch("app.cortex.models.get_synthesis_llm", return_value=fake)
        spy = mocker.patch.object(verify, "review_rca", new=mocker.AsyncMock())
        out = await cx.synthesize(_syn_state(), {})
        assert out["messages"][-1].content == "Root cause: OOM."
        spy.assert_not_awaited()

    async def test_flag_on_appends_caveat(self, mocker):
        mocker.patch.object(cx, "emit", new=mocker.AsyncMock())
        mocker.patch.object(cx.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(cx.settings, "KI_V5_VERIFY_LADDER", True)
        async def _astream(messages, config=None):
            yield AIMessage(content="Root cause: definitely DNS.")
        fake = mocker.Mock()
        fake.astream = _astream
        mocker.patch("app.cortex.models.get_synthesis_llm", return_value=fake)
        mocker.patch.object(verify, "review_rca", new=mocker.AsyncMock(
            return_value=RcaReview(supported=False, confidence=0.2, unsupported=["the DNS claim"])))
        out = await cx.synthesize(_syn_state(), {})
        content = out["messages"][-1].content
        assert content.startswith("Root cause: definitely DNS.")
        assert "⚠ Verification" in content and "the DNS claim" in content

    def test_gathered_evidence_bounds_and_joins(self):
        state = _syn_state(messages=[HumanMessage(content="q"), ToolMessage(tool_call_id="t", content="EVIDENCE_LINE")])
        ev = cx._gathered_evidence(state)
        assert "EVIDENCE_LINE" in ev and len(ev) <= 8000


class TestSynthesizeSnapshotGrounding:
    async def _run(self, mocker, fanout_on, snapshot):
        mocker.patch.object(cx, "emit", new=mocker.AsyncMock())
        mocker.patch.object(cx.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(cx.settings, "KI_V5_HARNESS_FANOUT", fanout_on)
        seen = {}
        async def _astream(messages, config=None):
            seen["system"] = "\n".join(m.content for m in messages if isinstance(m.content, str))
            yield AIMessage(content="answer")
        fake = mocker.Mock()
        fake.astream = _astream
        mocker.patch("app.cortex.models.get_synthesis_llm", return_value=fake)
        await cx.synthesize(_syn_state(cluster_snapshot=snapshot), {})
        return seen["system"]

    async def test_snapshot_grounds_answer_when_fanout_on(self, mocker):
        system = await self._run(mocker, True, "pod/crasher CrashLoopBackOff")
        assert "ground truth" in system and "pod/crasher CrashLoopBackOff" in system

    async def test_no_snapshot_injection_when_fanout_off(self, mocker):
        system = await self._run(mocker, False, "pod/crasher CrashLoopBackOff")
        assert "ground truth" not in system
