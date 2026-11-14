"""Running out of steps is an escalation the watchtower can see, not an answer.

Measured 2026-11-14 (`papers/v4-operator-r1/data/loop-2026-11-14/budget_probe_output.txt`): at the production
`AGENT_COORDINATOR_RECURSION_LIMIT` (150), LangGraph's `create_react_agent` does NOT raise `GraphRecursionError`.
Its `remaining_steps` check ends the loop with the plain message "Sorry, need more steps to process this request."
after 74 tool calls. So the coordinator's `except GraphRecursionError` escalation never ran, nothing was logged as an
error, and the watchtower recorded an unattended session that had looped 73 times as `watchtower_done`.

`tests/test_agent_loop_budget.py::TestExhaustionEscalates` mocks the agent to *raise* — the behaviour LangGraph shows
only at small limits (25 raises after 12 calls). These tests use the real `create_react_agent` instead.
"""

from __future__ import annotations

from unittest import mock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

import app.agent.nodes.coordinator as coord_mod
from app.core.config import settings

calls = {"n": 0}


@tool
def run_kubectl(command: str) -> str:
    """Pretend kubectl that always fails the same way."""
    calls["n"] += 1
    return "[kubectl exited 1] The request is invalid: … unrecognized type: string"


class _LoopingModel(GenericFakeChatModel):
    """Always asks for one more tool call: the shape of the E3 loop."""

    def bind_tools(self, tools, **kw):
        return self


def _forever():
    i = 0
    while True:
        i += 1
        yield AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "run_kubectl",
                    "args": {"command": "apply -f -"},
                    "id": f"c{i}",
                }
            ],
        )


@pytest.mark.asyncio
async def test_the_coordinator_turns_langgraphs_step_message_into_the_escalation(
    monkeypatch,
):
    calls["n"] = 0
    monkeypatch.setattr(
        coord_mod, "get_coordinator_llm", lambda: _LoopingModel(messages=_forever())
    )
    monkeypatch.setattr(coord_mod, "ALL_TOOLS", [run_kubectl])
    monkeypatch.setattr(coord_mod, "fault_isolated_tool_node", lambda tools: tools)
    state = {
        "messages": [HumanMessage(content="[autonomous investigation] fix the Job")],
        "session_id": "sess-steps-1",
        "findings": [],
        "memory_context": "",
        "cluster_snapshot": "",
    }
    with mock.patch.object(coord_mod.logger, "error") as err:
        result = await coord_mod.coordinator(state, config={"configurable": {}})

    assert calls["n"] > 50, "the real agent must actually have looped to its limit"
    last = result["messages"][-1]
    assert "Sorry, need more steps" not in last.content
    assert "without reaching a conclusion" in last.content
    assert str(settings.AGENT_COORDINATOR_RECURSION_LIMIT) in last.content
    assert last.additional_kwargs.get("ki_outcome") == "budget_exhausted"
    assert err.called, (
        "running out of steps is logged as an error, as the exception path does"
    )
    # The work done before the limit is kept for the report, not thrown away.
    assert any(getattr(m, "type", "") == "tool" for m in result["messages"])


def test_the_sentinel_is_recognised_only_as_the_whole_final_message():
    s = "Sorry, need more steps to process this request."
    assert coord_mod._ran_out_of_steps(AIMessage(content=s))
    assert not coord_mod._ran_out_of_steps(
        AIMessage(content="Here is the diagnosis. " + s)
    )
    assert not coord_mod._ran_out_of_steps(HumanMessage(content=s))


@pytest.mark.asyncio
async def test_the_watchtower_records_an_escalation_not_done(monkeypatch):
    from app.autonomy import watchtower as wt

    class _Snap:
        values = {
            "messages": [
                AIMessage(
                    content="I stopped …",
                    additional_kwargs={"ki_outcome": "budget_exhausted"},
                )
            ]
        }

    class _Graph:
        async def aget_state(self, config):
            return _Snap()

    async def _get_graph():
        return _Graph()

    monkeypatch.setattr("app.agent.workflow.get_graph", _get_graph)
    assert await wt._turn_outcome("auto-x") == "budget_exhausted"

    _Snap.values = {"messages": [AIMessage(content="The job is fixed.")]}
    assert await wt._turn_outcome("auto-x") is None
