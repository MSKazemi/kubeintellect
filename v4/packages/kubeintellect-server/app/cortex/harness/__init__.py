"""Harness gather-loop scaffolding (v5 ADR-101, specs/00).

ADR-101 keeps the V4 LangGraph shell and embeds a harness-style bounded tool loop *inside* the
gather node for read-only investigation subagent fan-out. This package holds the frozen
subagent contract (isolated context, ACI read-verb allowlist, ≤2k-token summary, no mutation)
that all downstream specs build against. The runner *mechanism* (LangGraph subgraph vs
in-process) is deferred to P2 — only the contract is fixed here, and it is pure/testable.
"""

from app.cortex.harness.runner import (
    plan_subagents,
    reconcile_results,
    run_fanout,
    run_subagent,
)
from app.cortex.harness.subagent import (
    SubagentContract,
    SubagentResult,
    bound_summary,
    enforce_read_only_allowlist,
)

__all__ = [
    "SubagentContract",
    "SubagentResult",
    "bound_summary",
    "enforce_read_only_allowlist",
    "plan_subagents",
    "reconcile_results",
    "run_fanout",
    "run_subagent",
]
