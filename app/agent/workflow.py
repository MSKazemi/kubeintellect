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
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from app.agent.hitl import is_denial as _is_denial
from app.agent.nodes.context_fetcher import context_fetcher
from app.agent.nodes.coordinator import coordinator
from app.agent.nodes.memory_loader import memory_loader
from app.agent.nodes.subagent import run_subagent
from app.agent.state import AgentFinding, AgentState, SubagentInput
from app.core.config import settings
from app.core.llm import get_langfuse_callbacks
from app.streaming.emitter import (
    ErrorEvent,
    HitlRequestEvent,
    StatusEvent,
    ToolCallEvent,
    ToolResultEvent,
    TokenEvent,
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


# ── Routing function ──────────────────────────────────────────────────────────


def route_coordinator(state: AgentState) -> str | list[Send]:
    """
    Conditional edge after coordinator.

    - rca_required=True  → fan-out: return list[Send] to 4 subagent_executor nodes.
    - rca_result is set  → synthesis done, go to END.
    - findings present   → subagents finished, route back to coordinator for synthesis.
    - otherwise          → direct answer completed, go to END.

    Returning list[Send] is LangGraph's fan-out mechanism; it bypasses the
    string-based path_map and dispatches directly to the target node.
    """
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
    builder.add_node("subagent_executor", subagent_executor)

    builder.add_edge(START, "memory_loader")
    builder.add_edge("memory_loader", "context_fetcher")
    builder.add_edge("context_fetcher", "coordinator")

    # No path_map: route_coordinator may return a string, END, or list[Send].
    # LangGraph handles list[Send] as fan-out commands directly without consulting
    # a path_map, so we omit the mapping to avoid spurious routing constraints.
    builder.add_conditional_edges("coordinator", route_coordinator)

    # All subagent branches feed back into coordinator for synthesis (fan-in).
    # LangGraph waits for all parallel Send branches before running coordinator.
    builder.add_edge("subagent_executor", "coordinator")

    return builder


# ── Compiled graph (singleton with checkpointer) ──────────────────────────────

_graph = None
_checkpointer_cm = None   # the context manager (holds the connection)
_checkpointer = None      # the actual AsyncPostgresSaver instance
_graph_lock = asyncio.Lock()


async def init_graph() -> None:
    """Build and compile the graph. Call once at app startup."""
    global _graph, _checkpointer_cm, _checkpointer
    async with _graph_lock:
        if _graph is not None:
            return
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
            _checkpointer_cm = AsyncPostgresSaver.from_conn_string(settings.POSTGRES_DSN)
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
        "pending_hitl": None,
        **(extra or {}),
    }


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
    config = {"configurable": {"thread_id": session_id, "user_role": user_role}}

    state = _fresh_turn_state(user_message, session_id, user_id, user_role, extra_state)

    callbacks = get_langfuse_callbacks()
    if callbacks:
        config["callbacks"] = callbacks
    result = await graph.ainvoke(state, config=config)
    return result


async def stream_events(
    user_message: str,
    session_id: str,
    user_id: str = "default",
    user_role: str = "admin",
):
    """Async generator yielding LangGraph astream_events for SSE.

    If the thread has a pending HITL interrupt, user_message is interpreted
    as an approval/denial and the graph is resumed via Command(resume=...).
    Otherwise a fresh turn is started.
    """
    graph = await get_graph()
    config = {"configurable": {"thread_id": session_id, "user_role": user_role}}

    # Check whether this thread is paused at a HITL interrupt
    graph_state = await graph.aget_state(config)
    has_interrupt = bool(graph_state.tasks and any(
        t.interrupts for t in graph_state.tasks
    ))

    if has_interrupt:
        denied = _is_denial(user_message)
        input_data = Command(resume=not denied)
        logger.info(f"stream_events: resuming HITL thread={session_id} approved={not denied}")
    else:
        input_data = _fresh_turn_state(user_message, session_id, user_id, user_role)

    callbacks = get_langfuse_callbacks()
    if callbacks:
        config["callbacks"] = callbacks
    async for event in graph.astream_events(input_data, config=config, version="v2"):
        yield event

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


def _translate_raw_event(session_id: str, raw: dict) -> "ToolCallEvent | ToolResultEvent | TokenEvent | HitlRequestEvent | None":
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
    return f"LLM error: {exc}"


async def run_session(
    user_message: str,
    session_id: str,
    user_id: str = "default",
    user_role: str = "admin",
) -> None:
    """
    Run the graph for one turn and emit typed events to the per-session queue.

    Called via ``asyncio.create_task()`` by the FastAPI streaming endpoint.
    Guarantees that ``close_session()`` is always called, even on error, so
    the SSE generator never blocks waiting for a sentinel that never arrives.
    """
    try:
        async for raw in stream_events(user_message, session_id, user_id, user_role):
            typed = _translate_raw_event(session_id, raw)
            if typed is not None:
                await emit(session_id, typed)
    except Exception as exc:
        logger.error(f"run_session error session={session_id}: {exc}", exc_info=False)
        user_msg = _llm_error_hint(exc)
        await emit(session_id, ErrorEvent(session_id=session_id, error=user_msg))
    finally:
        try:
            await close_session(session_id)
        except Exception:
            pass
