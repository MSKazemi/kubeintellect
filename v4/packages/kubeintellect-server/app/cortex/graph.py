"""Cortex V4 graph — explicit nodes, hand-rolled tool loop (ADR-001).

Topology:

    START → memory_loader → context_fetcher → triage
        triage(mode=chat)        → synthesize
        triage(mode=investigate) → gather_llm
    gather_llm  → gather_tools (when the model called tools) | synthesize
    gather_tools → gather_llm   (bounded loop)
    synthesize  → remember → END

Design points vs the V2 coordinator:
- The plan is FIRST-CLASS state: triage produces it as structured JSON, the
  tool loop advances `plan_cursor` per batch and emits PlanEvents with real
  transitions. No plan-block prose parsing, no post-hoc annotation.
- Routing is typed state, never sentinel strings in prose.
- Only the synthesis model streams; triage/specialist tiers have streaming
  disabled, so the session runner needs no token-buffer workaround.
- HITL: tools call interrupt() inside gather_tools; on resume LangGraph
  re-runs only that node, so no LLM call is replayed. Orphaned tool calls
  cannot occur — the executor always produces one ToolMessage per call.
"""
from __future__ import annotations

import json
import re
import time

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agent.nodes.context_fetcher import context_fetcher
from app.agent.nodes.memory_loader import memory_loader
from app.agent.state import AgentState, PlanStep
from app.core import metrics
from app.core.config import settings
from app.streaming.emitter import PlanEvent, StatusEvent, TokenEvent, emit
from app.answer_contract import PREMISE_CLAUSE
from app.tools.output_policy import (
    PARTIAL_CONTEXT_CLAUSE,
    RETRY_CLAUSE,
    TRUNCATION_CLAUSE,
    split_policy_lines,
    truncation_marker,
)
from app.tools.registry import ALL_TOOLS
from app.utils.logger import get_logger

logger = get_logger(__name__)

_TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}


class CortexState(AgentState):
    """AgentState + V4 cortex bookkeeping."""
    triage_mode: str          # chat | investigate
    plan_cursor: int
    gather_rounds: int
    turn_start_index: int     # messages[] length at turn start (for remember)
    turn_start_monotonic: float  # monotonic clock at turn start (P2 latency budget)


# ── Prompts ───────────────────────────────────────────────────────────────────

_TRIAGE_SYSTEM = """You are the triage tier of KubeIntellect, a Kubernetes operations agent.
Classify the user's request and produce an investigation plan.

Respond with ONLY a JSON object, no prose:
{"mode": "chat" | "investigate",
 "plan": ["step 1", "step 2", ...]}

mode=chat: ONLY greetings and purely conceptual questions about Kubernetes or
  KubeIntellect that do NOT concern this cluster's actual state
  (e.g. "hello", "what is a DaemonSet?", "how does HPA work?"). Do NOT pick chat
  merely because you could describe a command in words — if the user wants real
  data from the cluster, that is investigate.
mode=investigate: ANY request to observe, list, show, get, describe, check,
  count, summarize, diagnose, or change the actual cluster — including read-only
  queries such as "show all pods", "cluster health", "which pods are restarting".
  When in doubt, choose investigate.
The plan: 1-6 concrete steps (kubectl/PromQL/LogQL level); [] when mode=chat.
Use the cluster snapshot, matched playbooks, and memory below to make the
plan specific (name the namespaces/objects to inspect)."""

_GATHER_SYSTEM = """You are the investigation tier of KubeIntellect, executing a plan against a
Kubernetes cluster with the provided tools (kubectl, helm, Prometheus, Loki).

Rules:
- Follow the plan; call tools to gather evidence. Prefer parallel tool calls
  when steps are independent.
- Diagnose first. When the user asked you to fix/solve/resolve the issue,
  APPLY the fix with the tools (destructive commands go through an approval
  gate automatically) and then re-check the affected object to confirm the
  fix took effect. Do not stop at recommendations the user asked you to apply.
- NEVER ask the user for permission in text — execute via tools; the gate
  handles approval.
- If a tool replies that it is not configured or unavailable, do NOT retry
  it — work with what the other tools can provide. (The exact marker is named
  below.)
- When the evidence is sufficient (and any requested fix is applied), STOP
  calling tools and reply with the single word: EVIDENCE_COMPLETE

""" + TRUNCATION_CLAUSE + """

""" + RETRY_CLAUSE + """
"""

