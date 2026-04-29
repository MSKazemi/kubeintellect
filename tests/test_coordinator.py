"""
Unit tests for coordinator routing logic.

route_coordinator() is a pure function — tested here without an LLM,
without a real graph, and without Postgres.

The coordinator node itself (LLM + tool calls) is not unit-tested here;
that belongs in integration / eval tests against a real cluster.
"""
import pytest
from langchain_core.messages import HumanMessage
from langgraph.graph import END
from langgraph.types import Send

from app.agent.state import AgentFinding, AgentState, RCAResult
from app.agent.workflow import route_coordinator


def _state(**overrides) -> dict:
    """Return a minimal AgentState-like dict suitable for route_coordinator."""
    base = {
        "messages": [HumanMessage(content="get all pods")],
        "session_id": "test-session-001",
        "user_id": "tester",
        "user_role": "admin",
        "rca_required": False,
        "rca_result": None,
        "findings": [],
        "memory_context": "",
        "pending_hitl": None,
    }
    base.update(overrides)
    return base


def _finding(domain: str = "pod") -> AgentFinding:
    return AgentFinding(
        domain=domain,
        signals=["pod CrashLoopBackOff"],
        hypothesis="OOM kill",
        confidence=0.85,
        evidence=["Exit code 137"],
    )


# ── Fan-out path ──────────────────────────────────────────────────────────────


class TestRCAFanOut:
    def test_rca_required_returns_list_of_sends(self):
        result = route_coordinator(_state(rca_required=True))
        assert isinstance(result, list)

    def test_rca_required_returns_four_sends(self):
        result = route_coordinator(_state(rca_required=True))
        assert len(result) == 4

    def test_all_items_are_send_objects(self):
        result = route_coordinator(_state(rca_required=True))
        assert all(isinstance(s, Send) for s in result)

    def test_all_four_domains_covered(self):
        result = route_coordinator(_state(rca_required=True))
        domains = {s.arg["domain"] for s in result}
        assert domains == {"pod", "metrics", "logs", "events"}

    def test_send_target_is_subagent_executor(self):
        result = route_coordinator(_state(rca_required=True))
        assert all(s.node == "subagent_executor" for s in result)

    def test_send_carries_session_id(self):
        result = route_coordinator(_state(rca_required=True, session_id="abc-xyz"))
        assert all(s.arg["session_id"] == "abc-xyz" for s in result)

    def test_send_carries_user_role(self):
        result = route_coordinator(_state(rca_required=True, user_role="readonly"))
        assert all(s.arg["user_role"] == "readonly" for s in result)


# ── Synthesis path ─────────────────────────────────────────────────────────────


class TestSynthesisPath:
    def test_rca_result_set_routes_to_end(self):
        rca = RCAResult(
            root_cause="OOMKilled by memory limit",
            confidence=0.9,
            supporting_evidence=["Exit code 137"],
            reasoning="All four subagents agree",
            recommended_fix="kubectl set resources deployment app --limits=memory=512Mi",
        )
        result = route_coordinator(_state(rca_result=rca))
        assert result is END

    def test_findings_present_routes_back_to_coordinator(self):
        findings = [_finding("pod"), _finding("metrics")]
        result = route_coordinator(_state(findings=findings))
        assert result == "coordinator"

    def test_four_findings_routes_to_coordinator(self):
        findings = [_finding(d) for d in ("pod", "metrics", "logs", "events")]
        result = route_coordinator(_state(findings=findings))
        assert result == "coordinator"


# ── Direct answer path ─────────────────────────────────────────────────────────


class TestDirectAnswerPath:
    def test_no_flags_routes_to_end(self):
        result = route_coordinator(_state())
        assert result is END

    def test_rca_required_false_routes_to_end(self):
        result = route_coordinator(_state(rca_required=False))
        assert result is END

    def test_empty_findings_routes_to_end(self):
        result = route_coordinator(_state(findings=[]))
        assert result is END


# ── Findings reducer ───────────────────────────────────────────────────────────


