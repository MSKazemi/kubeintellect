"""Coordinator node — decides whether to answer directly or fan-out to RCA subagents."""
from __future__ import annotations

import re
import time

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt

from app.agent.state import AgentState, PlanStep
from app.core.config import settings
from app.core.llm import get_coordinator_llm
from app.streaming.emitter import PlanEvent, StatusEvent, emit
from app.tools.registry import ALL_TOOLS
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TARGETED_RE = re.compile(
    r"TARGETED:\s*namespace\s*=\s*(\S+?),\s*pod\s*=\s*(\S+?),\s*issue\s*=\s*(.+)",
    re.IGNORECASE,
)

# Investigation plan parser.
# Matches a leading "INVESTIGATION_PLAN:" block followed by one or more
# "- <step>" lines. The block is stripped from the message body before storage.
_PLAN_BLOCK_RE = re.compile(
    r"^\s*INVESTIGATION_PLAN:\s*\n((?:-\s+.+\n?)+)",
    re.MULTILINE,
)
_PLAN_STEP_RE = re.compile(r"^-\s+(.+)$", re.MULTILINE)
_PLAN_MIN_STEPS = 3

# Keep the last N messages from session history to prevent context bloat.
# Each exchange ≈ 4 messages (HumanMessage + AIMessage(tool_call) + ToolMessage + AIMessage).
# 20 messages ≈ 5 prior exchanges — enough context while capping prompt growth.
_MAX_SESSION_MESSAGES = 20


def _compress_dropped_messages(dropped: list[BaseMessage]) -> str:
    """Build a compact deterministic summary of dropped messages.

    Extracts: user topics, kubectl/query commands run, and key tool results.
    No LLM call — synchronous and zero-latency.
    """
    lines: list[str] = ["## Earlier Session Context (compressed)"]
    for msg in dropped:
        msg_type = getattr(msg, "type", None)
        if msg_type == "human" and isinstance(msg.content, str):
            topic = msg.content.strip().replace("\n", " ")[:120]
            lines.append(f"- User: {topic}")
        elif msg_type == "ai":
            # Extract tool calls (kubectl commands, queries)
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                cmd = args.get("command") or args.get("query") or args.get("logql") or ""
                if cmd:
                    lines.append(f"- Ran: {str(cmd)[:100]}")
            # Plain AI text (answers, decisions)
            if not tool_calls and isinstance(msg.content, str):
                snippet = msg.content.strip().replace("\n", " ")[:120]
                if snippet:
                    lines.append(f"- Assistant: {snippet}")
        elif msg_type == "tool" and isinstance(msg.content, str):
            # Keep only first line of tool output as a hint
            first_line = msg.content.strip().splitlines()[0][:100] if msg.content.strip() else ""
            if first_line:
                lines.append(f"  → {first_line}")
    return "\n".join(lines)


def _trim_session_messages(messages: list[BaseMessage]) -> tuple[list[BaseMessage], str | None]:
    """Return (recent_messages, compressed_summary_of_dropped).

    Caps history at _MAX_SESSION_MESSAGES, preserving exchange integrity by
    advancing to the first HumanMessage in the window (a naive tail-slice can
    start with a ToolMessage whose parent AIMessage(tool_calls) was cut off,
    causing Azure to reject with 400).

    Returns a non-None summary string when messages were dropped, so callers
    can inject it into the system prompt to preserve earlier context.
    """
    if len(messages) <= _MAX_SESSION_MESSAGES:
        return messages, None

    original_len = len(messages)
    keep = messages[-_MAX_SESSION_MESSAGES:]

    # Advance past any leading non-human messages (ToolMessage / AIMessage orphans).
    first_human = next(
        (i for i, m in enumerate(keep) if hasattr(m, "type") and m.type == "human"),
        0,
    )
    keep = keep[first_human:]
    dropped = messages[: original_len - len(keep)]

    summary = _compress_dropped_messages(dropped) if dropped else None

    logger.debug(
        f"coordinator: compressed session history {original_len} → {len(keep)} messages "
        f"({len(dropped)} dropped, summary={'yes' if summary else 'no'})"
    )
    return keep, summary


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