_SYNTHESIS_SYSTEM = """You are KubeIntellect's synthesis tier. Compose the final answer to the user
from the conversation and gathered evidence: findings first, root cause (if
diagnosed), what was done or recommended, and concrete next steps. Be precise
and cite the actual object names/namespaces/exit codes you observed. No tool
calls — answer only.

""" + PREMISE_CLAUSE + "\n\n" + TRUNCATION_CLAUSE

# Triage repair loop (#22): the triage tier answers in strict JSON; when the
# reply does not parse, we feed it back with a corrective hint and retry before
# falling back to the investigate default.
_TRIAGE_SNAPSHOT_MAX_CHARS = 3_000
_TRIAGE_MAX_PARSE_ATTEMPTS = 3
_TRIAGE_REPAIR_ECHO_MAX_CHARS = 2_000
_TRIAGE_REPAIR_HINT = (
    "Your previous response was not valid triage JSON. Reply with ONLY a JSON "
    'object matching the schema: {"mode": "chat" | "investigate", '
    '"plan": ["step 1", ...]}. No prose, no markdown fences.'
)


# ── Nodes ─────────────────────────────────────────────────────────────────────

async def triage(state: CortexState, config: RunnableConfig) -> dict:
    session_id = state["session_id"]
    await emit(session_id, StatusEvent(
        phase="analyzing", message="Triaging request…", session_id=session_id,
    ))

    context_parts = [PARTIAL_CONTEXT_CLAUSE, state.get("memory_context") or ""]
    snapshot = state.get("cluster_snapshot") or ""
    if snapshot:
        # `snapshot[:3000]` until 2026-08-24: the third cut in this codebase that shortened a
        # kubectl read and said nothing. The snapshot is filtered by the same namespace policy
        # `run_kubectl` applies, so its `[Protected] … withheld` sentence is in here — at the
        # end, where the cut lands. Triage decides chat-vs-investigate from this text; a
        # snapshot that reads as complete is the input that makes that decision confidently.
        body, policy = split_policy_lines(snapshot)
        shown = body[:_TRIAGE_SNAPSHOT_MAX_CHARS]
        note = ""
        if len(body) > _TRIAGE_SNAPSHOT_MAX_CHARS:
            note = "\n" + truncation_marker(
                len(body) - _TRIAGE_SNAPSHOT_MAX_CHARS,
                hint="cluster snapshot cut for the triage prompt",
            )
        tail = f"\n{policy}" if policy else ""
        context_parts.append(f"## Cluster snapshot\n{shown}{note}{tail}")
    playbooks = state.get("matched_playbooks") or []
    if playbooks:
        context_parts.append(f"## Matched playbooks\n{', '.join(playbooks)}")

    user_text = _last_user_text(state)
    prompt = [
        SystemMessage(content=_TRIAGE_SYSTEM + "\n\n" + "\n\n".join(p for p in context_parts if p)),
        HumanMessage(content=user_text),
    ]
    parsed = await _triage_with_repair(prompt, config)

    mode = parsed.get("mode", "investigate")
    steps = [
        PlanStep(description=str(s)[:200], status="pending")
        for s in (parsed.get("plan") or [])[:6]
    ]
    if mode == "investigate" and not steps:
        steps = [PlanStep(description=f"Investigate: {user_text[:150]}", status="pending")]

    if steps and settings.INVESTIGATION_PLAN_ENABLED:
        await emit(session_id, PlanEvent(
            steps=[s.model_dump() for s in steps], session_id=session_id,
        ))

    return {
        "triage_mode": mode,
        "investigation_plan": steps,
        "plan_cursor": 0,
        "gather_rounds": 0,
        "turn_start_index": len(state.get("messages", [])),
        "turn_start_monotonic": time.monotonic(),
    }


