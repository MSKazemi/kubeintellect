"""LangGraph state types for KubeIntellect V2."""
from __future__ import annotations

from typing import Annotated, Any

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


def _findings_reducer(left: list, right: list | None) -> list:
    """Accumulate subagent findings from parallel fan-out.

    ``None`` is the reset sentinel: emitted by memory_loader at the start of
    every turn so stale findings from a prior RCA never bleed into the next.
    A normal list (including ``[]``) is appended so all 4 parallel subagents
    contribute their finding correctly.
    """
    if right is None:
        return []
    return (left or []) + right


# ── Subagent structured output ────────────────────────────────────────────────


class AgentFinding(BaseModel):
    """Structured finding from one RCA specialist subagent."""
    domain: str = Field(description="Specialist domain: pod | metrics | logs | events")
    signals: list[str] = Field(description="Key signals observed (kubectl/prometheus/loki output highlights)")
    hypothesis: str = Field(description="Root-cause hypothesis for this domain")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score 0-1")
    evidence: list[str] = Field(description="Verbatim excerpts or metric values supporting the hypothesis")
    tool_calls_made: list[str] = Field(default_factory=list, description="Tools invoked by this subagent")


# ── Synthesizer structured output ─────────────────────────────────────────────


class RCAResult(BaseModel):
    """Final synthesized RCA from the coordinator."""
    root_cause: str = Field(description="Single-sentence root cause statement")
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: list[str]
    conflicting_evidence: list[str] = Field(default_factory=list)
    reasoning: str = Field(description="Chain-of-thought reasoning over the subagent findings")
    recommended_fix: str = Field(description="Concrete kubectl/config fix recommendation")
    affected_domain: list[str] = Field(default_factory=list, description="Which domains contributed")


# ── Main graph state ───────────────────────────────────────────────────────────


class AgentState(TypedDict):
    """State carried through the KubeIntellect coordinator graph."""
    # Message history — LangGraph reducer appends new messages automatically
    messages: Annotated[list[BaseMessage], add_messages]

    # Loaded from DB before coordinator runs (pinned system context)
    memory_context: str

    # Subagent findings accumulated via parallel fan-out.
    # Uses _findings_reducer so all 4 subagents contribute (not last-write-wins)
    # and memory_loader can reset with None between turns.
    findings: Annotated[list[AgentFinding], _findings_reducer]

    # Set by coordinator when the LLM requests a parallel RCA fan-out.
    # Consumed by route_coordinator to dispatch the 4 specialist subagents.
    # Reset to False by the per-turn initial state (no reducer → plain overwrite).
    rca_required: bool

    # Final RCA (set by synthesizer node, None until synthesis is done)
    rca_result: RCAResult | None

    # HITL state — set when run_kubectl raises HITLRequired
    pending_hitl: dict[str, Any] | None   # {action_id, command, risk_level, human_summary}

    # Conversation / session metadata
    session_id: str
    user_id: str
    user_role: str   # "admin" | "readonly" — injected by API auth layer


# ── Subagent-scoped state (used in Send payload) ───────────────────────────────


class SubagentInput(TypedDict):
    """Payload sent to each specialist subagent via Send API."""
    domain: str           # pod | metrics | logs | events
    session_id: str
    user_id: str
    user_role: str   # "admin" | "readonly"
    messages: list[BaseMessage]
    memory_context: str
