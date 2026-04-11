# app/orchestration/state.py
"""
KubeIntellect state definitions.

Constants, data classes, and typed state structures shared across the
orchestration layer.
"""

import operator
from typing import Any, Dict, Literal, Sequence, Annotated, List, Union, Optional

from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from langchain_core.agents import AgentAction
from langchain_core.messages import ToolMessage
from pydantic import BaseModel
from typing import TypedDict

from app.orchestration.schemas import TaskPlan  # noqa: F401 — used in SupervisorRoute

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RECURSION_LIMIT = 50
MAX_CLARIFICATIONS = 3

# Agent member names for the supervisor
AGENT_MEMBERS = [
    "Logs",
    "ConfigMapsSecrets",
    "RBAC",
    "Metrics",
    "Security",
    "Lifecycle",
    "Execution",
    "Deletion",
    "Infrastructure",
    "DynamicToolsExecutor",
    "CodeGenerator",
    "Apply",
    "DiagnosticsOrchestrator",
]

# Supervisor routing options
SUPERVISOR_OPTIONS = ["FINISH"] + AGENT_MEMBERS


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------

class AgentDefinition:
    """Configuration for a worker agent."""

    def __init__(self, name: str, tools: List[BaseTool], prompt: str):
        self.name = name
        self.tools = tools
        self.prompt = prompt


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------

class KubeIntellectState(TypedDict):
    """State structure for the KubeIntellect workflow graph."""
    messages: Annotated[Sequence[BaseMessage], operator.add]
    next: str  # Worker name or FINISH
    intermediate_steps: Annotated[List[Union[AgentAction, ToolMessage]], operator.add]
    reflection_memory: Optional[List[str]]  # Verbal reflections on HITL rejections / tool failures
    # Structured inter-agent output contracts (see app/orchestration/schemas.py).
    # Keyed by agent node name (e.g. "Logs", "Metrics").  Values are AgentResult
    # instances serialised to dicts so LangGraph can checkpoint them.
    agent_results: Optional[Dict[str, Any]]
    # Set by worker nodes when a tool call returns a deterministic 4xx HTTP error.
    # The supervisor checks this before calling the LLM and routes to FINISH instead
    # of re-dispatching the same agent with the same intent.
    # Reset to None at the start of every workflow invocation.
    last_tool_error: Optional[Dict[str, Any]]  # {"http_status": int, "agent": str, "message": str}
    # Counts how many times the supervisor has routed to a worker in the current turn.
    # Used by supervisor_router_node_func to detect runaway re-routing and enforce
    # the success-completion guard.  MUST be in the TypedDict so LangGraph persists
    # it across node transitions — if omitted, state.get() always returns the default
    # (0) and cycle-based guards never fire.
    supervisor_cycles: int
    # Set to True by worker nodes when their response fully answers the user's request.
    # The supervisor checks this to FINISH immediately without string-matching on output.
    # Reset to None at the start of every workflow invocation.
    task_complete: Optional[bool]
    # Set to True when the supervisor routes to DynamicToolsExecutor after a tool_just_created
    # event.  The supervisor checks this on the next cycle to FINISH immediately.
    dynamic_executor_ran_after_creation: Optional[bool]
    # Running log of actions taken this session. Each worker appends one line on
    # completion: "AgentName: one sentence of what was done and what was found."
    # The supervisor reads this to avoid re-running steps that already have results.
    # Capped at 20 entries to prevent unbounded growth across long conversations.
    steps_taken: Optional[List[str]]
    # Multi-step execution plan. Populated by the supervisor on the first invocation
    # of a multi-step query. Each element is a valid AGENT_MEMBERS name. Once set,
    # supervisor_router_node_func executes steps deterministically without re-calling
    # the LLM for each step.
    plan: Optional[List[str]]
    # Index of the next unexecuted plan step (0-based). Incremented after each step.
    # When plan_step == len(plan), the plan is exhausted and normal LLM routing resumes.
    # Set to len(plan) to abort the plan early (e.g., on tool error or clarification).
    plan_step: int
    # Tracks dispatch fingerprints issued in the current user turn to detect and block
    # re-dispatch loops. Each entry is "agent:hash" where hash is derived from the
    # last HumanMessage content.  Because the hash is message-scoped, the set is
    # implicitly "cleared" whenever a new user turn (new human message) arrives.
    seen_dispatches: Optional[List[str]]
    # DiagnosticsOrchestrator: set by the orchestrator node, read by the three parallel
    # signal sub-nodes (DiagnosticsLogs, DiagnosticsMetrics, DiagnosticsEvents).
    # Stored as a JSON string: {"namespace": str, "pod_name": str|null, "original_query": str}
    diagnostics_query: Optional[str]
    # Written by the three DiagnosticsOrchestrator signal sub-nodes independently.
    # No reducer — each node writes its own key so parallel updates never conflict.
    diagnostics_logs_result: Optional[Dict]
    diagnostics_metrics_result: Optional[Dict]
    diagnostics_events_result: Optional[Dict]
    # Full TaskPlan (serialized to dict) committed by the supervisor on multi-step queries.
    # Contains SequentialStep list with input_spec for each step; drives deterministic execution.
    task_plan: Optional[Dict[str, Any]]
    # PlanExecutionState (serialized to dict) — tracks current_step, completed_steps, failure_step.
    plan_execution_state: Optional[Dict[str, Any]]
    # Number of distinct tool calls made by the most recently completed worker agent.
    # Set by the worker node from the agent's ToolMessage count; used by the plan fast-path
    # to distinguish "agent did work + offered more (non-blocking ?)" from "agent asked for
    # mandatory input without calling any tools (blocking ?)".
    tool_calls_made: Optional[int]


# ---------------------------------------------------------------------------
# Routing model
# ---------------------------------------------------------------------------

class SupervisorRoute(BaseModel):
    """Pydantic model for supervisor routing decisions."""
    next: Literal[tuple(SUPERVISOR_OPTIONS)]
    plan: Optional[List[str]] = None        # Legacy: ordered agent names (kept for backward compat)
    task_plan: Optional[TaskPlan] = None    # Structured plan with input_spec per step (preferred)
    needs_human_input: bool = False         # True when the last worker message is a question awaiting user reply