async def _triage_with_repair(
    prompt: list[BaseMessage], config: RunnableConfig
) -> dict:
    """Invoke the triage tier with a bounded repair loop (#22).

    The triage tier answers in strict JSON. When the reply does not parse,
    feed it back with a corrective hint and retry — up to
    _TRIAGE_MAX_PARSE_ATTEMPTS total calls — before falling back to the
    investigate default, so one malformed reply no longer silently discards
    the request.
    """
    from app.cortex.models import get_triage_llm

    for attempt in range(1, _TRIAGE_MAX_PARSE_ATTEMPTS + 1):
        try:
            reply = await get_triage_llm().ainvoke(prompt, config)
        except Exception as exc:
            logger.warning(
                f"cortex.triage attempt {attempt} failed ({exc}) — defaulting to investigate"
            )
            return {"mode": "investigate", "plan": []}
        parsed = _parse_triage_json_strict(str(reply.content))
        if parsed is not None:
            return parsed
        if attempt == _TRIAGE_MAX_PARSE_ATTEMPTS:
            break
        logger.warning(
            f"cortex.triage attempt {attempt} returned unparseable JSON — retrying with a repair hint"
        )
        prompt = [
            *prompt,
            # Echo back only enough of the bad reply to make the correction
            # concrete. Unbounded, a pathological reply would be re-sent on
            # every remaining attempt and could exhaust the context window.
            AIMessage(content=str(reply.content)[:_TRIAGE_REPAIR_ECHO_MAX_CHARS]),
            HumanMessage(content=_TRIAGE_REPAIR_HINT),
        ]
    logger.warning(
        f"cortex.triage exhausted {_TRIAGE_MAX_PARSE_ATTEMPTS} attempts — defaulting to investigate"
    )
    return {"mode": "investigate", "plan": []}


async def gather_once(state: CortexState, config: RunnableConfig) -> dict:
    """One flat gather round: bind every tool, invoke the specialist LLM, return
    its reply. This is the v4 gather core; the ADR-101 harness runner reuses it so
    the fan-out seam is parity-by-construction with the flat path."""
    from app.cortex.models import get_specialist_llm

    session_id = state["session_id"]
    await emit(session_id, StatusEvent(
        phase="investigating", message="Gathering evidence…", session_id=session_id,
    ))

    plan_text = "\n".join(
        f"{i + 1}. [{s.status}] {s.description}"
        for i, s in enumerate(state.get("investigation_plan") or [])
    )
    system = _GATHER_SYSTEM
    if state.get("memory_context"):
        system += f"\n\n## Memory\n{state['memory_context']}"
    if plan_text:
        system += f"\n\n## Plan\n{plan_text}"
    # Runbooks-as-skills (P2): inject only the matched playbooks' diagnostic sequences. Default-off.
    if settings.CORTEX_V5_ENABLED and settings.KI_V5_RUNBOOK_SKILLS and state.get("matched_playbooks"):
        from app.cortex.skills import render_matched_skills
        skills = render_matched_skills(list(state["matched_playbooks"]))
        if skills:
            system += f"\n\n{skills}"
    # Change-first RCA (P2): rank recent changes as the search prior. No-op until the P1 change
    # ledger is populated (empty source ⇒ empty block ⇒ prompt unchanged). Default-off.
    if settings.CORTEX_V5_ENABLED and settings.KI_V5_CHANGE_FIRST_RCA:
        from app.cluster_id import get_cluster_id
        from app.cortex.change_rca import recent_changes, render_change_prior
        # Resolve the id the same way the *write* side does (the ledger append below). These
        # two disagreed: the append fell back to `get_cluster_id()` and the read fell back to
        # `""`, a key nothing is ever recorded under — so on any state without a cluster_id
        # (`watchdog_dispatch` builds exactly that, `cluster_id: ""`) the ledger held the
        # change and the prior came back empty, which renders as no block at all.
        cluster_id = state.get("cluster_id") or get_cluster_id()
        prior = render_change_prior(recent_changes(cluster_id))
        if prior:
            system += f"\n\n{prior}"

    llm = get_specialist_llm().bind_tools(ALL_TOOLS)
    messages = [SystemMessage(content=system), *state["messages"]]
    reply = await llm.ainvoke(messages, config)
    return {"messages": [reply], "gather_rounds": state.get("gather_rounds", 0) + 1}


async def gather_llm(state: CortexState, config: RunnableConfig) -> dict:
    """Gather node. Default: the v4 flat single-context round (`gather_once`).
    With the ADR-101 harness flag on, dispatch the read-only investigation fan-out
    runner instead (R-p0-06 / AC-04). Flag off ⇒ byte-identical v4 behavior."""
    if settings.CORTEX_V5_ENABLED and settings.KI_V5_HARNESS_FANOUT:
        from app.cortex.harness.runner import run_fanout
        return await run_fanout(state, config)
    return await gather_once(state, config)