# ── Investigation plan extraction ────────────────────────────────────────────


def _extract_plan(messages: list[BaseMessage]) -> tuple[list[PlanStep], list[BaseMessage]]:
    """Strip an INVESTIGATION_PLAN block from the first AIMessage; return steps + cleaned messages.

    Returns ([], messages) when no plan block is found or the block has fewer
    than _PLAN_MIN_STEPS steps. Steps with only whitespace are skipped.
    """
    if not messages:
        return [], messages
    cleaned: list[BaseMessage] = []
    plan: list[PlanStep] = []
    consumed = False
    for msg in messages:
        if (
            not consumed
            and isinstance(msg, AIMessage)
            and isinstance(msg.content, str)
        ):
            match = _PLAN_BLOCK_RE.search(msg.content)
            if match:
                steps_text = match.group(1)
                step_lines = [
                    s.strip()
                    for s in _PLAN_STEP_RE.findall(steps_text)
                    if s.strip()
                ]
                if len(step_lines) >= _PLAN_MIN_STEPS:
                    plan = [PlanStep(description=s) for s in step_lines]
                    new_content = (msg.content[:match.start()] + msg.content[match.end():]).strip()
                    msg = AIMessage(
                        content=new_content,
                        tool_calls=getattr(msg, "tool_calls", []) or [],
                    )
                    consumed = True
        cleaned.append(msg)
    return plan, cleaned


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

## Investigation Discipline
For any query that requires tool calls, follow these phases strictly:
  1. PLAN  — decide every tool call needed to answer the question completely.
  2. FETCH — emit ALL independent tool calls in ONE response (parallel).
  3. SYNTHESIZE — after all tool results return, produce ONE final answer.

Never respond with a partial answer and then call more tools to refine it.
Exception (sequential dependency): the second call genuinely depends on the
first result — e.g. "find the failing pod's name → describe THAT pod". Even
then, gather everything you can in parallel at each step.

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

## Routing Decision
Choose investigation depth based on the request:

SIMPLE — answer directly from the Cluster Snapshot and/or tool calls.
  Use for: list requests, status checks, single-resource lookups, mutations.
  When listing resources, always show COMPLETE raw output in a code block.

TARGETED — emit exactly on its own line:
  TARGETED: namespace=<ns>, pod=<pod>, issue=<one-line description>
  Use for: ONE specific resource is failing and needs deeper investigation
  (describe, events, deployment check). The system runs parallel reads and
  returns the results to you for the final answer.
  Do NOT escalate to RCA_REQUIRED for single-resource issues — TARGETED is sufficient.

RCA_REQUIRED — emit exactly: RCA_REQUIRED
  Use for: multi-pod / cross-namespace outages, unknown root cause, cascading failures.
  The system dispatches 4 specialist subagents in parallel.

For mutations, NEVER use kubectl edit (no interactive terminal available).
Use kubectl patch or kubectl apply -f - with stdin instead.

IMPORTANT — ConfigMaps and content with special characters:
  When creating or updating a ConfigMap whose values contain HTML, JSON, YAML,
  or any multi-line / special-character content, ALWAYS use kubectl apply -f -
  with a YAML manifest passed via stdin. NEVER use --from-literal with such
  content — the argument quoting becomes fragile and error-prone.
  Example: kubectl apply -f - (then pass the full ConfigMap YAML in stdin)

When synthesizing subagent findings (messages contain <findings> XML):
  Produce a comprehensive root-cause analysis with a concrete fix recommendation.
  Be specific: name the exact resource, namespace, and remediation command.

IMPORTANT — Truncated output:
  If any tool output contains a truncation marker (text like "[truncated" or "chars omitted"),
  you MUST include a visible warning in your response, for example:
  "> ⚠️ Output was truncated — use narrower filters (e.g. `-n <namespace>`, `-l <label>`, `--tail`) to see the full result."
  Never silently drop this warning. The user must know the list is incomplete.
