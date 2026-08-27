"""KubeIntellect's own domain metrics.

`/metrics` already served twenty families before this module existed, and every one of them came
from the FastAPI instrumentator: request counts, request latency, process RSS, GC statistics. All
true, all generic, and none of them about what this server actually does. An operator could see
that a request took 40 seconds and could not see whether it spent that time in one LLM call or in
eleven `kubectl` invocations, whether a tool failed, or whether an approval gate stopped it.

A campaign made the cost of that concrete: the H3-H8 lanes ran with no application metrics at all,
and the only reason per-request tokens exist for them is that the SSE stream carries a usage frame
(see `app/core/usage.py`). Everything else about those runs had to be read out of logs.

Four families, chosen because each answers a question the generic ones structurally cannot:

  * `kubeintellect_tool_calls_total{tool, outcome}` — what the agent did, and whether it worked.
  * `kubeintellect_tool_duration_seconds{tool}` — where a slow investigation actually went.
  * `kubeintellect_hitl_interrupts_total{tool}` — how often the approval gate fires, which is the
    safety property the product is sold on and was previously observable only in the audit log.
  * `kubeintellect_llm_calls_total` / `kubeintellect_llm_tokens_total{direction}` — the cost axis.

LABELS ARE BOUNDED ON PURPOSE
-----------------------------
`tool` comes from a fixed registry, so its cardinality is the size of that registry. Nothing here
is labelled by session, namespace, cluster, pod or user: those are unbounded, and an unbounded
label on a counter is how a metrics endpoint turns into an outage. If you add a label, it must
come from a closed set.

NOTHING HERE MAY BREAK A REQUEST
--------------------------------
Every entry point swallows its own exceptions. A metrics backend that is missing, misconfigured,
or a version that renamed something must degrade to "no metrics", never to a failed
investigation — instrumentation that can take down the thing it measures is worse than none.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Typed `Any` rather than `Counter | None`: these are either a prometheus collector or None
# depending on whether the optional backend imported, and every use is already guarded by
# `_ENABLED` — a guard mypy cannot follow across function boundaries. Spelling them `| None`
# would buy a union-attr error at each of the ten call sites and no additional safety.
_ENABLED = False
_TOOL_CALLS: Any = None
_TOOL_DURATION: Any = None
_HITL: Any = None
_LLM_CALLS: Any = None
_LLM_TOKENS: Any = None

try:
    from prometheus_client import Counter, Histogram

    _TOOL_CALLS = Counter(
        "kubeintellect_tool_calls_total",
        "Tool invocations by the agent, by tool and outcome.",
        ("tool", "outcome"),
    )
    _TOOL_DURATION = Histogram(
        "kubeintellect_tool_duration_seconds",
        "Wall-clock duration of a single tool invocation.",
        ("tool",),
        # A kubectl call lands near 0.1 s and an LLM-backed subagent near 60 s; the default
        # buckets top out at 10 s and would put both in +Inf, which is where latency goes to die.
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
    )
    _HITL = Counter(
        "kubeintellect_hitl_interrupts_total",
        "Human-in-the-loop approval gates raised, by the tool that raised one.",
        ("tool",),
    )
    _LLM_CALLS = Counter(
        "kubeintellect_llm_calls_total",
        "Completed model calls, summed across every node of a request.",
    )
    _LLM_TOKENS = Counter(
        "kubeintellect_llm_tokens_total",
        "Tokens consumed, by direction.",
        ("direction",),
    )
    _ENABLED = True
except Exception as exc:  # pragma: no cover - depends on the deployed dependency set
    logger.debug("domain metrics unavailable, continuing without them: %s", exc)


def enabled() -> bool:
    return _ENABLED


def record_tool_call(tool: str, outcome: str, duration_s: float | None = None) -> None:
    """One tool invocation. `outcome` must come from a closed set: ok, error, unknown_tool."""
    if not _ENABLED:
        return
    try:
        _TOOL_CALLS.labels(tool=tool or "<unnamed>", outcome=outcome).inc()
        if duration_s is not None:
            _TOOL_DURATION.labels(tool=tool or "<unnamed>").observe(duration_s)
    except Exception:  # pragma: no cover - never break a request to record it
        logger.debug("failed to record tool metric", exc_info=True)


def record_hitl_interrupt(tool: str) -> None:
    """An approval gate fired. Counted separately from `error`: a gate is the product working."""
    if not _ENABLED:
        return
    try:
        _HITL.labels(tool=tool or "<unnamed>").inc()
    except Exception:  # pragma: no cover
        logger.debug("failed to record hitl metric", exc_info=True)


def record_llm_usage(*, input_tokens: int = 0, output_tokens: int = 0, calls: int = 0) -> None:
    if not _ENABLED:
        return
    try:
        if calls:
            _LLM_CALLS.inc(calls)
        if input_tokens:
            _LLM_TOKENS.labels(direction="input").inc(input_tokens)
        if output_tokens:
            _LLM_TOKENS.labels(direction="output").inc(output_tokens)
    except Exception:  # pragma: no cover
        logger.debug("failed to record llm metric", exc_info=True)