def _bound_tool_content(content: str) -> str:
    """Bound a tool result before it enters context.

    Default (v4): silent mid-line chop at 8000 chars. With the ADR-101 harness
    flag on: the never-silent, line-aligned ≤2k-token summary bound — same char
    budget, but cut on a line boundary and stamped with an explicit truncation
    marker so the model is never handed a silently clipped result.

    **Whichever bound applies, the tool's own policy lines survive it** — fixed 2026-08-24.
    `run_kubectl` caps itself at 8 000 chars and appends `[truncated: N chars omitted …]`
    *after* that cap, so it returns 8 173 chars and `content[:8000]` deleted the notice with
    probability 1, on every single over-cap listing; the same cut takes the `[Protected] …
    withheld` sentence off a filtered one. The chop of the *body* stays silent here, because
    v4's silence is what the ADR-101 flag exists to change — but a sentence the tool already
    wrote is not this function's to destroy, on either side of the flag.
    """
    body, policy = split_policy_lines(content)
    if settings.CORTEX_V5_ENABLED and settings.KI_V5_HARNESS_FANOUT:
        from app.cortex.harness import bound_summary
        bounded = bound_summary(body)[0]
    else:
        bounded = body[:8000]
    return f"{bounded}\n{policy}" if policy else bounded


async def gather_tools(state: CortexState, config: RunnableConfig) -> dict:
    """Hand-rolled tool executor: parallel calls, exact ToolMessages, plan
    cursor advance. HITL interrupts fire inside tool coroutines; on resume
    LangGraph re-runs only this node."""
    session_id = state["session_id"]
    last = state["messages"][-1]
    tool_calls = getattr(last, "tool_calls", None) or []

    # Which calls in this batch produced no usable result. `_run_one` deliberately turns a
    # tool exception into an ordinary ToolMessage so the model can react to it — which means
    # the batch returns normally whether every tool worked or every tool failed, and the plan
    # transition below cannot tell those apart without being told.
    failures: list[str] = []

    async def _run_one(tc: dict) -> ToolMessage:
        name = tc.get("name", "")
        args = tc.get("args") or {}
        tool = _TOOLS_BY_NAME.get(name)
        if tool is None:
            failures.append(f"{name or '<unnamed>'}: unknown tool")
            metrics.record_tool_call(name, "unknown_tool")
            return ToolMessage(tool_call_id=tc.get("id", ""), name=name,
                               content=f"Unknown tool: {name}")
        started = time.perf_counter()
        try:
            result = await tool.ainvoke(args, config)
            content = result.content if hasattr(result, "content") else str(result)
            metrics.record_tool_call(name, "ok", time.perf_counter() - started)
        except Exception as exc:
            # GraphInterrupt must propagate for HITL; everything else becomes
            # an error ToolMessage the model can react to.
            from langgraph.errors import GraphInterrupt
            if isinstance(exc, GraphInterrupt):
                # Counted as its own outcome, never as an error: an approval gate stopping a
                # destructive action is the product working, and folding it into the error rate
                # would make the safety feature look like a defect on every dashboard.
                metrics.record_hitl_interrupt(name)
                raise
            metrics.record_tool_call(name, "error", time.perf_counter() - started)
            failures.append(f"{name}: {exc}")
            content = f"Tool error: {exc}"
        return ToolMessage(tool_call_id=tc.get("id", ""), name=name, content=_bound_tool_content(str(content)))

    # Responsiveness (P2): tool execution is the slowest, most silent phase — emit a progress
    # heartbeat while it runs so the SSE stream never goes quiet (REQ-developer-19). No-op off.
    from contextlib import AsyncExitStack
    results = []
    async with AsyncExitStack() as stack:
        if settings.CORTEX_V5_ENABLED and settings.KI_V5_RESPONSIVENESS:
            from app.cortex.responsiveness import heartbeat
            await stack.enter_async_context(heartbeat(
                session_id, "investigating", "Still gathering evidence…",
                interval=settings.KI_V5_HEARTBEAT_SECONDS,
            ))
        for tc in tool_calls:
            results.append(await _run_one(tc))  # sequential: keeps HITL resume deterministic

    # Plan transition: one tool batch advances the cursor.
    plan = list(state.get("investigation_plan") or [])
    cursor = state.get("plan_cursor", 0)
    if cursor < len(plan):
        # "done" is a claim that the step was carried out. A batch in which every tool errored
        # carried nothing out — and the CLI renders "done" as a green ✓, so the investigation
        # looked like it was progressing while it gathered nothing. The cursor still advances:
        # the step is finished either way, and a plan that never advances would hang the UI.
        step_status = "failed" if tool_calls and len(failures) == len(tool_calls) else "done"
        if failures:
            logger.warning(
                f"cortex.gather_tools: {len(failures)} of {len(tool_calls)} tool call(s) "
                f"failed for plan step {cursor} — marking it {step_status!r} "
                f"[{'; '.join(failures[:5])}"
                + (f" (+{len(failures) - 5} more)]" if len(failures) > 5 else "]")
            )
        plan[cursor] = plan[cursor].model_copy(update={"status": step_status})
        cursor += 1
        if cursor < len(plan):
            plan[cursor] = plan[cursor].model_copy(update={"status": "in_progress"})
        await emit(session_id, PlanEvent(
            steps=[s.model_dump() for s in plan], session_id=session_id,
        ))

    return {"messages": results, "investigation_plan": plan, "plan_cursor": cursor}


