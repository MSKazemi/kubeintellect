"""Coordinator node — decides whether to answer directly or fan-out to RCA subagents."""
from __future__ import annotations

import time

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.core.llm import get_coordinator_llm
from app.streaming.emitter import StatusEvent, emit
from app.tools.registry import ALL_TOOLS
from app.utils.logger import get_logger

logger = get_logger(__name__)

_COORDINATOR_SYSTEM = """\
You are KubeIntellect, an expert Kubernetes operations AI.

You have access to three tools:
- run_kubectl: run any kubectl command against the cluster
- query_prometheus: query Prometheus metrics (PromQL)
- query_loki: query Loki for application logs (LogQL)

Tool-selection by time intent — CRITICAL:

  "Current / active issues" (no time qualifier, or "now", "today"):
    - Use kubectl get pods --all-namespaces → shows live state only.
    - kubectl describe pod → shows Last State (exit reason + timestamp).
    - query_prometheus(range_minutes=0) → current metric snapshot (optional).
    - Do NOT use range_minutes>0 or query_loki for this case: they surface
      already-resolved problems and produce false positives.

  "Historical issues" (user says "last N hours/days", "yesterday", "last night"):
    - query_prometheus with range_minutes matching the window:
        increase(kube_pod_container_status_restarts_total[Nm]) ← pods that restarted
        kube_pod_container_status_last_terminated_reason        ← termination cause
    - query_loki with since="Nh" or since="Nd" → logs from that window.
    - kubectl describe pod → still useful for Last State timestamps.
    - Do NOT rely on kubectl get pods for history: it shows only current state.

  "Pods with issues" (no qualifier) means pods NOT in a desired state RIGHT NOW:
    - Non-Running phases: CrashLoopBackOff, Error, OOMKilled, ImagePullBackOff,
      Pending (stuck), Terminating (stuck), ContainerCreating (stuck).
    - kubectl get pods --all-namespaces is the correct and sufficient tool.

For SIMPLE questions (cluster info, status checks, single-resource lookups):
  Answer directly using tools as needed.
  When the user asks to list or get resources (pods, nodes, deployments, etc.),
  always show the COMPLETE raw output in a code block — never summarize or omit rows.

For COMPLEX root-cause analysis (pod crashes, service outages, performance degradation):
  Respond with EXACTLY: "RCA_REQUIRED"
  The system will automatically dispatch 4 specialist subagents in parallel.

When synthesizing subagent findings (messages contain <findings> XML):
  Produce a comprehensive root-cause analysis with a concrete fix recommendation.
  Be specific: name the exact resource, namespace, and remediation command.

IMPORTANT — Truncated output:
  If any tool output contains a truncation marker (text like "[truncated" or "chars omitted"),
  you MUST include a visible warning in your response, for example:
  "> ⚠️ Output was truncated — use narrower filters (e.g. `-n <namespace>`, `-l <label>`, `--tail`) to see the full result."
  Never silently drop this warning. The user must know the list is incomplete.
"""


async def coordinator(state: AgentState, config: RunnableConfig = None) -> dict:
    """
    Coordinator node.  Always returns a plain state-update dict — never Send objects.

    Two modes:
    - Decision  : no findings yet → ask the LLM; if it says RCA_REQUIRED set the flag.
    - Synthesis : findings present → synthesize them into a final RCAResult.

    The fan-out itself is the responsibility of route_coordinator (workflow.py), which
    reads the rca_required flag and returns list[Send] for LangGraph to execute.
    """
    session_id = state.get("session_id", "-")
    user_id = state.get("user_id", "-")

    # ── Synthesis mode: subagent findings are ready ───────────────────────────
    if state.get("findings"):
        logger.debug(f"coordinator: synthesizing {len(state['findings'])} findings session={session_id}")
        await emit(session_id, StatusEvent(
            phase="synthesizing",
            message="Synthesizing specialist findings…",
            session_id=session_id,
        ))
        return await _synthesize(state)

    # ── Decision mode: ask the LLM ────────────────────────────────────────────
    await emit(session_id, StatusEvent(
        phase="analyzing",
        message="Analyzing your request…",
        session_id=session_id,
    ))

    last_user_msg = ""
    for m in reversed(state.get("messages", [])):
        if hasattr(m, "type") and m.type == "human":
            last_user_msg = m.content[:120]
            break
    logger.debug(f"coordinator: invoking LLM user={user_id} session={session_id} msg={last_user_msg!r}")

    t0 = time.monotonic()
    try:
        result = await _direct_answer(state, config=config)
    except Exception as exc:
        logger.error(f"coordinator: LLM call failed session={session_id} error={exc!r}")
        raise
    elapsed = time.monotonic() - t0

    last = result["messages"][-1].content.strip() if result.get("messages") else ""
    is_rca = last == "RCA_REQUIRED"
    logger.debug(
        f"coordinator: LLM responded in {elapsed:.2f}s session={session_id} "
        f"decision={'RCA' if is_rca else 'direct'}"
    )

    if is_rca:
        logger.info("coordinator: LLM requested RCA — setting rca_required flag for fan-out")
        await emit(session_id, StatusEvent(
            phase="dispatching",
            message="Dispatching specialist subagents (pod · metrics · logs · events)…",
            session_id=session_id,
        ))
        # Do NOT add the "RCA_REQUIRED" sentinel text to message history.
        # route_coordinator reads rca_required and issues the Send fan-out.
        return {"rca_required": True}

    return result