"""


# ── Investigation Plan prompt block ───────────────────────────────────────────

_PLAN_PROMPT_BLOCK = """\

## Investigation Plan
For queries requiring 3+ tool calls, write the plan as the FIRST line of your
response, in this exact format:

INVESTIGATION_PLAN:
- <step 1 description>
- <step 2 description>
- <step 3 description>
- ...

Then proceed with your tool calls. After all tool results return, your final
answer must address every step. Do not emit a plan for trivial single-call
queries — only when 3 or more steps are needed.
"""


# ── Proactive Fix prompt block (injected when hitl_bypass=True) ───────────────

_PROACTIVE_FIX_BLOCK = """\

## Proactive Fix Mode (auto-approve active)
You are running in auto-approve mode — no human confirmation is required before
mutations. When you have identified the root cause and the fix is clear:

1. Apply the fix immediately via run_kubectl (patch / apply / create / delete).
   Do NOT say "let me know if you'd like me to apply this" — just apply it.
2. For ambiguous parameters (e.g. which image tag to use), pick the safest
   well-known default: `latest` for public images, the lowest-severity change
   for resource limits, etc. State your choice in the response.
3. Verify after every mutation: run `kubectl get` on the affected resource and
   report the actual post-fix state ("Pod is now Running (verified)").
4. If the fix cannot be determined with confidence, say so explicitly and stop —
   do not apply a guess.
"""


# ── Snapshot Sufficiency prompt block ─────────────────────────────────────────


def _snapshot_sufficiency_block(state: AgentState) -> str:
    """Render the Snapshot Sufficiency block when the mode is on.

    Returns an empty string when SNAPSHOT_SUFFICIENCY_MODE='off'.
    """
    mode = settings.SNAPSHOT_SUFFICIENCY_MODE
    if mode == "off":
        return ""
    age_s = max(0, int(time.time() - state.get("snapshot_built_at", time.time())))
    issues = bool(state.get("snapshot_has_issues", False))
    warnings = bool(state.get("snapshot_has_warnings", False))
    pod_count = int(state.get("snapshot_pod_count", 0))
    fresh_threshold = settings.SNAPSHOT_FRESHNESS_SECONDS

    bias_strength = "Strongly prefer" if mode == "strict" else "Prefer"

    return f"""

## Snapshot Sufficiency

The cluster snapshot above was fetched {age_s}s ago and contains {pod_count} pods.
Health flags: issues={str(issues).lower()}, warnings={str(warnings).lower()}.

When the user asks a LIST-SHAPED, READ-ONLY question AND issues=false AND
warnings=false AND the snapshot is fresher than {fresh_threshold}s:
  - {bias_strength} answering directly from the snapshot.
  - Examples that qualify: "how many pods", "list namespaces", "is the cluster
    healthy", "show pods in default", "what's running".

ALWAYS fetch fresh data (regardless of the flags above) when:
  - The question mentions logs, metrics, history, "yesterday", "last N hours",
    "trend", or any time-windowed signal.
  - The question targets a SPECIFIC named pod/deployment/service for detail
    (use describe, get -o yaml, or logs).
  - You just performed a mutation (patch/apply/create/delete) — verify with a
    fresh get.
  - The question contains "now", "right now", "currently", "this second" — the
    user is asking explicitly about freshness.
  - The snapshot is older than {fresh_threshold}s.

