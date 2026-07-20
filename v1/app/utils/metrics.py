# app/utils/metrics.py
"""
KubeIntellect Prometheus metrics.

Custom counters and histograms for the KubeIntellect API.
These are scraped by Prometheus via the /metrics endpoint exposed by
prometheus-fastapi-instrumentator (wired in app/main.py).

Usage:
    from app.utils.metrics import (
        agent_invocations_total,
        tool_calls_total,
        workflow_duration_seconds,
        hitl_decisions_total,
    )

    agent_invocations_total.labels(agent="Logs").inc()
    hitl_decisions_total.labels(decision="approved").inc()

    with workflow_duration_seconds.time():
        ...
"""

from prometheus_client import Counter, Histogram

# Total agent invocations, labelled by agent name.
# Incremented once per worker node invocation in routing.py.
agent_invocations_total = Counter(
    "kubeintellect_agent_invocations_total",
    "Total number of agent node invocations",
    ["agent"],
)

# Total tool calls, labelled by tool name and outcome (success | error).
# "error" is counted when the tool returns a string starting with "Error:".
tool_calls_total = Counter(
    "kubeintellect_tool_calls_total",
    "Total number of tool invocations",
    ["tool", "status"],
)

# End-to-end workflow duration (seconds) per user query.
# Recorded from the moment execute_workflow_stream() starts until
# workflow_complete is yielded.
workflow_duration_seconds = Histogram(
    "kubeintellect_workflow_duration_seconds",
    "End-to-end workflow execution time in seconds",
    buckets=[1, 2, 5, 10, 30, 60, 120],
)

# HITL (Human-in-the-Loop) approval/denial decisions.
# Incremented in chat_completions.py when the user responds to a HITL prompt.
hitl_decisions_total = Counter(
    "kubeintellect_hitl_decisions_total",
    "Total number of HITL decisions",
    ["decision"],  # "approved" | "denied"
)

# Tool output truncations by the ToolOutputSummarizer.
# Incremented once per truncated output in tool_output_summarizer.py.
tool_output_truncated_total = Counter(
    "kubeintellect_tool_output_truncated_total",
    "Total number of tool outputs truncated by the summarizer",
    ["tool_type"],  # "log" | "event" | "structured_read" | "generic" | "list"
)

# List resource truncations — item-level, tracked separately from token-level truncation.
# Labelled by resource_type (pod, node, deployment, service, configmap, namespace, item).
list_output_truncated_total = Counter(
    "kubeintellect_list_output_truncated_total",
    "Total number of list tool outputs truncated at the item level by the summarizer",
    ["resource_type"],
)

# LLM streaming errors by normalised error type.
# Incremented in workflow.py when astream() raises mid-response.
llm_stream_errors_total = Counter(
    "kubeintellect_llm_stream_errors_total",
    "Total number of LLM streaming errors by type",
    ["error_type"],  # rate_limit | context_length | auth | network | unknown
)

# Total SSE stream completions, labelled by outcome (success | error).
# Incremented in chat_completions.py at the end of each stream_generator invocation.
stream_completions_total = Counter(
    "kubeintellect_stream_completions_total",
    "Total number of SSE stream completions",
    ["status"],  # "success" | "error"
)

# Conversation summary cache hits and misses.
# Hit: cached summary found and used — no live LLM summarization call needed.
# Miss: no cache entry found (live call required) or background write failed.
summary_cache_hit_total = Counter(
    "summary_cache_hit_total", "Conversation summary cache hits"
)
summary_cache_miss_total = Counter(
    "summary_cache_miss_total", "Conversation summary cache misses"
)

# Tool calls suppressed by the intra-agent duplicate tool-call guard (B3 fix).
# Incremented in routing.py when an agent calls the same tool with the same args
# and same output 3+ consecutive times within a single turn.
tool_call_suppressed_total = Counter(
    "kubeintellect_tool_call_suppressed_total",
    "Tool calls suppressed by the intra-agent duplicate-call guard",
    ["agent", "tool_name"],
)
