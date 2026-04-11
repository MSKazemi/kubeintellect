"""
Tests for multi-step planning (Stage 1 — structured result contracts).

Covers:
  (a) 3-step sequential chain happy path through supervisor_router_node_func
  (b) Plan abort when a step produces an error mid-plan
  (c) input_spec resolution from agent_results
  (d) TaskPlan / SequentialStep / PlanExecutionState model validation
  (e) plan_preview formatting helper
"""

import pytest
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.orchestration.schemas import (
    PlanExecutionState,
    SequentialStep,
    TaskPlan,
)
from app.orchestration.state import SupervisorRoute


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------

class TestSequentialStep:
    def test_empty_input_spec(self):
        step = SequentialStep(agent="Logs")
        assert step.agent == "Logs"
        assert step.input_spec == {}

    def test_with_input_spec(self):
        step = SequentialStep(agent="Metrics", input_spec={"pod_name": "Logs.pod_name"})
        assert step.input_spec == {"pod_name": "Logs.pod_name"}

    def test_serialization_roundtrip(self):
        step = SequentialStep(agent="Lifecycle", input_spec={"ns": "Logs.namespace"})
        data = step.model_dump()
        restored = SequentialStep.model_validate(data)
        assert restored.agent == step.agent
        assert restored.input_spec == step.input_spec


class TestTaskPlan:
    def test_valid_plan(self):
        plan = TaskPlan(steps=[
            SequentialStep(agent="Logs"),
            SequentialStep(agent="Metrics"),
            SequentialStep(agent="Lifecycle"),
        ], query_summary="Test plan")
        assert len(plan.steps) == 3
        assert plan.query_summary == "Test plan"

    def test_exceeds_max_steps(self):
        with pytest.raises(ValueError, match="max_plan_steps"):
            TaskPlan(steps=[SequentialStep(agent="Logs")] * 11)

    def test_exactly_max_steps(self):
        plan = TaskPlan(steps=[SequentialStep(agent="Logs")] * 10)
        assert len(plan.steps) == 10

    def test_model_dump_roundtrip(self):
        plan = TaskPlan(steps=[
            SequentialStep(agent="Logs"),
            SequentialStep(agent="Metrics", input_spec={"pod": "Logs.pod_name"}),
        ])
        restored = TaskPlan.model_validate(plan.model_dump())
        assert len(restored.steps) == 2
        assert restored.steps[1].input_spec == {"pod": "Logs.pod_name"}


class TestPlanExecutionState:
    def test_defaults(self):
        state = PlanExecutionState()
        assert state.current_step == 0
        assert state.completed_steps == []
        assert state.failure_step is None

    def test_with_progress(self):
        state = PlanExecutionState(
            current_step=2,
            completed_steps=["Logs", "Metrics"],
            failure_step=None,
        )
        assert state.current_step == 2
        assert state.completed_steps == ["Logs", "Metrics"]

    def test_failure_tracking(self):
        state = PlanExecutionState(current_step=1, failure_step=1)
        assert state.failure_step == 1


class TestSupervisorRoute:
    def test_task_plan_field_accepted(self):
        plan = TaskPlan(steps=[
            SequentialStep(agent="Logs"),
            SequentialStep(agent="Metrics"),
        ])
        route = SupervisorRoute(next="Logs", task_plan=plan)
        assert route.task_plan is not None
        assert len(route.task_plan.steps) == 2

    def test_legacy_plan_field_still_works(self):
        route = SupervisorRoute(next="Logs", plan=["Logs", "Metrics"])
        assert route.plan == ["Logs", "Metrics"]
        assert route.task_plan is None

    def test_both_none_by_default(self):
        route = SupervisorRoute(next="FINISH")
        assert route.plan is None
        assert route.task_plan is None


# ---------------------------------------------------------------------------
# supervisor_router_node_func — plan execution
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> dict:
    """Build a minimal KubeIntellect state dict for testing."""
    defaults = {
        "messages": [HumanMessage(content="drain node worker-1, check health, list events")],
        "next": "",
        "intermediate_steps": [],
        "supervisor_cycles": 0,
        "agent_results": {},
        "last_tool_error": None,
        "task_complete": None,
        "reflection_memory": [],
        "plan": None,
        "plan_step": 0,
        "task_plan": None,
        "plan_execution_state": None,
        "steps_taken": [],
        "seen_dispatches": [],
        "dynamic_executor_ran_after_creation": None,
    }
    defaults.update(kwargs)
    return defaults


