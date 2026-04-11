# app/orchestration/schemas.py
"""
Structured inter-agent output contracts.

Each worker agent produces a typed Pydantic result that the LangGraph state
carries alongside the raw AIMessage.  Downstream agents and the supervisor
can read these structured fields instead of re-parsing unstructured strings.

Reference: MetaGPT (arXiv 2308.00352 §3.2) — structured message passing
between agents eliminates "Chinese whispers" information loss.
"""

import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Multi-step plan models (Stage 1 — structured result contracts)
# ---------------------------------------------------------------------------

class SequentialStep(BaseModel):
    """One step in a deterministic execution plan."""
    agent: str = Field(
        ...,
        description="Agent node name (must be a valid AGENT_MEMBERS entry).",
    )
    task: str = Field(
        default="",
        description=(
            "Precise description of ONLY what this step should do. "
            "Must be scoped to a single action (e.g., 'Create namespace loop-test'). "
            "Do NOT include actions belonging to other steps."
        ),
    )
    input_spec: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Mapping of input_key → 'AgentName.field' reference resolved from "
            "state.agent_results at runtime. Empty when the step takes no prior output."
        ),
    )


_MAX_PLAN_STEPS = 10


class TaskPlan(BaseModel):
    """Ordered, deterministic multi-step execution plan emitted by the supervisor."""
    steps: List[SequentialStep]
    query_summary: Optional[str] = Field(
        None,
        description="One-sentence user-facing summary of what this plan accomplishes.",
    )

    @model_validator(mode="after")
    def _check_max_steps(self) -> "TaskPlan":
        if len(self.steps) > _MAX_PLAN_STEPS:
            raise ValueError(
                f"TaskPlan exceeds max_plan_steps={_MAX_PLAN_STEPS} "
                f"(got {len(self.steps)}). Reduce the plan or split into separate queries."
            )
        return self


class PlanExecutionState(BaseModel):
    """Runtime tracking state for a TaskPlan execution."""
    current_step: int = 0
    completed_steps: List[str] = Field(default_factory=list)   # agent names of completed steps
    failure_step: Optional[int] = None   # 0-based step index where the plan failed


# User-facing agent names for plan preview (internal node name → description).
AGENT_FRIENDLY_NAMES: Dict[str, str] = {
    "Logs": "Fetch logs and events",
    "ConfigMapsSecrets": "Inspect ConfigMaps / Secrets",
    "RBAC": "Check access permissions",
    "Metrics": "Collect resource metrics",
    "Security": "Run security audit",
    "Lifecycle": "Manage workloads",
    "Execution": "Execute in container",
    "Deletion": "Clean up resources",
    "Infrastructure": "Inspect infrastructure",
    "DynamicToolsExecutor": "Run custom tool",
    "CodeGenerator": "Generate custom tool",
    "Apply": "Apply configuration",
    "DiagnosticsOrchestrator": "Run parallel diagnostics",
}


def format_plan_preview(task_plan_dict: dict | None, plan_names: list | None) -> str:
    """Produce a user-facing plan preview string from a committed plan.

    Prefers the structured task_plan (has query_summary and step details);
    falls back to the legacy agent-name list.  Returns empty string if no plan.
    """
    if task_plan_dict:
        try:
            tp = TaskPlan.model_validate(task_plan_dict)
            lines = []
            if tp.query_summary:
                lines.append(f"📋 **Plan**: {tp.query_summary}")
            lines.append(f"\n**{len(tp.steps)}-step execution plan:**")
            for i, step in enumerate(tp.steps, 1):
                friendly = AGENT_FRIENDLY_NAMES.get(step.agent, step.agent)
                lines.append(f"{i}. {friendly}")
            lines.append("")
            return "\n".join(lines)
        except Exception:
            pass  # fall through to legacy path

    if plan_names:
        steps = [AGENT_FRIENDLY_NAMES.get(a, a) for a in plan_names]
        body = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        return f"📋 **{len(steps)}-step execution plan:**\n{body}\n"

    return ""


