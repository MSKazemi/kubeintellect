"""
KubeIntellect V2 LangGraph workflow.

Graph shape:
  START → memory_loader → context_fetcher → coordinator
                                                 │
                               ┌─────────────────┴──────────────────────────┐
                               │ rca_required=True                            │ direct answer
                               ▼                                              ▼
               [Send x4] → subagent_executor (parallel)                     END
                                       ↓ (all 4 complete, fan-in)
                                   coordinator  (synthesis)
                                       ↓
                                     END

Fan-out is driven by route_coordinator returning list[Send] — NOT by the
coordinator node itself.  The coordinator always returns a plain dict; it
sets rca_required=True as a signal and route_coordinator acts on it.
"""
from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from app.agent.hitl import is_auto_approve_request as _is_auto_approve_request
from app.agent.hitl import is_approval as _is_approval
from app.agent.hitl import is_denial as _is_denial
from app.agent.nodes.context_fetcher import context_fetcher
from app.agent.nodes.coordinator import coordinator
from app.agent.nodes.memory_loader import memory_loader
from app.agent.nodes.subagent import run_subagent
from app.agent.state import AgentFinding, AgentState, SubagentInput
from app.core.config import settings
from app.core.llm import get_langfuse_callbacks, get_langfuse_run_metadata
from app.streaming.emitter import (
    ErrorEvent,
    HitlRequestEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    close_session,
    emit,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

_RCA_DOMAINS = ("pod", "metrics", "logs", "events")


# ── Subagent executor node ────────────────────────────────────────────────────


async def subagent_executor(payload: SubagentInput) -> dict:
    """LangGraph node that wraps run_subagent and accumulates findings."""
    session_id = payload["session_id"]
    domain = payload["domain"]
    await emit(session_id, StatusEvent(
        phase="investigating",
        message=f"Running {domain} diagnostics…",
        session_id=session_id,
    ))
    finding: AgentFinding = await run_subagent(payload)
    return {"findings": [finding]}


# ── Targeted investigator node ────────────────────────────────────────────────


async def targeted_investigator(state: AgentState) -> dict:
    """Run 3 parallel targeted reads for a single-resource issue, then return to coordinator."""
    from app.agent.nodes.context_fetcher import _run_kubectl_snapshot
    from app.tools.namespace_guard import protected_message

    info = state.get("targeted_investigation") or {}
    ns = info.get("namespace", "")
    pod = info.get("pod", "")
    issue = info.get("issue", "")
    session_id = state.get("session_id", "-")

    # `ns` and `pod` are `(\S+?)` captures out of a line the model wrote, and they are spliced
    # straight into an argv. `_kubectl_snapshot` refuses a blocked namespace itself, but a
    # refusal rendered inside a fence headed "### Pod Description" reads as a description; say
    # plainly that the read did not happen, and skip three subprocesses that cannot return.
    if ns.strip().lower() in settings.kubectl_blocked_namespaces:
        logger.warning(f"targeted_investigator: refused protected namespace {ns!r} session={session_id}")
        existing_snapshot = state.get("cluster_snapshot", "")
        refusal = (
            f"## Targeted Investigation: {pod} in {ns}\n"
            f"{protected_message(ns)}\n\n"
            "No pod description, events or deployments were read. Tell the user this "
            "namespace is protected; do not infer its contents from anything above."
        )
        return {
            "cluster_snapshot": f"{existing_snapshot}\n\n{refusal}" if existing_snapshot else refusal,
            "targeted_investigation": None,
        }

    await emit(session_id, StatusEvent(
        phase="investigating",
        message=f"Investigating {pod} in {ns}…",
        session_id=session_id,
    ))

    describe_out, events_out, deploy_out = await asyncio.gather(
        asyncio.to_thread(_run_kubectl_snapshot, ["describe", "pod", pod, "-n", ns]),
        asyncio.to_thread(_run_kubectl_snapshot, ["get", "events", "-n", ns, "--sort-by=.lastTimestamp"]),
        asyncio.to_thread(_run_kubectl_snapshot, ["get", "deployments", "-n", ns]),
    )

    detail = (
        f"## Targeted Investigation: {pod} in {ns}\n"
        f"**Issue**: {issue}\n\n"
        f"### Pod Description\n```\n{describe_out}\n```\n\n"
        f"### Namespace Events\n```\n{events_out}\n```\n\n"
        f"### Deployments\n```\n{deploy_out}\n```"
    )

    existing = state.get("cluster_snapshot", "")
    updated_snapshot = f"{existing}\n\n{detail}" if existing else detail

    logger.debug(
        f"targeted_investigator: detail={len(detail)} chars pod={pod} session={session_id}"
    )
    return {
        "cluster_snapshot": updated_snapshot,
        "targeted_investigation": None,
    }


# ── Routing function ──────────────────────────────────────────────────────────


def route_coordinator(state: AgentState) -> str | list[Send]:
    """
    Conditional edge after coordinator.

    - targeted_investigation set → run parallel targeted reads, return to coordinator.
    - rca_required=True          → fan-out: return list[Send] to 4 subagent_executor nodes.
    - rca_result is set          → synthesis done, go to END.
    - findings present           → subagents finished, route back to coordinator for synthesis.
    - otherwise                  → direct answer completed, go to END.

    Returning list[Send] is LangGraph's fan-out mechanism; it bypasses the
    string-based path_map and dispatches directly to the target node.
    """
    if state.get("targeted_investigation"):
        return "targeted_investigator"

    if state.get("rca_required"):
        session_id = state.get("session_id", "-")
        logger.info(f"route_coordinator: fanning out to {len(_RCA_DOMAINS)} subagents session={session_id}")

        # Pass only the current investigation query to each subagent.
        # Subagents must NOT inherit the full session history — it bloats their
        # context and causes the LLM to respond in prose instead of JSON.
        current_query = next(
            (m for m in reversed(state["messages"]) if hasattr(m, "type") and m.type == "human"),
            None,
        )
        subagent_messages = [current_query] if current_query else state["messages"][-1:]

        return [
            Send(
                "subagent_executor",
                SubagentInput(
                    domain=domain,
                    session_id=state["session_id"],
                    user_id=state["user_id"],
                    user_role=state.get("user_role", "admin"),
                    messages=subagent_messages,
                    memory_context=state.get("memory_context", ""),
                    evidence_bundle=state.get("cluster_snapshot", ""),
                ),
            )
            for domain in _RCA_DOMAINS
        ]

    if state.get("rca_result") is not None:
        return END

    if state.get("findings"):
        # Subagents wrote findings but coordinator hasn't synthesized yet.
        return "coordinator"

    return END


# ── Graph builder ─────────────────────────────────────────────────────────────


def build_graph() -> StateGraph:
    builder = StateGraph(AgentState)

    builder.add_node("memory_loader", memory_loader)
    builder.add_node("context_fetcher", context_fetcher)
    builder.add_node("coordinator", coordinator)
    builder.add_node("targeted_investigator", targeted_investigator)
    # subagent_executor is only ever reached through a Send (route_coordinator), so its
    # input is SubagentInput rather than AgentState. LangGraph's add_node signature has
    # no way to express a Send-only node, so it cannot type-check this edge.
    builder.add_node("subagent_executor", subagent_executor)  # type: ignore[arg-type]

    builder.add_edge(START, "memory_loader")
    builder.add_edge("memory_loader", "context_fetcher")
    builder.add_edge("context_fetcher", "coordinator")

    # No path_map: route_coordinator may return a string, END, or list[Send].
    # LangGraph handles list[Send] as fan-out commands directly without consulting
    # a path_map, so we omit the mapping to avoid spurious routing constraints.
    builder.add_conditional_edges("coordinator", route_coordinator)

    # Targeted investigator runs parallel reads then returns to coordinator for final answer.
    builder.add_edge("targeted_investigator", "coordinator")

    # All subagent branches feed back into coordinator for synthesis (fan-in).
    # LangGraph waits for all parallel Send branches before running coordinator.
    builder.add_edge("subagent_executor", "coordinator")

    return builder


# ── Compiled graph (singleton with checkpointer) ──────────────────────────────

_graph: Any = None
# The context manager (holds the connection). Either checkpointer backend can land
# here, so it is typed by the shared base rather than by whichever one is compiled in.
_checkpointer_cm: AbstractAsyncContextManager[BaseCheckpointSaver[Any]] | None = None
_checkpointer: BaseCheckpointSaver[Any] | None = None
_graph_lock = asyncio.Lock()


async def init_graph() -> None:
    """Build and compile the graph. Call once at app startup."""
    global _graph, _checkpointer_cm, _checkpointer
    async with _graph_lock:
        if _graph is not None:
            return
        if settings.CORTEX_V4_ENABLED:
            from app.cortex.graph import build_cortex_graph
            logger.info("CORTEX_V4_ENABLED — building the V4 explicit-node graph")
            builder = build_cortex_graph()
        else:
            builder = build_graph()
        if settings.USE_SQLITE:
            from pathlib import Path

            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            db_path = str(Path(settings.SQLITE_PATH).expanduser())
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"Building LangGraph workflow with AsyncSqliteSaver ({db_path})")
            _checkpointer_cm = AsyncSqliteSaver.from_conn_string(db_path)
        else:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            logger.info("Building LangGraph workflow with AsyncPostgresSaver")
            dsn = settings.POSTGRES_DSN
            if not dsn:
                # Unreachable while POSTGRES_DSN only returns None under USE_SQLITE,
                # which this branch has already excluded — kept so a future change to
                # that property fails loudly instead of inside the driver.
                raise RuntimeError(
                    "POSTGRES_DSN resolved to None with USE_SQLITE=false. Set DATABASE_URL "
                    "or the POSTGRES_* settings, or run with USE_SQLITE=true."
                )
            _checkpointer_cm = AsyncPostgresSaver.from_conn_string(dsn)
        _checkpointer = await _checkpointer_cm.__aenter__()
        await _checkpointer.setup()
        _graph = builder.compile(checkpointer=_checkpointer)
        logger.info("LangGraph workflow ready")