If unsure, fetch. Stale answers are worse than redundant calls.
"""


# ── Matched-playbooks prompt block ────────────────────────────────────────────


def _playbooks_block(state: AgentState) -> str:
    """Render details of any playbooks whose triggers fired against the snapshot."""
    if not settings.PLAYBOOKS_ENABLED:
        return ""
    matched: list[str] = list(state.get("matched_playbooks") or [])
    if not matched:
        return ""

    try:
        from app.agent.playbooks import get_playbook
    except Exception:
        return ""

    sections: list[str] = ["\n## Recognized Failure Patterns\n"
                           "The snapshot matches these known patterns. Follow their\n"
                           "investigation steps before improvising.\n"]
    for name in matched:
        pb = get_playbook(name)
        if pb is None:
            continue
        steps = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(pb.investigation_steps))
        evidence = "\n".join(f"  - {e}" for e in pb.expected_evidence)
        sections.append(
            f"### {pb.name}\n"
            f"Investigation steps:\n{steps}\n"
            f"Look for:\n{evidence}\n"
            f"Fix template: {pb.recommended_fix_template}\n"
        )
    return "\n".join(sections)


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
    targeted_match = _TARGETED_RE.search(last) if not is_rca else None
    logger.debug(
        f"coordinator: LLM responded in {elapsed:.2f}s session={session_id} "
        f"decision={'RCA' if is_rca else 'TARGETED' if targeted_match else 'direct'}"
    )

    if targeted_match:
        ns = targeted_match.group(1).rstrip(",")
        pod = targeted_match.group(2).rstrip(",")
        issue = targeted_match.group(3).strip()
        logger.info(f"coordinator: TARGETED ns={ns} pod={pod} issue={issue!r} session={session_id}")
        await emit(session_id, StatusEvent(
            phase="investigating",
            message=f"Targeting {pod} in {ns}…",
            session_id=session_id,
        ))
        return {
            "targeted_investigation": {"namespace": ns, "pod": pod, "issue": issue},
            "rca_required": False,
        }

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
    session_id = state.get("session_id", "-")

    system_parts: list[str] = [_COORDINATOR_SYSTEM]
    if settings.INVESTIGATION_PLAN_ENABLED:
        system_parts.append(_PLAN_PROMPT_BLOCK)
    if memory_context:
        system_parts.append(f"\n\n## Cluster Context\n{memory_context}")
    if cluster_snapshot:
        system_parts.append(f"\n\n{cluster_snapshot}")
    snapshot_block = _snapshot_sufficiency_block(state)
    if snapshot_block:
        system_parts.append(snapshot_block)
    playbook_block = _playbooks_block(state)
    if playbook_block:
        system_parts.append(playbook_block)
    hitl_bypass = bool((config or {}).get("configurable", {}).get("hitl_bypass", False))
    if hitl_bypass:
        system_parts.append(_PROACTIVE_FIX_BLOCK)

    llm = get_coordinator_llm()
    agent = create_react_agent(llm, tools=ALL_TOOLS)

    history, history_summary = _trim_session_messages(list(state["messages"]))
    if history_summary:
        system_parts.append(f"\n\n{history_summary}")
    input_messages = [SystemMessage(content="\n".join(system_parts))] + history
    result = await agent.ainvoke({"messages": input_messages}, config=config)

    new_messages = result["messages"][len(input_messages):]
    new_messages = _trim_tool_messages(new_messages)  # A4: cap tool output before state storage

    update: dict = {"messages": new_messages}

    # Extract investigation plan, emit PlanEvent, and store on state.
    if settings.INVESTIGATION_PLAN_ENABLED:
        plan, new_messages = _extract_plan(new_messages)
        update["messages"] = new_messages
        if plan:
            update["investigation_plan"] = plan
            await emit(session_id, PlanEvent(
                steps=[s.model_dump() for s in plan],
                session_id=session_id,
            ))
            logger.info(
                f"investigation_plan_emitted session={session_id} step_count={len(plan)}"
            )

    tool_calls = sum(1 for m in new_messages if hasattr(m, "tool_calls") and m.tool_calls)
    logger.debug(
        f"coordinator: direct answer complete new_msgs={len(new_messages)} tool_calls={tool_calls}"
    )
    return update


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
        "rca_result": rca.model_dump(),  # plain dict avoids LangGraph msgpack serialization warning
        "rca_required": False,
        "messages": [AIMessage(content=summary)],
    }