# ---------------------------------------------------------------------------
# Base result
# ---------------------------------------------------------------------------

class AgentResult(BaseModel):
    """Common fields present on every agent result."""
    agent_name: str
    success: bool
    raw_output: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def _is_success(cls, raw_output: str) -> bool:
        """Heuristic: treat output as failed if it starts with a known error prefix."""
        lower = raw_output.lower().strip()
        return not (
            lower.startswith("error")
            or lower.startswith("failed")
            or lower.startswith("i don't have")
            or lower.startswith("i cannot")
            or lower.startswith("i'm unable")
            or "i don't have the necessary tools" in lower
        )


# ---------------------------------------------------------------------------
# Per-agent result types
# ---------------------------------------------------------------------------

_NAMESPACE_RE = re.compile(r"\bnamespace[:\s]+([a-z0-9\-]+)", re.IGNORECASE)
_POD_RE = re.compile(r"\bpod[:\s]+([a-z0-9\-\.]+)", re.IGNORECASE)
_ERROR_INDICATORS = (
    "crashloopbackoff", "oomkilled", "imagepullbackoff", "errimagepull",
    "backoff", "pending", "evicted", "error", "failed", "exception",
    "traceback",
)


class LogsResult(AgentResult):
    """Result produced by the Logs agent."""
    namespace: Optional[str] = None
    pod_name: Optional[str] = None
    log_lines: Optional[List[str]] = None      # up to 20 sampled lines
    error_indicators: Optional[List[str]] = None  # e.g. ["CrashLoopBackOff"]

    @classmethod
    def from_raw(cls, raw_output: str, agent_name: str = "Logs") -> "LogsResult":
        ns_match = _NAMESPACE_RE.search(raw_output)
        pod_match = _POD_RE.search(raw_output)
        lines = [ln.strip() for ln in raw_output.splitlines() if ln.strip()]
        indicators = [kw for kw in _ERROR_INDICATORS if kw in raw_output.lower()]
        return cls(
            agent_name=agent_name,
            success=cls._is_success(raw_output),
            raw_output=raw_output,
            namespace=ns_match.group(1) if ns_match else None,
            pod_name=pod_match.group(1) if pod_match else None,
            log_lines=lines[:20] if lines else None,
            error_indicators=indicators or None,
        )


class MetricsResult(AgentResult):
    """Result produced by the Metrics agent."""
    namespace: Optional[str] = None
    resource_type: Optional[str] = None        # "pod" | "node"
    high_cpu_resources: Optional[List[str]] = None
    high_memory_resources: Optional[List[str]] = None

    @classmethod
    def from_raw(cls, raw_output: str, agent_name: str = "Metrics") -> "MetricsResult":
        ns_match = _NAMESPACE_RE.search(raw_output)
        rtype = None
        lower = raw_output.lower()
        if "node" in lower:
            rtype = "node"
        elif "pod" in lower:
            rtype = "pod"
        # Extract resource names following high CPU / high memory patterns
        cpu_re = re.findall(r"([a-z0-9\-]+)\s+(?:cpu|CPU)[:\s]+[\d\.]+", raw_output)
        mem_re = re.findall(r"([a-z0-9\-]+)\s+(?:memory|mem|MiB|GiB)[:\s]+[\d\.]+", raw_output)
        return cls(
            agent_name=agent_name,
            success=cls._is_success(raw_output),
            raw_output=raw_output,
            namespace=ns_match.group(1) if ns_match else None,
            resource_type=rtype,
            high_cpu_resources=cpu_re[:10] or None,
            high_memory_resources=mem_re[:10] or None,
        )


