"""Cortex V4 graph — triage parsing, routing, tool loop, plan transitions (P4)."""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.state import PlanStep
from app.cortex import graph as cx


def _state(**over):
    base = {
        "messages": [HumanMessage(content="why is my pod crashing?")],
        "session_id": "s1",
        "user_id": "u1",
        "user_role": "admin",
        "memory_context": "",
        "cluster_snapshot": "",
        "matched_playbooks": [],
        "investigation_plan": [],
        "plan_cursor": 0,
        "gather_rounds": 0,
        "turn_start_index": 0,
        "triage_mode": "investigate",
    }
    base.update(over)
    return base


class TestTriageParsing:
    def test_valid_json(self):
        parsed = cx._parse_triage_json('{"mode": "chat", "plan": []}')
        assert parsed["mode"] == "chat"

    def test_json_embedded_in_prose(self):
        parsed = cx._parse_triage_json(
            'Sure! {"mode": "investigate", "plan": ["check pods"]} done'
        )
        assert parsed["plan"] == ["check pods"]

    def test_garbage_defaults_to_investigate(self):
        assert cx._parse_triage_json("not json")["mode"] == "investigate"
        assert cx._parse_triage_json('{"mode": "nonsense"}')["mode"] == "investigate"


class TestRouting:
    def test_chat_routes_to_synthesize(self):
        assert cx.route_triage(_state(triage_mode="chat")) == "synthesize"

    def test_investigate_routes_to_gather(self):
        assert cx.route_triage(_state(triage_mode="investigate")) == "gather_llm"

    def test_tool_calls_route_to_tools(self):
        msg = AIMessage(content="", tool_calls=[
            {"name": "run_kubectl", "args": {"command": "get pods"}, "id": "c1"},
        ])
        state = _state(messages=[msg], gather_rounds=1)
        assert cx.route_gather(state) == "gather_tools"

    def test_no_tool_calls_route_to_synthesize(self):
        state = _state(messages=[AIMessage(content="EVIDENCE_COMPLETE")])
        assert cx.route_gather(state) == "synthesize"

    def test_round_budget_forces_synthesize(self):
        msg = AIMessage(content="", tool_calls=[
            {"name": "run_kubectl", "args": {}, "id": "c1"},
        ])
        state = _state(messages=[msg], gather_rounds=99)
        assert cx.route_gather(state) == "synthesize"


class TestGatherTools:
    async def test_executes_and_advances_plan(self, mocker):
        mocker.patch.object(cx, "emit")
        fake_tool = mocker.AsyncMock()
        fake_tool.ainvoke.return_value = "pod list output"
        mocker.patch.dict(cx._TOOLS_BY_NAME, {"run_kubectl": fake_tool})

        msg = AIMessage(content="", tool_calls=[
            {"name": "run_kubectl", "args": {"command": "get pods -n s"}, "id": "c1"},
        ])
        plan = [
            PlanStep(description="list pods", status="in_progress"),
            PlanStep(description="check logs", status="pending"),
        ]
        out = await cx.gather_tools(
            _state(messages=[msg], investigation_plan=plan, plan_cursor=0), {}
        )
        assert isinstance(out["messages"][0], ToolMessage)
        assert out["messages"][0].content == "pod list output"
        assert out["messages"][0].tool_call_id == "c1"
        assert out["investigation_plan"][0].status == "done"
        assert out["investigation_plan"][1].status == "in_progress"
        assert out["plan_cursor"] == 1

    async def test_unknown_tool_yields_error_message(self, mocker):
        mocker.patch.object(cx, "emit")
        msg = AIMessage(content="", tool_calls=[
            {"name": "nope_tool", "args": {}, "id": "c9"},
        ])
        out = await cx.gather_tools(_state(messages=[msg]), {})
        assert "Unknown tool" in out["messages"][0].content

    async def test_tool_exception_becomes_error_toolmessage(self, mocker):
        mocker.patch.object(cx, "emit")
        fake_tool = mocker.AsyncMock()
        fake_tool.ainvoke.side_effect = RuntimeError("boom")
        mocker.patch.dict(cx._TOOLS_BY_NAME, {"run_kubectl": fake_tool})
        msg = AIMessage(content="", tool_calls=[
            {"name": "run_kubectl", "args": {}, "id": "c1"},
        ])
        out = await cx.gather_tools(_state(messages=[msg]), {})
        assert "Tool error: boom" in out["messages"][0].content

    async def test_graph_interrupt_propagates(self, mocker):
        """HITL interrupts must escape the executor untouched."""
        import pytest
        from langgraph.errors import GraphInterrupt

        mocker.patch.object(cx, "emit")
        fake_tool = mocker.AsyncMock()
        fake_tool.ainvoke.side_effect = GraphInterrupt()
        mocker.patch.dict(cx._TOOLS_BY_NAME, {"run_kubectl": fake_tool})
        msg = AIMessage(content="", tool_calls=[
            {"name": "run_kubectl", "args": {"command": "delete pod x"}, "id": "c1"},
        ])
        with pytest.raises(GraphInterrupt):
            await cx.gather_tools(_state(messages=[msg]), {})