async def close_graph() -> None:
    """Cleanly close the checkpointer connection. Call at app shutdown."""
    global _graph, _checkpointer_cm, _checkpointer
    if _checkpointer_cm is not None:
        await _checkpointer_cm.__aexit__(None, None, None)
        _checkpointer_cm = None
        _checkpointer = None
        _graph = None


async def get_graph():
    """Return the compiled graph (must call init_graph first)."""
    if _graph is None:
        await init_graph()
    return _graph


# ── Shared initial-state helper ───────────────────────────────────────────────


def _fresh_turn_state(
    user_message: str,
    session_id: str,
    user_id: str,
    user_role: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the per-turn state update that resets transient RCA fields.

    Note: findings is intentionally omitted here.  memory_loader resets it via
    _findings_reducer(None) at node entry.  Including findings=[] in this dict
    would be a no-op because operator.add(existing, []) == existing.
    """
    return {
        "messages": [HumanMessage(content=user_message)],
        "session_id": session_id,
        "user_id": user_id,
        "user_role": user_role,
        "memory_context": "",
        "cluster_snapshot": "",
        "rca_required": False,
        "rca_result": None,
        "targeted_investigation": None,
        "pending_hitl": None,
        # Snapshot health flags (re-populated by context_fetcher each turn)
        "snapshot_has_issues": False,
        "snapshot_has_warnings": False,
        "snapshot_pod_count": 0,
        "snapshot_read_failed": False,
        "snapshot_built_at": 0.0,
        "investigation_plan": None,
        # Matched playbooks (re-populated by context_fetcher each turn)
        "matched_playbooks": [],
        # Cluster identity (re-populated by context_fetcher each turn — cheap, cached)
        "cluster_id": "unknown",
        **(extra or {}),
    }


# Shown to the operator when the OUTER graph hits its budget — i.e. the
# coordinator↔investigation cycle failed to converge. Distinct from the coordinator's
# own tool-call budget message: this one means the turn as a whole did not settle.
GRAPH_BUDGET_EXHAUSTED_MESSAGE = (
    "I stopped because this turn hit its overall step budget ({limit} recursion units) "
    "without settling on an answer.\n\n"
    "Everything produced before this point is above, and no further action was taken. "
    "This usually means the investigation kept re-opening instead of converging.\n\n"
    "You can: ask a narrower question, or raise `AGENT_GRAPH_RECURSION_LIMIT` if this "
    "genuinely needs more cycles."
)


# ── Public invoke helpers ──────────────────────────────────────────────────────


async def invoke(
    user_message: str,
    session_id: str,
    user_id: str = "default",
    user_role: str = "admin",
    extra_state: dict[str, Any] | None = None,
) -> AgentState:
    """Single-turn invoke (non-streaming). Returns final state."""
    graph = await get_graph()
    config: RunnableConfig = {
        "configurable": {"thread_id": session_id, "user_role": user_role},
        # Explicit runaway backstop — see settings.AGENT_GRAPH_RECURSION_LIMIT.
        # LangGraph's default is 10007, which is not a bound in any useful sense.
        "recursion_limit": settings.AGENT_GRAPH_RECURSION_LIMIT,
    }

    state = _fresh_turn_state(user_message, session_id, user_id, user_role, extra_state)

    callbacks = get_langfuse_callbacks()
    if callbacks:
        config["callbacks"] = callbacks
        config["metadata"] = get_langfuse_run_metadata(session_id)
    try:
        result = await graph.ainvoke(state, config=config)
    except GraphRecursionError:
        # Halt and escalate with the last checkpointed state rather than losing the
        # whole turn to an uncaught exception. The caller still sees every message
        # produced before the budget ran out, plus an explicit escalation.
        logger.error(
            f"invoke: graph budget exhausted session={session_id} "
            f"limit={settings.AGENT_GRAPH_RECURSION_LIMIT} — returning partial state",
            extra={"session_id": session_id},
        )
        snapshot = await graph.aget_state(config)
        partial = cast(
            AgentState,
            dict(snapshot.values) if snapshot and snapshot.values else state,
        )
        partial["messages"] = list(partial.get("messages", [])) + [
            AIMessage(content=GRAPH_BUDGET_EXHAUSTED_MESSAGE.format(
                limit=settings.AGENT_GRAPH_RECURSION_LIMIT,
            ))
        ]
        return partial
    return result


async def stream_events(
    user_message: str,
    session_id: str,
    user_id: str = "default",
    user_role: str = "admin",
    auto_approve: bool = False,
):
    """Async generator yielding LangGraph astream_events for SSE.

    If the thread has a pending HITL interrupt, user_message is interpreted
    as an approval/denial and the graph is resumed via Command(resume=...).
    Otherwise a fresh turn is started.

    auto_approve=True skips all HITL interrupt gates — useful for testing
    and trusted automation. The flag is passed via configurable so kubectl_tool
    can read it without touching AgentState.
    """
    graph = await get_graph()
    config: RunnableConfig = {
        "configurable": {"thread_id": session_id, "user_role": user_role, "hitl_bypass": auto_approve},
        # Explicit runaway backstop — see settings.AGENT_GRAPH_RECURSION_LIMIT.
        "recursion_limit": settings.AGENT_GRAPH_RECURSION_LIMIT,
    }

    # "approve all" bypasses HITL for **this turn only**, not for the session.
    #
    # Nothing persists it: `hitl_bypass` is rebuilt from `auto_approve` on every call, and
    # `auto_approve` comes from the request body (`kq --auto-approve`) or from the current
    # message. The next turn starts gated again, and the `kq` REPL does not latch it either.
    # Measured 2026-08-20 — turn 1 "approve all" -> True, turns 2 and 3 -> False — after four
    # doc surfaces and this log line had said "for the rest of the session" since the feature
    # was written. The gap is in the **safe** direction (the gate stays on), so the code is
    # left as it is and the claim is corrected; whether the bypass should genuinely span a
    # session is an owner decision, not a side effect of a wording fix.
    if _is_auto_approve_request(user_message):
        auto_approve = True
        config["configurable"]["hitl_bypass"] = True
        logger.info(f"stream_events: HITL bypassed for this turn session={session_id}")

    # Check whether this thread is paused at a HITL interrupt
    graph_state = await graph.aget_state(config)
    has_interrupt = bool(graph_state.tasks and any(
        t.interrupts for t in graph_state.tasks
    ))

    # Either a resume command or the partial state update from _fresh_turn_state.
    input_data: Command[Any] | dict[str, Any]
    if has_interrupt:
        # An approval must be *recognised*, never merely "not recognised as a denial". This was
        # `resume=not _is_denial(...)` against an exact-match list of 13 phrases, which meant
        # every unlisted reply executed the pending destructive command. Measured 2026-08-20:
        # "No.", "NO!", "no thanks", "don't do that", "cancel it", "stop it", "not yet", "wait",
        # "why?" and an empty message all resumed with True. `docs/security.md` has always
        # documented the opposite ("anything else -> treated as denial"); the code now matches it.
        # "approve all" counts as an approval of the pending action as well as enabling bypass —
        # it is an approval phrase, and cancelling the very action the user just approved would
        # be a new bug in the other direction.
        approved = _is_approval(user_message) or _is_auto_approve_request(user_message)
        if not approved and not _is_denial(user_message):
            logger.warning(
                f"stream_events: HITL reply not recognised as approval, cancelling "
                f"thread={session_id} reply={user_message[:80]!r}"
            )
        input_data = Command(resume=approved)
        logger.info(f"stream_events: resuming HITL thread={session_id} approved={approved}")
    else:
        input_data = _fresh_turn_state(user_message, session_id, user_id, user_role)

    callbacks = get_langfuse_callbacks()
    if callbacks:
        config["callbacks"] = callbacks
        config["metadata"] = get_langfuse_run_metadata(session_id)
    try:
        async for event in graph.astream_events(input_data, config=config, version="v2"):
            yield event
    except GraphRecursionError:
        # Surface the halt to the operator as a real event. Dropping it here would be
        # exactly the silent truncation this budget exists to prevent.
        logger.error(
            f"stream_events: graph budget exhausted session={session_id} "
            f"limit={settings.AGENT_GRAPH_RECURSION_LIMIT} — escalating to the operator",
            extra={"session_id": session_id},
        )
        yield {
            "event": "on_agent_budget_exhausted",
            "data": {"limit": settings.AGENT_GRAPH_RECURSION_LIMIT},
        }
        return

    # After the stream ends, check for a newly created interrupt and surface it
    new_state = await graph.aget_state(config)
    if new_state.tasks:
        for task in new_state.tasks:
            for intr in task.interrupts:
                val = intr.value if hasattr(intr, "value") else intr
                if isinstance(val, dict) and val.get("type") == "hitl":
                    yield {"event": "on_hitl_interrupt", "data": val}
                    return


# ── Typed-event translation ───────────────────────────────────────────────────


def _translate_raw_event(session_id: str, raw: dict) -> ToolCallEvent | ToolResultEvent | TokenEvent | HitlRequestEvent | None:
    """
    Convert a LangGraph astream_events v2 dict to a typed emitter Event.

    Status events are emitted directly from nodes (memory_loader, coordinator,
    subagent_executor), so on_chain_start is intentionally not translated here.
    """
    kind = raw.get("event", "")

    if kind == "on_tool_start":
        tool_name = raw.get("name", "tool")
        input_data = raw.get("data", {}).get("input", {})
        command = input_data.get("command") if isinstance(input_data, dict) else None
        return ToolCallEvent(tool=tool_name, command=command, session_id=session_id)

    if kind == "on_tool_end":
        tool_name = raw.get("name", "tool")
        output = raw.get("data", {}).get("output", "")
        # LangChain tool output may be a ToolMessage object
        if hasattr(output, "content"):
            output = output.content
        return ToolResultEvent(tool=tool_name, output=str(output)[:500], session_id=session_id)

    if kind == "on_chat_model_stream":
        chunk = raw.get("data", {}).get("chunk")
        if chunk and hasattr(chunk, "content") and chunk.content:
            return TokenEvent(content=chunk.content, session_id=session_id)

    if kind == "on_agent_budget_exhausted":
        limit = raw.get("data", {}).get("limit", 0)
        return TokenEvent(
            content=GRAPH_BUDGET_EXHAUSTED_MESSAGE.format(limit=limit),
            session_id=session_id,
        )

    if kind == "on_hitl_interrupt":
        val = raw.get("data", {})
        return HitlRequestEvent(
            risk_level=val.get("risk_level", "medium"),
            command=val.get("command", "destructive action"),
            stdin_yaml=val.get("stdin"),
            session_id=session_id,
        )

    return None


# ── Background task for the SSE endpoint ─────────────────────────────────────


def _llm_error_hint(exc: Exception) -> str:
    msg = str(exc).lower()
    if "missing an 'http://' or 'https://'" in msg or "unsupported protocol" in msg:
        return (
            "LLM connection failed: AZURE_OPENAI_ENDPOINT is missing the protocol. "
            "Set it to https://... in ~/.kubeintellect/.env and restart."
        )
    if "authentication" in msg or "401" in msg or "api key" in msg:
        return "LLM authentication failed: check your API key in ~/.kubeintellect/.env."
    if "connection error" in msg or "connection refused" in msg:
        return "LLM connection failed: check your endpoint URL and network connectivity."
    if "rate limit" in msg or "429" in msg:
        return "LLM rate limit hit — please try again in a moment."
    if "content_filter" in msg or "content management policy" in msg or "responsibleaipolicyviolation" in msg:
        return (
            "Azure content filter blocked this request. "
            "Try rephrasing — if the issue persists, start a new session (/new) to reset conversation history."
        )
    return f"LLM error: {exc}"


async def run_session(
    user_message: str,
    session_id: str,
    user_id: str = "default",
    user_role: str = "admin",
    auto_approve: bool = False,
) -> None:
    """
    Run the graph for one turn and emit typed events to the per-session queue.

    Called via ``asyncio.create_task()`` by the FastAPI streaming endpoint.
    Guarantees that ``close_session()`` is always called, even on error, so
    the SSE generator never blocks waiting for a sentinel that never arrives.
    """
    try:
        # Buffer token events so intermediate LLM calls (planning steps inside
        # create_react_agent's react loop) are not streamed to the client.
        # Each on_tool_start signals that the preceding LLM call was a planning
        # step — discard its tokens. Only the final synthesis tokens (no
        # following tool call) are flushed after the loop ends.
        token_buffer: list = []

        # V4 cortex: nodes emit tokens/status directly through the emitter
        # (only the synthesis model streams), so raw token translation would
        # double-emit. The buffer workaround below is V2-only.
        v4 = settings.CORTEX_V4_ENABLED

        async for raw in stream_events(user_message, session_id, user_id, user_role, auto_approve=auto_approve):
            kind = raw.get("event", "")

            if v4 and kind == "on_chat_model_stream":
                continue

            if kind == "on_chat_model_stream":
                typed = _translate_raw_event(session_id, raw)
                if typed is not None:
                    token_buffer.append(typed)
                continue

            if kind == "on_tool_start":
                # Previous LLM call was an intermediate planning step — drop its tokens.
                token_buffer.clear()

            typed = _translate_raw_event(session_id, raw)
            if typed is not None:
                await emit(session_id, typed)

        # Flush the final synthesis tokens (no tool_start followed them).
        for tok in token_buffer:
            await emit(session_id, tok)

    except Exception as exc:
        logger.error(f"run_session error session={session_id}: {exc}", exc_info=False)
        user_msg = _llm_error_hint(exc)
        await emit(session_id, ErrorEvent(session_id=session_id, error=user_msg))
    finally:
        try:
            await close_session(session_id)
        except Exception:
            pass