class TestPlanStepExecution:
    """Tests for deterministic plan-step routing in supervisor_router_node_func."""

    def _make_chain_returning(self, route: SupervisorRoute):
        chain = MagicMock()
        chain.invoke = MagicMock(return_value=route)
        return chain

    def test_happy_path_3_steps(self):
        """A committed 3-step plan routes through all agents deterministically."""
        from app.orchestration.routing import supervisor_router_node_func

        # Initial state: plan committed, about to execute step 1
        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Lifecycle"),
            SequentialStep(agent="Metrics"),
            SequentialStep(agent="Logs"),
        ], query_summary="3-step plan")
        state = _make_state(
            plan=["Lifecycle", "Metrics", "Logs"],
            plan_step=0,
            task_plan=task_plan.model_dump(),
            plan_execution_state=PlanExecutionState().model_dump(),
        )
        chain = self._make_chain_returning(SupervisorRoute(next="FINISH"))

        # Step 0 → routes to Lifecycle, advances plan_step to 1
        result = supervisor_router_node_func(state, chain)
        assert result["next"] == "Lifecycle"
        assert result["plan_step"] == 1
        assert result["plan_execution_state"]["current_step"] == 1

        # Step 1 → routes to Metrics
        state["plan_step"] = 1
        state["plan_execution_state"] = result["plan_execution_state"]
        state["messages"].append(AIMessage(content="Workload managed.", name="Lifecycle"))
        result = supervisor_router_node_func(state, chain)
        assert result["next"] == "Metrics"
        assert result["plan_step"] == 2
        assert "Lifecycle" in result["plan_execution_state"]["completed_steps"]

        # Step 2 → routes to Logs
        state["plan_step"] = 2
        state["plan_execution_state"] = result["plan_execution_state"]
        state["messages"].append(AIMessage(content="Metrics collected.", name="Metrics"))
        result = supervisor_router_node_func(state, chain)
        assert result["next"] == "Logs"
        assert result["plan_step"] == 3

    def test_plan_abort_on_tool_error_4xx(self):
        """4xx tool error exhausts the plan via the 4xx guard (fires before plan-step block)."""
        from app.orchestration.routing import supervisor_router_node_func

        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Lifecycle"),
            SequentialStep(agent="Metrics"),
        ]).model_dump()
        state = _make_state(
            plan=["Lifecycle", "Metrics"],
            plan_step=1,
            task_plan=task_plan,
            plan_execution_state=PlanExecutionState(current_step=1).model_dump(),
            last_tool_error={"http_status": 404, "agent": "Lifecycle", "message": "not found"},
        )
        chain = self._make_chain_returning(SupervisorRoute(next="FINISH"))

        result = supervisor_router_node_func(state, chain)
        assert result["next"] == "FINISH"
        # 4xx guard exhausts the plan so next invocation doesn't re-enter plan-step path
        assert result["plan_step"] == 2   # len(plan) = 2 → exhausted
        assert result["plan_execution_state"]["failure_step"] == 1

    def test_plan_abort_on_clarification(self):
        """Plan aborts when the last worker message contains a question."""
        from app.orchestration.routing import supervisor_router_node_func

        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Lifecycle"),
            SequentialStep(agent="Metrics"),
        ]).model_dump()
        state = _make_state(
            messages=[
                HumanMessage(content="drain node then check health"),
                AIMessage(content="Which node should I drain?", name="Lifecycle"),
            ],
            plan=["Lifecycle", "Metrics"],
            plan_step=1,
            task_plan=task_plan,
            plan_execution_state=PlanExecutionState(current_step=1).model_dump(),
        )
        chain = self._make_chain_returning(SupervisorRoute(next="FINISH"))

        result = supervisor_router_node_func(state, chain)
        assert result["next"] == "FINISH"
        assert result["plan_execution_state"]["failure_step"] == 1

    def test_plan_does_not_abort_when_worker_called_tools_and_offered_more(self):
        """Plan must NOT abort when worker called tools (did real work) and added a trailing offer."""
        from app.orchestration.routing import supervisor_router_node_func

        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Metrics"),
            SequentialStep(agent="Logs"),
        ]).model_dump()
        state = _make_state(
            messages=[
                HumanMessage(content="check cpu then list events"),
                # Metrics did real work but ended with a polite offer
                AIMessage(
                    content="Here are the top nodes by CPU: testbed-worker: 1.1%. "
                            "Would you like me to investigate further?",
                    name="Metrics",
                ),
            ],
            plan=["Metrics", "Logs"],
            plan_step=1,
            task_plan=task_plan,
            plan_execution_state=PlanExecutionState(current_step=1).model_dump(),
        )
        state["tool_calls_made"] = 3  # Metrics called 3 tools → did real work
        chain = self._make_chain_returning(SupervisorRoute(next="FINISH"))

        result = supervisor_router_node_func(state, chain)
        # Must NOT abort — must continue to Logs (step 1)
        assert result["next"] == "Logs"
        assert result["plan_step"] == 2

    def test_task_complete_does_not_abort_plan_with_remaining_steps(self):
        """task_complete=True from a plan step must NOT force FINISH if more steps remain."""
        from app.orchestration.routing import supervisor_router_node_func

        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Infrastructure"),
            SequentialStep(agent="Lifecycle"),
            SequentialStep(agent="Lifecycle"),
        ]).model_dump()
        state = _make_state(
            messages=[
                HumanMessage(content="create namespace and deployment"),
                # Infrastructure completed its step and set task_complete=True
                AIMessage(content="The namespace has been created successfully.", name="Infrastructure"),
            ],
            plan=["Infrastructure", "Lifecycle", "Lifecycle"],
            plan_step=1,   # Step 1 (Lifecycle) is next — plan has more steps
            task_plan=task_plan,
            plan_execution_state=PlanExecutionState(current_step=1).model_dump(),
        )
        state["task_complete"] = True           # Simulates Infrastructure completing step
        state["supervisor_cycles"] = 1          # Simulates 1 cycle already done
        chain = self._make_chain_returning(SupervisorRoute(next="FINISH"))

        result = supervisor_router_node_func(state, chain)
        # Must NOT FINISH — must continue with Lifecycle (plan step 1)
        assert result["next"] == "Lifecycle"
        assert result["plan_step"] == 2

    def test_commit_task_plan_from_llm(self):
        """Supervisor commits a task_plan returned by the LLM chain."""
        from app.orchestration.routing import supervisor_router_node_func

        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Metrics"),
            SequentialStep(agent="Logs"),
            SequentialStep(agent="Security"),
        ], query_summary="Health check")
        route = SupervisorRoute(next="Metrics", task_plan=task_plan)
        chain = self._make_chain_returning(route)

        state = _make_state()  # no active plan
        result = supervisor_router_node_func(state, chain)

        assert result["next"] == "Metrics"
        assert result["plan"] == ["Metrics", "Logs", "Security"]
        assert result["plan_step"] == 1
        assert result["task_plan"] is not None
        assert result["plan_execution_state"]["current_step"] == 1

    def test_commit_task_plan_uses_plan_first_step_not_llm_next(self):
        """When LLM's next disagrees with task_plan.steps[0], plan's first step wins."""
        from app.orchestration.routing import supervisor_router_node_func

        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Infrastructure"),
            SequentialStep(agent="Lifecycle"),
            SequentialStep(agent="Infrastructure"),
            SequentialStep(agent="Lifecycle"),
        ], query_summary="Create namespace, deployment, service, check rollout")
        # LLM incorrectly set next="Lifecycle" but plan starts with Infrastructure
        route = SupervisorRoute(next="Lifecycle", task_plan=task_plan)
        chain = self._make_chain_returning(route)

        state = _make_state()
        result = supervisor_router_node_func(state, chain)

        # Must route to Infrastructure (plan step 0), not Lifecycle (LLM's next)
        assert result["next"] == "Infrastructure"
        assert result["plan"] == ["Infrastructure", "Lifecycle", "Infrastructure", "Lifecycle"]
        assert result["plan_step"] == 1

    def test_invalid_task_plan_falls_back_to_single_step(self):
        """task_plan with < 2 valid steps falls back to normal single-step routing."""
        from app.orchestration.routing import supervisor_router_node_func

        # task_plan with 1 valid step (< 2) — should not commit the plan
        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Logs"),
        ])
        route = SupervisorRoute(next="Logs", task_plan=task_plan)
        chain = self._make_chain_returning(route)

        state = _make_state()
        result = supervisor_router_node_func(state, chain)

        assert result["next"] == "Logs"
        assert result.get("plan") is None  # plan was NOT committed