async def synthesize(state: CortexState, config: RunnableConfig) -> dict:
    from app.cortex.models import get_synthesis_llm

    session_id = state["session_id"]
    await emit(session_id, StatusEvent(
        phase="synthesizing", message="Composing answer…", session_id=session_id,
    ))

    # Round-budget boundary: if the loop ended while the model still wanted
    # tools, the dangling tool_calls would 400 the provider — close them.
    closers: list[ToolMessage] = []
    last = state["messages"][-1] if state.get("messages") else None
    for tc in (getattr(last, "tool_calls", None) or []):
        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if tc_id:
            closers.append(ToolMessage(
                tool_call_id=tc_id,
                content="Tool budget exhausted — answer with the evidence gathered so far.",
            ))

    # Close out the plan: anything not done is skipped.
    plan = list(state.get("investigation_plan") or [])
    changed = False
    for i, step in enumerate(plan):
        if step.status in ("pending", "in_progress"):
            plan[i] = step.model_copy(update={"status": "skipped"})
            changed = True
    if plan and changed:
        await emit(session_id, PlanEvent(
            steps=[s.model_dump() for s in plan], session_id=session_id,
        ))

    messages: list[BaseMessage] = [SystemMessage(content=_SYNTHESIS_SYSTEM)]
    # Ground the final answer in the raw cluster snapshot when the fan-out is on: synthesize would
    # otherwise see only the subagents' reconciled evidence, so a subagent error (e.g. a false
    # "not found") could propagate into the answer. The snapshot is ground truth from the cluster.
    if settings.CORTEX_V5_ENABLED and settings.KI_V5_HARNESS_FANOUT and state.get("cluster_snapshot"):
        messages.append(SystemMessage(
            content="## Current cluster snapshot (ground truth — trust this over any claim that a "
                    f"resource is missing)\n{state['cluster_snapshot']}"))
    messages += [*state["messages"], *closers]
    llm = get_synthesis_llm()
    chunks: list[str] = []
    async for chunk in llm.astream(messages, config):
        text = chunk.content if isinstance(chunk.content, str) else ""
        if text:
            chunks.append(text)
            await emit(session_id, TokenEvent(content=text, session_id=session_id))
    answer = "".join(chunks)

    # Verification ladder, read side (P2): adversarially review the RCA against the evidence and
    # stream a calibrated caveat if it flags unsupported claims. Fails open; default-off.
    if settings.CORTEX_V5_ENABLED and settings.KI_V5_VERIFY_LADDER:
        from app.cortex.verify import render_review_note, review_rca
        await emit(session_id, StatusEvent(
            phase="verifying", message="Reviewing conclusion…", session_id=session_id,
        ))
        review = await review_rca(answer, _gathered_evidence(state))
        note = render_review_note(review)
        if note:
            await emit(session_id, TokenEvent(content=note, session_id=session_id))
            answer += note

    # Escalation-avoidance brief (P2): for an investigation, append a responder-skill-calibrated
    # brief with explicit escalate-only-if bounds. Fails safe; default-off; investigate-mode only.
    if (settings.CORTEX_V5_ENABLED and settings.KI_V5_ESCALATION_BRIEFS
            and state.get("triage_mode") == "investigate"):
        from app.cortex.briefs import build_brief, render_brief
        brief = await build_brief(
            answer, _gathered_evidence(state), responder_level=settings.KI_V5_RESPONDER_LEVEL,
        )
        rendered = render_brief(brief)
        await emit(session_id, TokenEvent(content=rendered, session_id=session_id))
        answer += rendered

    # Responsiveness (P2): surface a latency-budget breach so a slow investigation is visible
    # (REQ-developer-19). Default-off; a warning only, never blocks the answer.
    if (settings.CORTEX_V5_ENABLED and settings.KI_V5_RESPONSIVENESS
            and state.get("turn_start_monotonic")):
        from app.cortex.responsiveness import PhaseBudget
        budget = PhaseBudget.since(
            state["turn_start_monotonic"],
            first_signal_s=settings.KI_V5_FIRST_SIGNAL_BUDGET_S,
            full_s=settings.KI_V5_FULL_BUDGET_S,
        )
        if budget.full_breached():
            await emit(session_id, StatusEvent(
                phase="synthesizing", message=f"⏱ Over latency budget: {budget.warning()}",
                session_id=session_id,
            ))

    return {
        "messages": [*closers, AIMessage(content=answer)],
        "investigation_plan": plan,
    }


