"""RCA specialist subagent — one domain, ReAct loop, structured AgentFinding output."""
from __future__ import annotations

import json

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from app.agent.state import AgentFinding, SubagentInput
from app.core.llm import get_subagent_llm
from app.tools.registry import ALL_TOOLS
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Each ReAct step consumes ~3 recursion units (LLM call + tool call + tool response).
# Allow up to ~16 tool calls per subagent before giving up.
_SUBAGENT_RECURSION_LIMIT = 50

_DOMAIN_PROMPTS: dict[str, str] = {
    "pod": (
        "You are a Kubernetes pod-health specialist. "
        "Investigate pod status, container restarts, OOMKilled events, "
        "image pull errors, readiness/liveness probe failures, and resource limits. "
        "Use kubectl (get pods --all-namespaces, describe pod, logs) to gather evidence. "
        "Limit yourself to at most 6 tool calls — focus on the most suspicious pods first."
    ),
    "metrics": (
        "You are a Kubernetes metrics specialist. "
        "Investigate CPU/memory usage, HPA scaling events, throttling, "
        "and resource saturation via Prometheus queries. "
        "Look for anomalies in the last 30 minutes (use range_minutes=30). "
        "Use instant queries (range_minutes=0) for current resource utilisation. "
        "Limit yourself to at most 6 Prometheus queries — prioritise high-level signals first."
    ),
    "logs": (
        "You are a Kubernetes application-log specialist. "
        "Investigate recent error/warning log lines from affected pods via Loki. "
        "Identify error patterns, stack traces, and timing correlations.\n\n"
        "IMPORTANT — query efficiency:\n"
        "  Use a single broad query that covers all namespaces at once, for example:\n"
        "    {namespace=~\".+\"} |= `ERROR` \n"
        "  or target the most relevant namespace first:\n"
        "    {namespace=\"kubeintellect\"} |= `ERROR`\n"
        "  Do NOT query each namespace in a separate call. "
        "Limit yourself to at most 4 Loki queries total."
    ),
    "events": (
        "You are a Kubernetes cluster-events specialist. "
        "Investigate warning events, scheduler failures, node pressure, "
        "PVC binding issues, and network policy rejections via kubectl events. "
        "Use 'kubectl get events --all-namespaces' as your first call to get a full picture. "
        "Limit yourself to at most 5 tool calls."
    ),
}

_FINDING_SCHEMA_HINT = """
After your investigation, respond with a JSON object matching this schema:
{
  "domain": "<your domain>",
  "signals": ["<key signal 1>", ...],
  "hypothesis": "<root-cause hypothesis>",
  "confidence": <0.0-1.0>,
  "evidence": ["<verbatim excerpt 1>", ...],
  "tool_calls_made": ["<tool(args)>", ...]
}
Respond with ONLY the JSON object, no markdown fences.
"""


async def run_subagent(payload: SubagentInput) -> AgentFinding:
    """Execute one specialist subagent and return a structured AgentFinding."""
    domain = payload["domain"]
    messages = payload["messages"]
    memory_context = payload.get("memory_context", "")

    logger.debug(f"subagent [{domain}]: starting investigation")

    system_parts = [_DOMAIN_PROMPTS[domain]]
    if memory_context:
        system_parts.append(f"\n\n## Cluster Context\n{memory_context}")
    system_parts.append(_FINDING_SCHEMA_HINT)
    system_prompt = "\n".join(system_parts)

    llm = get_subagent_llm()
    agent = create_react_agent(llm, tools=ALL_TOOLS)

    input_messages = [SystemMessage(content=system_prompt)] + list(messages)

    result = await agent.ainvoke(
        {"messages": input_messages},
        config={"recursion_limit": _SUBAGENT_RECURSION_LIMIT},
    )

    # Extract the last AI message text as the finding JSON
    last_msg = result["messages"][-1]
    raw_content = last_msg.content if hasattr(last_msg, "content") else str(last_msg)

    try:
        clean = raw_content.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        finding = AgentFinding.model_validate(json.loads(clean.strip()))
    except Exception as exc:
        logger.warning(f"subagent [{domain}]: failed to parse finding JSON — {exc}")
        finding = AgentFinding(
            domain=domain,
            signals=["(parse error)"],
            hypothesis="Could not parse subagent response",
            confidence=0.0,
            evidence=[raw_content[:500]],
        )

    logger.debug(f"subagent [{domain}]: confidence={finding.confidence:.2f}")
    return finding