async def _direct_answer(state: AgentState, config: RunnableConfig = None) -> dict:
    """Run coordinator LLM with tools for simple queries."""
    from langgraph.prebuilt import create_react_agent

    memory_context = state.get("memory_context", "")
    system_parts = [_COORDINATOR_SYSTEM]
    if memory_context:
        system_parts.append(f"\n\n## Cluster Context\n{memory_context}")

    llm = get_coordinator_llm()
    agent = create_react_agent(llm, tools=ALL_TOOLS)

    input_messages = [SystemMessage(content="\n".join(system_parts))] + list(state["messages"])
    result = await agent.ainvoke({"messages": input_messages}, config=config)

    new_messages = result["messages"][len(input_messages):]
    tool_calls = sum(1 for m in new_messages if hasattr(m, "tool_calls") and m.tool_calls)
    logger.debug(
        f"coordinator: direct answer complete new_msgs={len(new_messages)} tool_calls={tool_calls}"
    )
    return {"messages": new_messages}


async def _synthesize(state: AgentState) -> dict:
    """Synthesize subagent findings into a final RCAResult."""
    import json

    from langchain_core.messages import HumanMessage

    from app.agent.state import RCAResult

    findings = state["findings"]
    findings_xml = "\n".join(
        f"<finding domain='{f.domain}' confidence='{f.confidence}'>\n"
        f"  hypothesis: {f.hypothesis}\n"
        f"  signals: {', '.join(f.signals)}\n"
        f"  evidence: {chr(10).join(f.evidence[:3])}\n"
        f"</finding>"
        for f in findings
    )

    synthesis_prompt = f"""
You have received findings from 4 specialist subagents:

<findings>
{findings_xml}
</findings>

Synthesize these into a single root-cause analysis. Respond with ONLY a JSON object:
{{
  "root_cause": "<single-sentence root cause>",
  "confidence": <0.0-1.0>,
  "supporting_evidence": ["<evidence 1>", ...],
  "conflicting_evidence": ["<conflict 1>" or []],
  "reasoning": "<chain-of-thought over the findings>",
  "recommended_fix": "<concrete kubectl/config fix>",
  "affected_domain": ["<domain>", ...]
}}
"""

    llm = get_coordinator_llm()
    response = await llm.ainvoke(
        [SystemMessage(content=_COORDINATOR_SYSTEM), HumanMessage(content=synthesis_prompt)]
    )

    raw = response.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    try:
        rca = RCAResult.model_validate(json.loads(raw.strip()))
    except Exception as exc:
        logger.warning(f"coordinator: failed to parse RCA JSON — {exc}")
        rca = RCAResult(
            root_cause="Synthesis failed — see individual findings",
            confidence=0.0,
            supporting_evidence=[f.hypothesis for f in findings],
            reasoning=f"Parse error: {exc}",
            recommended_fix="Review findings manually",
        )

    summary = (
        f"**Root Cause**: {rca.root_cause}\n\n"
        f"**Confidence**: {rca.confidence:.0%}\n\n"
        f"**Recommended Fix**: {rca.recommended_fix}\n\n"
        f"**Reasoning**: {rca.reasoning}"
    )

    return {
        "rca_result": rca,
        "rca_required": False,
        "messages": [AIMessage(content=summary)],
    }