_EVIDENCE_BUDGET = 8000


def _gathered_evidence(state: CortexState) -> str:
    """Concatenate this turn's evidence (tool + fan-out message text) for the reviewer, bounded.

    The bound is never silent. This text is the *entire* world of the adversarial reviewer —
    it is given the claim and this, and asked which statements the evidence does not support,
    with a standing instruction to treat "not found" conclusions with suspicion. A silent
    `[:8000]` therefore does not merely lose evidence, it manufactures the reviewer's grounds
    for objecting: measured 2026-08-24 on a six-read gather, the decisive
    `Reason: OOMKilled / Limits: memory 128Mi` lines fell 749 characters past the cut, and what
    the reviewer received ended mid-row at `web-022   1/1` — a partial line that reads as a
    complete one. Nothing in the text said any of it was missing.

    The cut is line-aligned and stamped, in the same terms `run_kubectl` uses for partial tool
    output: absence from this text is not evidence.
    """
    start = state.get("turn_start_index", 0)
    parts = []
    for msg in (state.get("messages") or [])[start:]:
        content = getattr(msg, "content", "")
        if isinstance(content, str) and content.strip():
            parts.append(content)
    joined = "\n\n".join(parts)
    if len(joined) <= _EVIDENCE_BUDGET:
        return joined
    cut = joined[:_EVIDENCE_BUDGET]
    nl = cut.rfind("\n")
    if nl > _EVIDENCE_BUDGET // 2:  # only prefer a line break if it is not wastefully early
        cut = cut[:nl]
    dropped = len(joined) - len(cut)
    return cut.rstrip() + (
        f"\n\n…[TRUNCATED: {dropped} of {len(joined)} characters of gathered evidence are NOT "
        f"included above, and what was dropped is the MOST RECENT evidence of this turn. "
        f"Absence of a fact from this text is NOT evidence that it was not observed.]"
    )