class ConfigsResult(AgentResult):
    """Result produced by the Configs agent."""
    namespace: Optional[str] = None
    configmaps_found: Optional[List[str]] = None
    secrets_found: Optional[List[str]] = None

    @classmethod
    def from_raw(cls, raw_output: str, agent_name: str = "Configs") -> "ConfigsResult":
        ns_match = _NAMESPACE_RE.search(raw_output)
        cm_re = re.findall(r"\b([a-z0-9\-]+)\s+(?:configmap|ConfigMap)\b", raw_output, re.IGNORECASE)
        sec_re = re.findall(r"\b([a-z0-9\-]+)\s+(?:secret|Secret)\b", raw_output, re.IGNORECASE)
        return cls(
            agent_name=agent_name,
            success=cls._is_success(raw_output),
            raw_output=raw_output,
            namespace=ns_match.group(1) if ns_match else None,
            configmaps_found=cm_re[:20] or None,
            secrets_found=sec_re[:20] or None,
        )


class RBACResult(AgentResult):
    """Result produced by the RBAC agent."""
    namespace: Optional[str] = None
    roles_found: Optional[List[str]] = None
    bindings_found: Optional[List[str]] = None
    violations_found: Optional[List[str]] = None

    @classmethod
    def from_raw(cls, raw_output: str, agent_name: str = "RBAC") -> "RBACResult":
        ns_match = _NAMESPACE_RE.search(raw_output)
        role_re = re.findall(r"\bRole[:\s]+([a-z0-9\-]+)", raw_output, re.IGNORECASE)
        binding_re = re.findall(r"\bRoleBinding[:\s]+([a-z0-9\-]+)", raw_output, re.IGNORECASE)
        violation_re = re.findall(r"(?:violation|overly|excessive|wildcard)[^\n.]*", raw_output, re.IGNORECASE)
        return cls(
            agent_name=agent_name,
            success=cls._is_success(raw_output),
            raw_output=raw_output,
            namespace=ns_match.group(1) if ns_match else None,
            roles_found=role_re[:20] or None,
            bindings_found=binding_re[:20] or None,
            violations_found=[v.strip() for v in violation_re[:10]] or None,
        )


class SecurityResult(AgentResult):
    """Result produced by the Security agent."""
    namespace: Optional[str] = None
    vulnerabilities_found: Optional[List[str]] = None
    compliance_issues: Optional[List[str]] = None
    privileged_pods: Optional[List[str]] = None

    @classmethod
    def from_raw(cls, raw_output: str, agent_name: str = "Security") -> "SecurityResult":
        ns_match = _NAMESPACE_RE.search(raw_output)
        vuln_re = re.findall(r"(?:CVE-\d{4}-\d+|vulnerability|HIGH|CRITICAL)[^\n.]*", raw_output, re.IGNORECASE)
        compliance_re = re.findall(r"(?:policy|psp|psa|violation|non-compliant)[^\n.]*", raw_output, re.IGNORECASE)
        priv_re = re.findall(r"(?:privileged|root|hostPID|hostNetwork)[^\n]*pod[^\n]*", raw_output, re.IGNORECASE)
        return cls(
            agent_name=agent_name,
            success=cls._is_success(raw_output),
            raw_output=raw_output,
            namespace=ns_match.group(1) if ns_match else None,
            vulnerabilities_found=[v.strip() for v in vuln_re[:10]] or None,
            compliance_issues=[c.strip() for c in compliance_re[:10]] or None,
            privileged_pods=[p.strip() for p in priv_re[:10]] or None,
        )


