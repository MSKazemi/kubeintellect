"""ADR-101 read-only fan-out runner — P2 body (v5 specs/00 §R-p0-06/07, roadmap §4).

Covers the seam dispatch, plan decomposition, the isolated subagent loop (fake LLM — no
cluster), deterministic reconciliation, and the robustness guarantees (per-subagent failure
isolation + all-fail fallback to flat gather).
"""
from __future__ import annotations

import pytest
from app.agent.state import PlanStep
from app.cortex import graph as cx
from app.cortex.harness import (
    plan_subagents,
    reconcile_results,
    run_fanout,
    run_subagent,
)
from app.cortex.harness.subagent import (
    ACI_READ_VERB_ALLOWLIST,
    SubagentContract,
    SubagentResult,
)
from langchain_core.messages import AIMessage, HumanMessage


def _state(**over):
    base = {
        "messages": [HumanMessage(content="why is the web pod down?")],
        "session_id": "s1",
        "cluster_snapshot": "",
        "investigation_plan": [],
        "plan_cursor": 0,
        "gather_rounds": 0,
    }
    base.update(over)
    return base


# ── fake LLM (no network) ─────────────────────────────────────────────────────
class _FakeBound:
    def __init__(self, replies):
        self._replies, self._i = list(replies), 0

    async def ainvoke(self, messages, config=None):
        r = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return r


class _FakeLLM:
    """Returns preset AIMessage replies; records the tools it was bound with."""
    def __init__(self, replies):
        self._replies = replies
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return _FakeBound(self._replies)


