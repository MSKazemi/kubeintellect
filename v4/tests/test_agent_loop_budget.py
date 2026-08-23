"""The agent's loop budgets are a SAFETY control, and these tests exist to keep them one.

Found 2026-08-19: the bound existed exactly where the agent was harmless and was missing
exactly where it could mutate the cluster.

- ``agent/nodes/subagent.py`` bounded the **read-only** RCA subagents at 50.
- The **coordinator** — which holds the write-capable ``ALL_TOOLS`` — and the outer graph
  both inherited LangGraph's default ``recursion_limit`` of **10007** (~3,300 ReAct steps).
  ``langgraph/_internal/_config.py``: ``DEFAULT_RECURSION_LIMIT = int(getenv(..., "10007"))``.
- ``GraphRecursionError`` was caught nowhere in the codebase, so exhausting the budget
  destroyed the whole turn instead of returning what had been found.

Nothing else can catch this class of defect: the limit is a *default*, so every test passes
whether or not it is set, and a runaway loop only shows up in production. These tests assert
the bound exists, that it is nowhere near the library default, and that exhausting it halts
and escalates rather than truncating silently.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError

from langgraph import prebuilt

from app.agent import workflow as wf
from app.agent.nodes import coordinator as coord_mod
from app.core.config import settings

# LangGraph 1.x's default. If the coordinator or the graph is ever seen carrying this,
# the explicit budget has been removed or is not reaching the runnable.
LANGGRAPH_DEFAULT_RECURSION_LIMIT = 10007


# ── The bounds exist, and are actually bounds ─────────────────────────────────

class TestBudgetsAreConfigured:
    def test_both_budgets_are_positive(self):
        assert settings.AGENT_GRAPH_RECURSION_LIMIT > 0
        assert settings.AGENT_COORDINATOR_RECURSION_LIMIT > 0

    def test_budgets_are_far_below_the_library_default(self):
        """A 'bound' of 10007 is not a bound; it is the absence of one."""
        assert settings.AGENT_GRAPH_RECURSION_LIMIT < LANGGRAPH_DEFAULT_RECURSION_LIMIT / 10
        assert settings.AGENT_COORDINATOR_RECURSION_LIMIT < LANGGRAPH_DEFAULT_RECURSION_LIMIT / 10

    def test_write_capable_coordinator_is_bounded_at_least_as_tightly_as_a_thousand_steps(self):
        """The loop that can mutate the cluster must not be looser than the read-only one by orders
        of magnitude. It may legitimately be larger — it orchestrates as well as acts."""
        assert settings.AGENT_COORDINATOR_RECURSION_LIMIT <= 1000


# ── The bound reaches the runnable ────────────────────────────────────────────

class TestBudgetReachesTheRunnable:
    @pytest.mark.asyncio
    async def test_invoke_sets_an_explicit_graph_recursion_limit(self, monkeypatch):
        seen: dict = {}

        class _Graph:
            async def ainvoke(self, state, config=None):
                seen["config"] = config
                return state

        async def _get_graph():
            return _Graph()

        monkeypatch.setattr(wf, "get_graph", _get_graph)
        monkeypatch.setattr(wf, "get_langfuse_callbacks", lambda: None)
        await wf.invoke("get pods", "sess-budget-1")

        assert seen["config"]["recursion_limit"] == settings.AGENT_GRAPH_RECURSION_LIMIT
        assert seen["config"]["recursion_limit"] != LANGGRAPH_DEFAULT_RECURSION_LIMIT

    @pytest.mark.asyncio
    async def test_coordinator_overrides_the_inherited_limit(self, monkeypatch):
        """The coordinator is invoked with the *parent* config; without an explicit override it
        silently inherits 10007."""
        seen: dict = {}

        class _Agent:
            async def ainvoke(self, payload, config=None):
                seen["config"] = config
                return {"messages": list(payload["messages"]) + [AIMessage(content="done")]}

        monkeypatch.setattr(prebuilt, "create_react_agent", lambda llm, tools: _Agent())
        monkeypatch.setattr(coord_mod, "get_coordinator_llm", lambda: object())

        state = {
            "messages": [HumanMessage(content="get pods")],
            "session_id": "sess-budget-2",
            "findings": [],
            "memory_context": "",
            "cluster_snapshot": "",
        }
        parent_config = {"configurable": {"thread_id": "sess-budget-2", "user_role": "admin"}}
        await coord_mod.coordinator(state, config=parent_config)

        assert seen["config"]["recursion_limit"] == settings.AGENT_COORDINATOR_RECURSION_LIMIT
        assert seen["config"]["recursion_limit"] != LANGGRAPH_DEFAULT_RECURSION_LIMIT

    @pytest.mark.asyncio
    async def test_coordinator_override_preserves_the_parent_config(self, monkeypatch):
        """Bounding the loop must not drop `user_role` (RBAC) or `hitl_bypass` (the HITL gate) —
        both travel only in the run config."""
        seen: dict = {}

        class _Agent:
            async def ainvoke(self, payload, config=None):
                seen["config"] = config
                return {"messages": [AIMessage(content="done")]}

        monkeypatch.setattr(prebuilt, "create_react_agent", lambda llm, tools: _Agent())
        monkeypatch.setattr(coord_mod, "get_coordinator_llm", lambda: object())

        state = {
            "messages": [HumanMessage(content="scale it")],
            "session_id": "sess-budget-3",
            "findings": [],
            "memory_context": "",
            "cluster_snapshot": "",
        }
        parent_config = {
            "configurable": {"thread_id": "s", "user_role": "viewer", "hitl_bypass": True},
        }
        await coord_mod.coordinator(state, config=parent_config)

        assert seen["config"]["configurable"]["user_role"] == "viewer"
        assert seen["config"]["configurable"]["hitl_bypass"] is True


# ── Exhaustion halts and escalates; it never truncates silently ───────────────

class TestExhaustionEscalates:
    @pytest.mark.asyncio
    async def test_coordinator_budget_exhaustion_returns_an_escalation_not_an_exception(
        self, monkeypatch
    ):
        class _Agent:
            async def ainvoke(self, payload, config=None):
                raise GraphRecursionError("limit reached")

        monkeypatch.setattr(prebuilt, "create_react_agent", lambda llm, tools: _Agent())
        monkeypatch.setattr(coord_mod, "get_coordinator_llm", lambda: object())

        state = {
            "messages": [HumanMessage(content="why is everything broken")],
            "session_id": "sess-budget-4",
            "findings": [],
            "memory_context": "",
            "cluster_snapshot": "",
        }
        result = await coord_mod.coordinator(state, config={"configurable": {}})

        assert result["messages"], "exhaustion must still produce a message for the operator"
        text = result["messages"][-1].content
        assert "budget" in text.lower()
        # The operator must be told the work is INCOMPLETE, not handed a truncated answer.
        assert "without reaching a conclusion" in text
        assert str(settings.AGENT_COORDINATOR_RECURSION_LIMIT) in text

    @pytest.mark.asyncio
    async def test_invoke_budget_exhaustion_returns_partial_state_with_an_escalation(
        self, monkeypatch
    ):
        earlier = AIMessage(content="I checked the pods and found a CrashLoopBackOff")

        class _Snapshot:
            values = {"messages": [earlier], "session_id": "sess-budget-5"}

        class _Graph:
            async def ainvoke(self, state, config=None):
                raise GraphRecursionError("limit reached")

            async def aget_state(self, config):
                return _Snapshot()

        async def _get_graph():
            return _Graph()

        monkeypatch.setattr(wf, "get_graph", _get_graph)
        monkeypatch.setattr(wf, "get_langfuse_callbacks", lambda: None)

        result = await wf.invoke("what is wrong", "sess-budget-5")

        # The partial work survives — losing it was the original defect.
        assert earlier in result["messages"]
        assert "step budget" in result["messages"][-1].content
        assert str(settings.AGENT_GRAPH_RECURSION_LIMIT) in result["messages"][-1].content

    def test_budget_event_translates_to_a_visible_operator_message(self):
        """An untranslated event is dropped by `_translate_raw_event` — i.e. silently. This is
        the streaming path's half of the same guarantee."""
        event = wf._translate_raw_event(
            "sess-budget-6",
            {"event": "on_agent_budget_exhausted", "data": {"limit": 120}},
        )
        assert event is not None, "the halt must reach the operator, not be swallowed"
        assert "budget" in event.content.lower()
        assert "120" in event.content
