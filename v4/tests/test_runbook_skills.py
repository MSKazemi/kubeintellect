"""Runbooks-as-skills v0 (v5 P2) — on-demand rendering + gather-prompt injection."""
from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.agent.playbooks.loader import Playbook
from app.cortex import graph as cx
from app.cortex.skills import render_matched_skills, render_skill


def test_render_skill_has_all_sections():
    pb = Playbook(
        name="OOMKilled",
        investigation_steps=("Describe the pod", "Check memory limits"),
        expected_evidence=("Last State OOMKilled",),
        recommended_fix_template="Raise the memory limit.",
    )
    out = render_skill(pb)
    assert "SKILL: OOMKilled" in out
    assert "1. Describe the pod" in out and "2. Check memory limits" in out
    assert "- Last State OOMKilled" in out
    assert "Raise the memory limit." in out


def test_render_matched_only_known_names():
    out = render_matched_skills(["OOMKilled", "does-not-exist"])
    assert "SKILL: OOMKilled" in out
    assert "loaded on demand" in out
    assert "does-not-exist" not in out


def test_render_matched_empty_returns_blank():
    assert render_matched_skills([]) == ""
    assert render_matched_skills(["nope"]) == ""


def test_render_matched_caps_at_max():
    names = ["OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "Evicted", "NodeNotReady", "QuotaExceeded"]
    out = render_matched_skills(names, max_skills=2)
    assert out.count("### SKILL:") == 2


def _gather_state(**over):
    base = {
        "messages": [HumanMessage(content="why is the pod down?")],
        "session_id": "s1",
        "memory_context": "",
        "investigation_plan": [],
        "matched_playbooks": ["OOMKilled"],
    }
    base.update(over)
    return base


class TestGatherInjection:
    async def _run(self, mocker, flag, matched):
        mocker.patch.object(cx, "emit", new=mocker.AsyncMock())
        mocker.patch.object(cx.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(cx.settings, "KI_V5_RUNBOOK_SKILLS", flag)
        captured = {}
        class _LLM:
            def bind_tools(self, tools):
                return self
            async def ainvoke(self, messages, config=None):
                captured["system"] = messages[0].content
                from langchain_core.messages import AIMessage
                return AIMessage(content="ok")
        mocker.patch("app.cortex.models.get_specialist_llm", return_value=_LLM())
        await cx.gather_once(_gather_state(matched_playbooks=matched), {})
        return captured["system"]

    async def test_flag_on_injects_matched_skill(self, mocker):
        system = await self._run(mocker, True, ["OOMKilled"])
        assert "SKILL: OOMKilled" in system and "loaded on demand" in system

    async def test_flag_off_no_injection(self, mocker):
        system = await self._run(mocker, False, ["OOMKilled"])
        assert "SKILL:" not in system

    async def test_no_match_no_injection(self, mocker):
        system = await self._run(mocker, True, [])
        assert "SKILL:" not in system
