"""Shared data models for the KubeIntellect evaluation harness."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamEvent:
    type: str  # start|status|tool_call|tool_result|token|hitl_request|plan|error|done
    data: dict[str, Any]


@dataclass
class EvalResult:
    session_id: str
    query_id: str
    messages: list[dict]
    final_text: str
    events: list[StreamEvent]
    had_hitl: bool
    had_error: bool
    error_message: str | None
    latency_ms: float
    start_ts: float
    end_ts: float
    tool_calls: list[str]       # tool names in call order
    tool_errors: list[str]      # tools that returned errors
    status_phases: list[str]    # coordinator phases seen
    hitl_commands: list[str]    # commands that triggered HITL


@dataclass
class LangfuseGeneration:
    name: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


@dataclass
class LangfuseToolSpan:
    name: str
    input_args: dict
    output: str | None
    error: str | None
    latency_ms: float


@dataclass
class LangfuseTrace:
    trace_id: str
    session_id: str
    total_tokens: int
    total_latency_ms: float
    generations: list[LangfuseGeneration]
    tool_spans: list[LangfuseToolSpan]
    has_error: bool
    error_messages: list[str]
    available: bool  # False when Langfuse is not configured or trace not found yet


@dataclass
class LokiLogLine:
    timestamp_ns: int
    level: str   # INFO | WARNING | ERROR
    message: str


@dataclass
class LokiLogs:
    session_id: str
    lines: list[LokiLogLine]
    error_count: int
    warning_count: int
    kubectl_commands: list[str]
    hitl_events: list[str]
    available: bool


@dataclass
class PrometheusMetrics:
    p95_latency_ms: float | None
    error_rate: float | None
    request_count: int | None
    available: bool


@dataclass
class SignalScores:
    completion: float              # 0-1: reached final answer without error
    tool_correctness: float        # 0-1: called expected tools (1.0 if no expectation set)
    error_free: float              # 0-1: no errors in stream, traces, or logs
    hitl_discipline: float         # 0-1: no unexpected HITL on read-only queries
    latency: float                 # 0-1: normalised against per-category baseline
    cluster_resolved: float | None = None  # 1.0 issue fixed in cluster, 0.0 not fixed, None no verify.sh
    notes: list[str] = None        # human-readable explanations for low scores

    def __post_init__(self):
        if self.notes is None:
            self.notes = []


@dataclass
class TraceIssue:
    id: str
    severity: str   # HIGH | MEDIUM | LOW
    detail: str


@dataclass
class TurnResult:
    """One round-trip on the same session_id. Turn 0 is the original query."""
    turn_index: int
    sent_content: str
    final_text: str
    tool_calls: list[str]
    had_hitl: bool
    had_error: bool
    latency_ms: float


@dataclass
class EvalRecord:
    query_id: str
    title: str
    session_id: str
    run_id: str
    result: EvalResult
    trace: LangfuseTrace | None
    logs: LokiLogs | None
    scores: SignalScores
    aggregate_score: float
    review_verdict: str | None   # filled in manually after reviewing the answer
    timestamp: str
    trace_issues: list[TraceIssue] = field(default_factory=list)
    cluster_resolved: bool | None = None  # True=issue fixed in cluster, False=not fixed, None=no verify.sh
    agent_asked_first: bool = False       # agent gated, runner sent follow-up, agent then acted
    follow_up_sent: str | None = None     # text the harness sent on turn 2 (or None)
    num_turns: int = 1                    # 1 = single-turn, 2 = followed up
    turns: list[TurnResult] = field(default_factory=list)
    version: str = ""        # target version: v1 | v2 | v3
    category: str = ""       # scenario category from metadata.yaml