class TestFindingsReducer:
    def test_none_resets_to_empty(self):
        from app.agent.state import _findings_reducer
        existing = [_finding("pod"), _finding("metrics")]
        assert _findings_reducer(existing, None) == []

    def test_appends_new_findings(self):
        from app.agent.state import _findings_reducer
        existing = [_finding("pod")]
        new = [_finding("metrics")]
        result = _findings_reducer(existing, new)
        assert len(result) == 2
        assert result[0].domain == "pod"
        assert result[1].domain == "metrics"

    def test_empty_list_appended_cleanly(self):
        from app.agent.state import _findings_reducer
        existing = [_finding("pod")]
        result = _findings_reducer(existing, [])
        assert result == existing

    def test_accumulates_all_four_domains(self):
        from app.agent.state import _findings_reducer
        acc: list = []
        for domain in ("pod", "metrics", "logs", "events"):
            acc = _findings_reducer(acc, [_finding(domain)])
        assert len(acc) == 4
        domains = {f.domain for f in acc}
        assert domains == {"pod", "metrics", "logs", "events"}

    def test_none_after_accumulation_resets(self):
        from app.agent.state import _findings_reducer
        acc = [_finding(d) for d in ("pod", "metrics", "logs", "events")]
        assert _findings_reducer(acc, None) == []


# ── F2: orphan tool_call filler ────────────────────────────────────────────────


class TestOrphanToolCallFiller:
    """When HITL interrupts a parallel batch, the LLM re-proposes orphans on
    the next loop. _fill_orphan_tool_calls injects placeholders to break the loop."""

    def _ai(self, *tc_ids):
        from langchain_core.messages import AIMessage
        return AIMessage(
            content="",
            tool_calls=[
                {"id": tc_id, "name": "run_kubectl", "args": {"command": f"kubectl x{tc_id}"}}
                for tc_id in tc_ids
            ],
        )

    def _tm(self, tc_id, content="ok"):
        from langchain_core.messages import ToolMessage
        return ToolMessage(content=content, tool_call_id=tc_id)

    def test_no_orphans_when_all_executed(self):
        from app.agent.nodes.coordinator import _fill_orphan_tool_calls
        msgs = [self._ai("a", "b"), self._tm("a"), self._tm("b")]
        out = _fill_orphan_tool_calls(msgs)
        assert len(out) == 3   # nothing added

    def test_fills_orphan_after_partial_batch(self):
        from app.agent.nodes.coordinator import _fill_orphan_tool_calls
        from langchain_core.messages import ToolMessage
        msgs = [self._ai("a", "b", "c"), self._tm("a")]
        out = _fill_orphan_tool_calls(msgs)
        # Original 2 + 2 placeholders for b and c
        tool_msgs = [m for m in out if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 3
        ids = {m.tool_call_id for m in tool_msgs}
        assert ids == {"a", "b", "c"}

    def test_placeholder_marks_skipped(self):
        from app.agent.nodes.coordinator import _fill_orphan_tool_calls
        from langchain_core.messages import ToolMessage
        msgs = [self._ai("a", "b"), self._tm("a")]
        out = _fill_orphan_tool_calls(msgs)
        b_msg = next(m for m in out if isinstance(m, ToolMessage) and m.tool_call_id == "b")
        assert "Skipped" in b_msg.content

    def test_does_not_duplicate_existing(self):
        from app.agent.nodes.coordinator import _fill_orphan_tool_calls
        from langchain_core.messages import ToolMessage
        msgs = [self._ai("a", "b"), self._tm("a"), self._tm("b")]
        out = _fill_orphan_tool_calls(msgs)
        b_msgs = [m for m in out if isinstance(m, ToolMessage) and m.tool_call_id == "b"]
        assert len(b_msgs) == 1   # no duplicate, real result preserved
        assert b_msgs[0].content == "ok"

    def test_no_ai_message_no_op(self):
        from app.agent.nodes.coordinator import _fill_orphan_tool_calls
        from langchain_core.messages import HumanMessage
        msgs = [HumanMessage(content="hi")]
        assert _fill_orphan_tool_calls(msgs) == msgs