class TestSynthesize:
    async def test_streams_tokens_and_skips_open_steps(self, mocker):
        emitted = []

        async def _capture(sid, event):
            emitted.append(event)

        mocker.patch.object(cx, "emit", side_effect=_capture)

        async def _astream(messages, config):
            for text in ("All ", "good."):
                chunk = mocker.MagicMock()
                chunk.content = text
                yield chunk

        fake_llm = mocker.MagicMock()
        fake_llm.astream = _astream
        mocker.patch("app.cortex.models.get_synthesis_llm", return_value=fake_llm)

        plan = [PlanStep(description="a", status="done"),
                PlanStep(description="b", status="pending")]
        out = await cx.synthesize(_state(investigation_plan=plan), {})

        assert out["messages"][0].content == "All good."
        assert out["investigation_plan"][1].status == "skipped"
        token_events = [e for e in emitted if getattr(e, "type", "") == "token"]
        assert [t.content for t in token_events] == ["All ", "good."]


class TestGraphShape:
    def test_compiles_with_expected_nodes(self):
        compiled = cx.build_cortex_graph().compile()
        assert {"triage", "gather_llm", "gather_tools", "synthesize", "remember"} <= set(
            compiled.nodes.keys()
        )

    def test_no_sentinel_regexes_in_cortex(self):
        """The V4 path must not parse routing sentinels out of prose."""
        import inspect
        source = inspect.getsource(cx)
        for sentinel in ("RCA_REQUIRED", "TARGETED:", "INVESTIGATION_PLAN:"):
            assert sentinel not in source


class TestRoundBudgetBoundary:
    async def test_dangling_tool_calls_closed_before_synthesis(self, mocker):
        """Round-budget exhaustion with pending tool_calls must not leave an
        orphaned assistant message (provider 400) — found in the full
        62-scenario regression."""
        sent = {}

        async def _astream(messages, config):
            sent["messages"] = messages
            chunk = mocker.MagicMock()
            chunk.content = "done"
            yield chunk

        fake_llm = mocker.MagicMock()
        fake_llm.astream = _astream
        mocker.patch("app.cortex.models.get_synthesis_llm", return_value=fake_llm)
        mocker.patch.object(cx, "emit")

        dangling = AIMessage(content="", tool_calls=[
            {"name": "query_loki", "args": {}, "id": "dangle-1"},
        ])
        out = await cx.synthesize(_state(messages=[dangling]), {})

        closers = [m for m in sent["messages"] if isinstance(m, ToolMessage)]
        assert closers and closers[0].tool_call_id == "dangle-1"
        returned_tool_msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
        assert returned_tool_msgs and returned_tool_msgs[0].tool_call_id == "dangle-1"