async def remember(state: CortexState, config: RunnableConfig) -> dict:
    """Reflexion + episode write — reuses the battle-tested V2 outcome path.

    Mutating turns go through _maybe_record_direct_outcome (R2–R8 semantics,
    cluster verification, pattern promotion). Read-only investigations still
    deserve recall later — they get a lightweight report_only episode.
    """
    try:
        from app.agent.nodes.coordinator import (
            _maybe_record_direct_outcome,
            _ran_mutation,
        )
        turn_messages = list(state["messages"])[state.get("turn_start_index", 0):]
        mutated, mut_cmds = _ran_mutation(turn_messages)
        if mutated:
            _maybe_record_direct_outcome(state, turn_messages)
            # Change ledger (P1): record the mutations we just applied so change-first RCA can
            # rank them as the search prior on a later investigation. Default-off.
            if settings.CORTEX_V5_ENABLED and settings.KI_V5_CHANGE_LEDGER and mut_cmds:
                from app.cluster_id import get_cluster_id
                from app.memory.change_ledger import record_from_commands
                cid = state.get("cluster_id") or get_cluster_id()
                record_from_commands(cid, mut_cmds, time.time())
        elif state.get("triage_mode") == "investigate":
            import asyncio as _asyncio

            from app.cluster_id import get_cluster_id
            from app.memory.episodes import write_episode

            answer = ""
            last = turn_messages[-1] if turn_messages else None
            if last is not None and isinstance(last.content, str):
                answer = last.content
            cluster_id = state.get("cluster_id") or get_cluster_id()
            _asyncio.create_task(write_episode(
                cluster_id=cluster_id,
                # Provenance drives the memory write-admission trust score, so it is read
                # from the state field only an in-process caller can set — never from
                # `user_id`, which is `body.user` and free for any chat client to choose.
                trigger_kind=(
                    "detector" if state.get("trigger_source") == "detector" else "user_query"
                ),
                trigger_detail=_last_user_text(state)[:300],
                summary=answer[:1200],
                outcome="report_only",
                playbooks=list(state.get("matched_playbooks") or []),
                created_by_role=state.get("user_role"),
                request_id=state.get("session_id"),
            ))
            # Investigation write-back (P2): reinforce the topology from what this turn observed.
            if settings.CORTEX_V5_ENABLED and settings.KI_V5_INVESTIGATION_WRITEBACK:
                from app.memory.writeback import (
                    apply_writeback,
                    signals_from_investigation,
                )
                signals = signals_from_investigation(
                    cluster_id, list(state.get("matched_playbooks") or []))
                if signals:
                    _asyncio.create_task(apply_writeback(cluster_id, signals))
    except Exception as exc:
        logger.warning(f"cortex.remember failed (non-fatal): {exc}")
    return {}


# ── Routing ───────────────────────────────────────────────────────────────────

def route_triage(state: CortexState) -> str:
    return "synthesize" if state.get("triage_mode") == "chat" else "gather_llm"


def route_gather(state: CortexState) -> str:
    last = state["messages"][-1]
    has_calls = bool(getattr(last, "tool_calls", None))
    if has_calls and state.get("gather_rounds", 0) <= settings.CORTEX_MAX_GATHER_ROUNDS:
        return "gather_tools"
    return "synthesize"


def build_cortex_graph() -> StateGraph:
    builder = StateGraph(CortexState)
    builder.add_node("memory_loader", memory_loader)
    builder.add_node("context_fetcher", context_fetcher)
    builder.add_node("triage", triage)
    builder.add_node("gather_llm", gather_llm)
    builder.add_node("gather_tools", gather_tools)
    builder.add_node("synthesize", synthesize)
    builder.add_node("remember", remember)

    builder.add_edge(START, "memory_loader")
    builder.add_edge("memory_loader", "context_fetcher")
    builder.add_edge("context_fetcher", "triage")
    builder.add_conditional_edges("triage", route_triage)
    builder.add_conditional_edges("gather_llm", route_gather)
    builder.add_edge("gather_tools", "gather_llm")
    builder.add_edge("synthesize", "remember")
    builder.add_edge("remember", END)
    return builder


# ── Helpers ───────────────────────────────────────────────────────────────────

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_triage_json_strict(text: str) -> dict | None:
    """Parse a triage reply into a validated plan dict, or None when the
    reply is not usable triage JSON (no JSON block, malformed JSON, an
    unrecognised mode, or a `plan` that is not a list). The repair loop uses
    this to tell "bad reply" apart from "investigate" as a deliberate answer.

    `plan` is type-checked here rather than at the call site because that is
    where a bad value does its damage: `triage` does
    `(parsed.get("plan") or [])[:6]`, so a *string* plan slices into six
    single characters and emits six one-character PlanSteps. Returning None
    instead sends it back through the repair loop, which is what the caller
    wants for every other malformed field.
    """
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("mode") not in ("chat", "investigate"):
        return None
    plan = data.get("plan")
    if plan is not None and not isinstance(plan, list):
        return None
    return data


def _last_user_text(state) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage) and isinstance(message.content, str):
            return message.content
    return ""