class TestInputSpecResolution:
    """Tests for input_spec resolution from agent_results."""

    def test_input_spec_injects_context_message(self):
        """When input_spec references a prior result field, a SystemMessage is injected."""
        from app.orchestration.routing import supervisor_router_node_func

        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Logs"),
            SequentialStep(agent="Metrics", input_spec={"pod_name": "Logs.pod_name"}),
        ]).model_dump()

        state = _make_state(
            messages=[
                HumanMessage(content="fetch logs then get metrics for the same pod"),
                AIMessage(content="Found pod webapp-abc123 in namespace default.", name="Logs"),
            ],
            plan=["Logs", "Metrics"],
            plan_step=1,
            task_plan=task_plan,
            plan_execution_state=PlanExecutionState(current_step=1).model_dump(),
            agent_results={
                "Logs": {
                    "agent_name": "Logs",
                    "success": True,
                    "raw_output": "Found logs.",
                    "pod_name": "webapp-abc123",
                    "namespace": "default",
                }
            },
        )
        chain = MagicMock()
        chain.invoke = MagicMock(return_value=SupervisorRoute(next="FINISH"))

        result = supervisor_router_node_func(state, chain)

        assert result["next"] == "Metrics"
        injected = result.get("messages", [])
        assert any(
            isinstance(m, SystemMessage) and "webapp-abc123" in m.content
            for m in injected
        ), "Expected SystemMessage with resolved pod_name=webapp-abc123"

    def test_missing_input_spec_field_skipped_silently(self):
        """Unresolvable input_spec references are silently skipped (no crash)."""
        from app.orchestration.routing import supervisor_router_node_func

        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Logs"),
            SequentialStep(agent="Metrics", input_spec={"pod_name": "Logs.nonexistent_field"}),
        ]).model_dump()

        state = _make_state(
            messages=[HumanMessage(content="check metrics")],
            plan=["Logs", "Metrics"],
            plan_step=1,
            task_plan=task_plan,
            plan_execution_state=PlanExecutionState(current_step=1).model_dump(),
            agent_results={"Logs": {"agent_name": "Logs", "success": True, "raw_output": "ok"}},
        )
        chain = MagicMock()
        chain.invoke = MagicMock(return_value=SupervisorRoute(next="FINISH"))

        # Should not raise
        result = supervisor_router_node_func(state, chain)
        assert result["next"] == "Metrics"
        # Step task header SystemMessage is always injected; no input_spec SystemMessage
        injected = [m for m in result.get("messages", []) if isinstance(m, SystemMessage)]
        # Only the step-header message injected (task description), not input_spec context
        assert all("PLAN STEP" in m.content or "Plan context" not in m.content for m in injected)


# ---------------------------------------------------------------------------
# plan_preview formatting
# ---------------------------------------------------------------------------

class TestPlanPreviewFormat:
    """format_plan_preview is in schemas.py (workflow.py is mocked in conftest)."""

    def test_task_plan_preview(self):
        from app.orchestration.schemas import format_plan_preview

        task_plan = TaskPlan(steps=[
            SequentialStep(agent="Lifecycle"),
            SequentialStep(agent="Metrics"),
            SequentialStep(agent="Logs"),
        ], query_summary="Drain, check metrics, list events").model_dump()

        preview = format_plan_preview(task_plan, None)
        assert "Drain, check metrics, list events" in preview
        assert "3-step" in preview
        assert "Manage workloads" in preview     # Lifecycle friendly name
        assert "Collect resource metrics" in preview

    def test_legacy_plan_preview(self):
        from app.orchestration.schemas import format_plan_preview

        preview = format_plan_preview(None, ["Security", "RBAC"])
        assert "2-step" in preview
        assert "Run security audit" in preview
        assert "Check access permissions" in preview

    def test_empty_plan_returns_empty_string(self):
        from app.orchestration.schemas import format_plan_preview

        assert format_plan_preview(None, None) == ""
        assert format_plan_preview(None, []) == ""