class TestGatherSeam:
    async def test_flag_off_uses_flat_gather(self, mocker):
        mocker.patch.object(cx.settings, "CORTEX_V5_ENABLED", False)
        mocker.patch.object(cx.settings, "KI_V5_HARNESS_FANOUT", True)
        flat = mocker.patch.object(cx, "gather_once", new=mocker.AsyncMock(return_value={"m": 1}))
        fanout = mocker.patch("app.cortex.harness.runner.run_fanout",
                              new=mocker.AsyncMock(return_value={"m": 2}))
        assert await cx.gather_llm(_state(), {}) == {"m": 1}
        flat.assert_awaited_once()
        fanout.assert_not_awaited()

    async def test_both_flags_on_dispatches_fanout(self, mocker):
        mocker.patch.object(cx.settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(cx.settings, "KI_V5_HARNESS_FANOUT", True)
        fanout = mocker.patch("app.cortex.harness.runner.run_fanout",
                              new=mocker.AsyncMock(return_value={"m": 2}))
        assert await cx.gather_llm(_state(), {}) == {"m": 2}
        fanout.assert_awaited_once()


class TestPlanSubagents:
    def test_one_contract_per_actionable_step_capped(self):
        plan = [PlanStep(description=f"step {i}", status="pending") for i in range(6)]
        contracts = plan_subagents(_state(investigation_plan=plan), 4)
        assert len(contracts) == 4
        assert contracts[0].objective == "step 0"

    def test_skips_done_steps(self):
        plan = [PlanStep(description="done one", status="done"),
                PlanStep(description="live one", status="in_progress")]
        contracts = plan_subagents(_state(investigation_plan=plan), 4)
        assert [c.objective for c in contracts] == ["live one"]

    def test_falls_back_to_user_objective_without_plan(self):
        contracts = plan_subagents(_state(), 4)
        assert len(contracts) == 1
        assert "web pod" in contracts[0].objective

    def test_contracts_are_read_only(self):
        c = plan_subagents(_state(), 4)[0]
        assert c.allowed_verbs == ACI_READ_VERB_ALLOWLIST and c.may_mutate is False

    @pytest.mark.parametrize("cap", [0, -1])
    def test_nonpositive_bound_yields_none(self, cap):
        assert plan_subagents(_state(), cap) == []


class TestRunSubagent:
    async def test_summarizes_when_llm_stops(self):
        llm = _FakeLLM([AIMessage(content="root cause: OOMKilled; raise memory limit")])
        r = await run_subagent(SubagentContract(objective="why down?"), {}, llm=llm)
        assert "OOMKilled" in r.summary
        assert r.verbs_used == ()
        # investigators are bound to exactly the ACI read verbs
        assert {t.name for t in llm.bound_tools} == set(ACI_READ_VERB_ALLOWLIST)

    async def test_runs_read_verb_then_summarizes(self, mocker):
        from app.cortex.harness import runner as rn

        class _FakeTool:
            name = "inspect"
            async def ainvoke(self, args, config=None):
                return "pod/web Running"

        call = AIMessage(content="", tool_calls=[{"name": "inspect", "args": {"kind": "pod", "name": "web"}, "id": "t1"}])
        done = AIMessage(content="pod web is Running; not the culprit")
        llm = _FakeLLM([call, done])
        # swap the ACI verb list for a cluster-free fake (StructuredTool methods aren't patchable).
        mocker.patch.object(rn, "ACI_READ_VERBS", [_FakeTool()])
        r = await run_subagent(SubagentContract(objective="check web"), {}, llm=llm, max_rounds=3)
        assert r.verbs_used == ("inspect",)
        assert "not the culprit" in r.summary

    async def test_failure_is_degraded_not_raised(self):
        boom = mocker_boom()
        r = await run_subagent(SubagentContract(objective="x"), {}, llm=boom)
        assert r.summary.startswith("[investigation error")
        assert isinstance(r, SubagentResult)

    async def test_large_model_flag_selects_synthesis_tier(self, mocker):
        mocker.patch.object(cx.settings, "KI_V5_HARNESS_SUBAGENT_LARGE_MODEL", True)
        big = _FakeLLM([AIMessage(content="done")])
        small = _FakeLLM([AIMessage(content="done")])
        mocker.patch("app.cortex.models.get_synthesis_llm", return_value=big)
        mocker.patch("app.cortex.models.get_specialist_llm", return_value=small)
        await run_subagent(SubagentContract(objective="x"), {})  # llm=None ⇒ picks a tier
        assert big.bound_tools is not None and small.bound_tools is None   # large tier used

    async def test_default_uses_specialist_tier(self, mocker):
        mocker.patch.object(cx.settings, "KI_V5_HARNESS_SUBAGENT_LARGE_MODEL", False)
        big = _FakeLLM([AIMessage(content="done")])
        small = _FakeLLM([AIMessage(content="done")])
        mocker.patch("app.cortex.models.get_synthesis_llm", return_value=big)
        mocker.patch("app.cortex.models.get_specialist_llm", return_value=small)
        await run_subagent(SubagentContract(objective="x"), {})
        assert small.bound_tools is not None and big.bound_tools is None


def mocker_boom():
    class _Boom:
        def bind_tools(self, tools):
            raise RuntimeError("llm exploded")
    return _Boom()


class TestReconcile:
    def test_deterministic_evidence_bundle(self):
        rs = [
            SubagentResult(objective="a", summary="found X", truncated=False, verbs_used=("inspect",)),
            SubagentResult(objective="b", summary="found Y", truncated=True, verbs_used=()),
        ]
        out = reconcile_results(rs)
        assert "Finding 1: a" in out and "Finding 2: b" in out
        assert "read verbs used: inspect" in out
        assert "read verbs used: none (truncated)" in out
        assert reconcile_results(rs) == out  # deterministic


class TestRunFanout:
    async def test_parallel_reconciled_evidence_message(self):
        async def fake_runner(contract, cfg):
            return SubagentResult(objective=contract.objective, summary=f"evidence for {contract.objective}",
                                  truncated=False, verbs_used=("inspect",))
        plan = [PlanStep(description="check pods", status="pending"),
                PlanStep(description="check events", status="pending")]
        out = await run_fanout(_state(investigation_plan=plan), {}, runner=fake_runner)
        msg = out["messages"][0]
        assert isinstance(msg, AIMessage)
        assert not (getattr(msg, "tool_calls", None) or [])   # ⇒ routes to synthesize
        assert "check pods" in msg.content and "check events" in msg.content
        assert out["gather_rounds"] == 1

    async def test_all_failed_falls_back_to_flat_gather(self, mocker):
        flat = mocker.patch.object(cx, "gather_once", new=mocker.AsyncMock(return_value={"flat": True}))

        async def dead_runner(contract, cfg):
            return SubagentResult(objective=contract.objective, summary="[investigation error: boom]",
                                  truncated=False, verbs_used=())
        out = await run_fanout(_state(), {}, runner=dead_runner)
        assert out == {"flat": True}
        flat.assert_awaited_once()

    async def test_no_contracts_falls_back_to_flat_gather(self, mocker):
        mocker.patch.object(cx.settings, "KI_V5_HARNESS_MAX_SUBAGENTS", 0)
        flat = mocker.patch.object(cx, "gather_once", new=mocker.AsyncMock(return_value={"flat": True}))
        out = await run_fanout(_state(), {})
        assert out == {"flat": True}
        flat.assert_awaited_once()
