"""DeepAgents subagent specs for KubeIntellect (v3).

Five specialists. The first four mirror v2's domain experts; the fifth
(`deep_investigator`) handles cross-domain or unusual probes.

Each subagent runs on the smaller/faster `subagent_llm` rather than the
coordinator model, matching v2's cost profile. They share the virtual
filesystem (read_file / write_file / ls are added automatically by the
framework) and write their output to `/findings/<name>.md`.
"""

from __future__ import annotations

from deepagents.middleware.subagents import SubAgent

from app.agent.prompts import (
    DEEP_INVESTIGATOR_PROMPT,
    EVENTS_SPECIALIST_PROMPT,
    LOGS_SPECIALIST_PROMPT,
    METRICS_SPECIALIST_PROMPT,
    POD_SPECIALIST_PROMPT,
)
from app.core.llm import get_subagent_llm
from app.tools.kubectl_tool import run_kubectl
from app.tools.loki_tool import query_loki
from app.tools.prometheus_tool import query_prometheus


def build_subagents() -> list[SubAgent]:
    """Build the SubAgent specs lazily so the LLM factory only fires when needed."""
    model = get_subagent_llm()

    return [
        SubAgent(
            name="pod_specialist",
            description=(
                "Diagnoses pod-level failures (CrashLoopBackOff, OOMKilled, "
                "ImagePullBackOff, probe failures, restarts). Use for any "
                "single-pod or workload-pod symptom."
            ),
            system_prompt=POD_SPECIALIST_PROMPT,
            tools=[run_kubectl],
            model=model,
        ),
        SubAgent(
            name="metrics_specialist",
            description=(
                "Investigates CPU, memory, saturation, and SLO questions via "
                "Prometheus. Use for resource-shaped or trend questions."
            ),
            system_prompt=METRICS_SPECIALIST_PROMPT,
            tools=[query_prometheus],
            model=model,
        ),
        SubAgent(
            name="logs_specialist",
            description=(
                "Investigates application logs and error patterns via Loki. "
                "Use for log-volume spikes, error-message hunts, cross-pod "
                "log correlation."
            ),
            system_prompt=LOGS_SPECIALIST_PROMPT,
            tools=[query_loki],
            model=model,
        ),
        SubAgent(
            name="events_specialist",
            description=(
                "Investigates cluster events (scheduling failures, "
                "FailedMount, image pull issues, eviction). Use for "
                "control-plane or node-level symptoms."
            ),
            system_prompt=EVENTS_SPECIALIST_PROMPT,
            tools=[run_kubectl],
            model=model,
        ),
        SubAgent(
            name="deep_investigator",
            description=(
                "Catch-all for cross-domain or unusual probes that don't fit "
                "a single specialist (compare namespaces, validate "
                "NetworkPolicy, correlate metric+log spikes, RBAC audit). "
                "Has the full tool surface."
            ),
            system_prompt=DEEP_INVESTIGATOR_PROMPT,
            tools=[run_kubectl, query_prometheus, query_loki],
            model=model,
        ),
    ]
