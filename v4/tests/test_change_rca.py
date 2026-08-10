"""Change-first RCA policy (v5 P2, A-CH-02-07) — rank + render + pluggable source + injection."""
from __future__ import annotations

from app.cortex import graph as cx
from app.cortex.change_rca import (
    ChangeRecord,
    _empty_source,
    rank_by_recency,
    recent_changes,
    render_change_prior,
    set_change_source,
)
from langchain_core.messages import HumanMessage

_CHANGES = [
    ChangeRecord(kind="image", target="deploy/web", ts_epoch=100.0, namespace="demo", detail=":v2 -> :v3"),
    ChangeRecord(kind="config", target="cm/app", ts_epoch=300.0, namespace="demo"),
    ChangeRecord(kind="scale", target="deploy/api", ts_epoch=200.0),
]


def test_rank_by_recency_desc():
    ranked = rank_by_recency(_CHANGES)
    assert [c.ts_epoch for c in ranked] == [300.0, 200.0, 100.0]


class TestRenderPrior:
    def test_empty_is_blank(self):
        assert render_change_prior([]) == ""

    def test_recency_order_and_cap(self):
        out = render_change_prior(_CHANGES, max_items=2)
        assert "79% of outages follow a change" in out
        # most-recent first, capped at 2
        assert out.count("\n- ") == 2
        assert out.index("cm/app") < out.index("deploy/api")

    def test_shows_age_when_now_given(self):
        out = render_change_prior(_CHANGES, now=100.0 + 600)  # 10 min after the oldest
        assert "10m ago" in out          # image change at t=100, now=700 → 10m
        assert "in demo" in out

    def test_no_age_without_now(self):
        assert "ago" not in render_change_prior(_CHANGES)


class TestSource:
    def teardown_method(self):
        set_change_source(_empty_source)   # restore default between tests

    def test_default_source_is_empty(self):
        set_change_source(_empty_source)
        assert recent_changes("cl-1") == []

    def test_registered_source_used(self):
        set_change_source(lambda cid, ns=None: _CHANGES if cid == "cl-1" else [])
        assert len(recent_changes("cl-1")) == 3
        assert recent_changes("other") == []

    def test_source_exception_fails_to_empty(self):
        def boom(cid, ns=None):
            raise RuntimeError("ledger down")
        set_change_source(boom)
        assert recent_changes("cl-1") == []


def _gather_state(**over):
    base = {
        "messages": [HumanMessage(content="why down?")],
        "session_id": "s1",
        "memory_context": "",
        "investigation_plan": [],
        "matched_playbooks": [],
        "cluster_id": "cl-1",
    }
    base.update(over)
    return base


class TestGatherInjection:
    def teardown_method(self):
        set_change_source(_empty_source)

    async def _system_prompt(self, mocker, flag):
        mocker.patch.object(cx, "emit", new=mocker.AsyncMock())
        mocker.patch.object(cx.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(cx.settings, "KI_V5_CHANGE_FIRST_RCA", flag)
        captured = {}
        class _LLM:
            def bind_tools(self, tools):
                return self
            async def ainvoke(self, messages, config=None):
                captured["system"] = messages[0].content
                from langchain_core.messages import AIMessage
                return AIMessage(content="ok")
        mocker.patch("app.cortex.models.get_specialist_llm", return_value=_LLM())
        await cx.gather_once(_gather_state(), {})
        return captured["system"]

    async def test_flag_on_with_changes_injects_prior(self, mocker):
        set_change_source(lambda cid, ns=None: _CHANGES)
        system = await self._system_prompt(mocker, True)
        assert "Recent changes (consider these FIRST" in system

    async def test_flag_on_empty_ledger_is_noop(self, mocker):
        set_change_source(_empty_source)   # the P1-not-yet-populated reality
        system = await self._system_prompt(mocker, True)
        assert "Recent changes" not in system

    async def test_flag_off_no_injection(self, mocker):
        set_change_source(lambda cid, ns=None: _CHANGES)
        system = await self._system_prompt(mocker, False)
        assert "Recent changes" not in system
