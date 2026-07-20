"""Escalation-avoidance briefs (v5 P2, A-CH-17-07) — build (fail-safe) + render + wiring."""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.cortex import briefs as bm
from app.cortex import graph as cx
from app.cortex.briefs import EscalationBrief, build_brief, render_brief
from app.cortex.briefs import _FALLBACK_ESCALATE_IF


class _LLM:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, messages, config=None):
        return AIMessage(content=self._content)


class _BoomLLM:
    async def ainvoke(self, messages, config=None):
        raise RuntimeError("down")


_GOOD = (
    '{"summary": "web pod OOMKilled", '
    '"actions": ["raise the memory limit to 512Mi", "roll the deployment"], '
    '"escalate_if": ["the pod OOMKills again after the bump"], "confidence": 0.8}'
)


class TestBuildBrief:
    async def test_parses_and_keeps_safety_net(self):
        b = await build_brief("OOMKilled", "evidence", responder_level="junior", llm=_LLM(_GOOD))
        assert b.summary == "web pod OOMKilled"
        assert "raise the memory limit to 512Mi" in b.actions
        assert b.responder_level == "junior" and b.confidence == 0.8 and b.fell_back is False
        # model condition present AND the always-safe fallback conditions appended
        assert "the pod OOMKills again after the bump" in b.escalate_if
        for cond in _FALLBACK_ESCALATE_IF:
            assert cond in b.escalate_if

    async def test_missing_escalate_if_falls_back(self):
        bad = '{"summary": "x", "actions": ["do a thing"], "escalate_if": [], "confidence": 0.9}'
        b = await build_brief("rc", "ev", llm=_LLM(bad))
        assert b.fell_back is True and b.escalate_if == _FALLBACK_ESCALATE_IF

    async def test_garbage_falls_back(self):
        b = await build_brief("rc", "ev", llm=_LLM("no json here"))
        assert b.fell_back is True and b.actions and b.escalate_if

    async def test_exception_falls_back(self):
        b = await build_brief("rc", "ev", llm=_BoomLLM())
        assert b.fell_back is True

    async def test_unknown_level_normalizes(self):
        b = await build_brief("rc", "ev", responder_level="wizard", llm=_LLM(_GOOD))
        assert b.responder_level == "intermediate"


class TestRenderBrief:
    def test_renders_numbered_actions_and_bounds(self):
        b = EscalationBrief(summary="s", actions=["a1", "a2"], escalate_if=["c1"],
                            responder_level="senior", confidence=0.5)
        out = render_brief(b)
        assert "Responder brief (senior)" in out
        assert "1. a1" in out and "2. a2" in out
        assert "Escalate only if:" in out and "- c1" in out
        assert "50%" in out


def _syn_state(mode="investigate", **over):
    base = {
        "messages": [HumanMessage(content="why down?"), ToolMessage(tool_call_id="t", content="pod OOMKilled")],
        "session_id": "s1",
        "investigation_plan": [],
        "turn_start_index": 0,
        "triage_mode": mode,
    }
    base.update(over)
    return base


class TestSynthesizeWiring:
    async def _run(self, mocker, mode, flag):
        mocker.patch.object(cx, "emit", new=mocker.AsyncMock())
        mocker.patch.object(cx.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(cx.settings, "KI_V5_ESCALATION_BRIEFS", flag)
        async def _astream(messages, config=None):
            yield AIMessage(content="Root cause: OOM.")
        fake = mocker.Mock()
        fake.astream = _astream
        mocker.patch("app.cortex.models.get_synthesis_llm", return_value=fake)
        mocker.patch.object(bm, "build_brief", new=mocker.AsyncMock(
            return_value=EscalationBrief(summary="brief!", actions=["a"], escalate_if=["c"])))
        return await cx.synthesize(_syn_state(mode=mode), {})

    async def test_investigate_appends_brief(self, mocker):
        out = await self._run(mocker, "investigate", True)
        assert "Responder brief" in out["messages"][-1].content

    async def test_chat_mode_no_brief(self, mocker):
        out = await self._run(mocker, "chat", True)
        assert "Responder brief" not in out["messages"][-1].content

    async def test_flag_off_no_brief(self, mocker):
        out = await self._run(mocker, "investigate", False)
        assert "Responder brief" not in out["messages"][-1].content
