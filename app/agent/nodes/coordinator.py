"""Coordinator node — decides whether to answer directly or fan-out to RCA subagents."""
from __future__ import annotations

import re
import time

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt

from app.agent.state import AgentState
from app.core.llm import get_coordinator_llm
from app.streaming.emitter import StatusEvent, emit
from app.tools.registry import ALL_TOOLS
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Keep the last N messages from session history to prevent context bloat.
# Each exchange ≈ 4 messages (HumanMessage + AIMessage(tool_call) + ToolMessage + AIMessage).
# 20 messages ≈ 5 prior exchanges — enough context while capping prompt growth.
_MAX_SESSION_MESSAGES = 20


def _trim_session_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Return recent messages capped at _MAX_SESSION_MESSAGES, preserving exchange integrity.

    A naive tail-slice can start with a ToolMessage whose AIMessage(tool_calls) was
    cut off, causing Azure to reject the request with a 400 error. We always advance
    to the first HumanMessage in the window so every retained exchange is complete.
    """
    if len(messages) <= _MAX_SESSION_MESSAGES:
        return messages

    original_len = len(messages)
    trimmed = messages[-_MAX_SESSION_MESSAGES:]

    # Advance past any leading non-human messages (ToolMessage / AIMessage orphans)
    # so the window always starts at a clean exchange boundary.
    first_human = next(
        (i for i, m in enumerate(trimmed) if hasattr(m, "type") and m.type == "human"),
        0,
    )
    trimmed = trimmed[first_human:]

    logger.warning(
        f"coordinator: trimmed session history {original_len} → {len(trimmed)} messages"
    )
    return trimmed


# ── Tool output trimmer (A4 — ISS-01) ────────────────────────────────────────

_TOOL_OUTPUT_MAX_CHARS = 2_000
_KUBECTL_TABLE_ROWS = 30
_KUBECTL_KEEP_RE = re.compile(
    r"error|warning|failed|pending|oomkilled|crashloop|backoff|imagepull|containercreating",
    re.IGNORECASE,
)


def _trim_tool_output(content: str) -> str:
    if len(content) <= _TOOL_OUTPUT_MAX_CHARS:
        return content

    lines = content.splitlines(keepends=True)

    if lines and "NAME" in lines[0].upper():
        # kubectl table: header + first N rows + any important rows
        header = lines[0]
        kept: list[str] = []
        row_count = 0
        for line in lines[1:]:
            if _KUBECTL_KEEP_RE.search(line):
                kept.append(line)
            elif row_count < _KUBECTL_TABLE_ROWS:
                kept.append(line)
                row_count += 1
        trimmed = header + "".join(kept)
    else:
        # logs / describe / prometheus / loki: keep first 60 lines
        trimmed = "".join(lines[:60])

    if len(trimmed) > _TOOL_OUTPUT_MAX_CHARS:
        omitted = len(trimmed) - _TOOL_OUTPUT_MAX_CHARS
        trimmed = (
            trimmed[:_TOOL_OUTPUT_MAX_CHARS]
            + f"\n[+{omitted} chars trimmed from LLM context]"
        )
    return trimmed


def _trim_tool_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Cap ToolMessage content before it enters the LLM context (ISS-01)."""
    result: list[BaseMessage] = []
    for msg in messages:
        if isinstance(msg, ToolMessage) and isinstance(msg.content, str):
            trimmed = _trim_tool_output(msg.content)
            if trimmed != msg.content:
                msg = ToolMessage(
                    content=trimmed,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name,
                )
        result.append(msg)
    return result


# ── Coordinator system prompt ─────────────────────────────────────────────────

_COORDINATOR_SYSTEM = """\
You are KubeIntellect, an expert Kubernetes operations AI.

You have access to three tools:
- run_kubectl: run any kubectl command against the cluster
- query_prometheus: query Prometheus metrics (PromQL)
- query_loki: query Loki for application logs (LogQL)

## Cluster Snapshot
A real-time snapshot is pre-loaded in your context (see "## Cluster Snapshot" section).
ALWAYS consult this before making tool calls.
- If the answer is in the snapshot (e.g. pod state, warning events), answer without extra tool calls.
- If a Warning Event shows the exact error message, use it directly.
- Only call tools to DRILL DOWN into specific resources found in the snapshot.

## Parallel Tool Execution
Emit ALL independent tool calls in a SINGLE response. The runtime executes them concurrently.
Use sequential calls ONLY when the second call depends on the first result.

Parallel (always):   (get pods) + (get events) + (describe node)
Parallel (always):   (loki error query) + (prometheus CPU query)
Sequential (only):   (get pod name) → (describe that pod) → (patch that pod)

## Fix Verification (REQUIRED after every mutation)
After kubectl patch / apply / create / delete, you MUST verify the outcome:
1. Make one more kubectl get call on the affected resource (e.g. kubectl get pods -n <ns>)
2. Report ACTUAL state: "Pod is now Running (verified)" or "Fix applied — pod still in <state>"
Never end after applying a fix without a follow-up verification read.

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

For mutations, NEVER use kubectl edit (no interactive terminal available).
Use kubectl patch or kubectl apply -f - with stdin instead.

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
    except GraphInterrupt:
        raise  # HITL — expected, not a failure
    except Exception as exc:
        logger.error(f"coordinator: LLM call failed session={session_id} error={exc!r}")
        raise
    elapsed = time.monotonic() - t0

    # Guard: LLM returned nothing — context too large or rate-limited silently
    if not result.get("messages"):
        logger.warning(f"coordinator: LLM returned no messages session={session_id} — likely context overflow")
        error_text = (
            "I was unable to generate a response — the session context may have grown too large. "
            "Please start a new session to continue."
        )
        return {"messages": [AIMessage(content=error_text)]}

    last = result["messages"][-1].content.strip()
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
    cluster_snapshot = state.get("cluster_snapshot", "")
    system_parts = [_COORDINATOR_SYSTEM]
    if memory_context:
        system_parts.append(f"\n\n## Cluster Context\n{memory_context}")
    if cluster_snapshot:
        system_parts.append(f"\n\n{cluster_snapshot}")

    llm = get_coordinator_llm()
    agent = create_react_agent(llm, tools=ALL_TOOLS)

    history = _trim_session_messages(list(state["messages"]))
    input_messages = [SystemMessage(content="\n".join(system_parts))] + history
    result = await agent.ainvoke({"messages": input_messages}, config=config)

    new_messages = result["messages"][len(input_messages):]
    new_messages = _trim_tool_messages(new_messages)  # A4: cap tool output before state storage
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