class LifecycleResult(AgentResult):
    """Result produced by the Lifecycle agent."""
    namespace: Optional[str] = None
    deployment_name: Optional[str] = None
    operation: Optional[str] = None            # "scale" | "restart" | "rollout" | "cordon" | "hpa"
    new_replica_count: Optional[int] = None

    @classmethod
    def from_raw(cls, raw_output: str, agent_name: str = "Lifecycle") -> "LifecycleResult":
        ns_match = _NAMESPACE_RE.search(raw_output)
        dep_re = re.search(r"\bdeployment[:\s]+([a-z0-9\-]+)", raw_output, re.IGNORECASE)
        op = None
        lower = raw_output.lower()
        for kw, label in (("scale", "scale"), ("restart", "restart"), ("rollout", "rollout"),
                          ("cordon", "cordon"), ("uncordon", "uncordon"), ("hpa", "hpa")):
            if kw in lower:
                op = label
                break
        replica_re = re.search(r"(?:scaled to|replicas?[:\s]+)(\d+)", raw_output, re.IGNORECASE)
        return cls(
            agent_name=agent_name,
            success=cls._is_success(raw_output),
            raw_output=raw_output,
            namespace=ns_match.group(1) if ns_match else None,
            deployment_name=dep_re.group(1) if dep_re else None,
            operation=op,
            new_replica_count=int(replica_re.group(1)) if replica_re else None,
        )


class GeneralAgentResult(AgentResult):
    """Generic result for agents without a specialised schema (Execution, Deletion, etc.)."""

    @classmethod
    def from_raw(cls, raw_output: str, agent_name: str) -> "GeneralAgentResult":
        return cls(
            agent_name=agent_name,
            success=cls._is_success(raw_output),
            raw_output=raw_output,
        )


class SignalResult(BaseModel):
    """Result from one diagnostic signal (logs, metrics, or events)."""
    signal: str                      # "logs" | "metrics" | "events"
    success: bool
    data: Optional[str] = None       # truncated tool output on success
    error: Optional[str] = None      # error message on failure (timeout, tool error, etc.)


class DiagnosticsResult(AgentResult):
    """Aggregated result from DiagnosticsOrchestrator — three parallel signals."""
    namespace: str
    pod_name: Optional[str] = None
    signals: List[SignalResult] = Field(default_factory=list)
    partial_failure: bool = False    # True when ≥1 signal failed

    @classmethod
    def from_raw(cls, raw_output: str, agent_name: str = "DiagnosticsOrchestrator") -> "DiagnosticsResult":
        return cls(
            agent_name=agent_name,
            success=cls._is_success(raw_output),
            raw_output=raw_output,
            namespace="unknown",
        )

    def to_supervisor_message(self) -> str:
        lines = [
            f"**DiagnosticsOrchestrator** — namespace: `{self.namespace}`"
            + (f", pod: `{self.pod_name}`" if self.pod_name else ""),
        ]
        for s in self.signals:
            status = "✓" if s.success else "✗"
            lines.append(f"\n### {status} {s.signal.capitalize()}")
            if s.success and s.data:
                lines.append(s.data[:800])
            elif s.error:
                lines.append(f"**Error**: {s.error}")
        if self.partial_failure:
            failed = [s.signal for s in self.signals if not s.success]
            lines.append(
                f"\n⚠️ Partial failure — signals unavailable: {', '.join(failed)}. "
                "Use individual agents (Logs / Metrics / Logs-for-events) for deeper investigation."
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry: map node names → factory functions
# ---------------------------------------------------------------------------

_AGENT_RESULT_FACTORIES = {
    "Logs": LogsResult.from_raw,
    "Metrics": MetricsResult.from_raw,
    "Configs": ConfigsResult.from_raw,
    "RBAC": RBACResult.from_raw,
    "Security": SecurityResult.from_raw,
    "Lifecycle": LifecycleResult.from_raw,
    "DiagnosticsOrchestrator": DiagnosticsResult.from_raw,
}


def build_agent_result(agent_name: str, raw_output: str) -> AgentResult:
    """
    Build a typed AgentResult for the given agent and its raw string output.

    Falls back to GeneralAgentResult for agents without a specialised schema.
    """
    factory = _AGENT_RESULT_FACTORIES.get(agent_name)
    if factory:
        return factory(raw_output, agent_name)
    return GeneralAgentResult.from_raw(raw_output, agent_name)
