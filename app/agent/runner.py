"""SSE bridge for the DeepAgents-based KubeIntellect.

Per turn:
  1. StatusEvent("loading") + pre-seed /snapshot.md
  2. If thread is paused at a HITL interrupt, treat the user message as
     approve/deny and resume with Command(resume=...). Otherwise feed a
     fresh HumanMessage.
  3. Stream astream_events → typed Token / ToolCall / ToolResult / Status
     / Plan events.
  4. After the stream, walk graph.aget_state for any new interrupt and
     emit a HitlRequestEvent.
  5. Always close_session() in finally.
"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from app.agent.hitl import (
    is_auto_approve_request as _is_auto_approve_request,
    is_denial as _is_denial,
)
from app.agent.main_agent import get_graph
from app.core.llm import get_langfuse_callbacks
from app.streaming.emitter import (
    ErrorEvent,
    HitlRequestEvent,
    PlanEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    close_session,
    emit,
)
from app.tools.snapshot_tool import seed_snapshot_state
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Tools whose calls we surface to the UI as ToolCall/ToolResult events.
# Internal DeepAgents tools (write_file, ls, read_file, task) are
# intentionally excluded — they're plumbing. write_todos and HITL get
# their own translations below.
_USER_VISIBLE_TOOLS = {
    "run_kubectl", "query_prometheus", "query_loki",
    "refresh_snapshot", "lookup_playbook",
    "read_memory", "write_memory",
}


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
    if "disallowed shell characters" in msg or "shell metachar" in msg:
        return (
            "Command rejected: shell interpolation not allowed. "
            "Run the pod-lookup and exec as two separate kubectl calls: "
            "first `kubectl get pods -n <ns> -l <selector> -o jsonpath='{.items[0].metadata.name}'` "
            "then `kubectl exec -n <ns> -it <pod-name> -- <cmd>`."
        )
    if "content_filter" in msg or "content management policy" in msg or "responsibleaipolicyviolation" in msg:
        return (
            "Azure content filter blocked this request. "
            "Try rephrasing — if the issue persists, start a new session (/new) to reset conversation history."
        )
    return f"LLM error: {exc}"


def _normalise_todos(raw_output: Any) -> list[dict] | None:
    """Coerce write_todos output into [{description, status}, ...] for PlanEvent.

    The deepagents write_todos tool may return a list of strings, a list of
    dicts with arbitrary keys, or a Command/dict wrapping such a list.
    Returns None if we can't make sense of it (so we just skip emit).
    """
    payload = raw_output
    if hasattr(payload, "update"):
        payload = getattr(payload, "update", None)
    if isinstance(payload, dict):
        for key in ("todos", "plan", "items"):
            if key in payload and isinstance(payload[key], list):
                payload = payload[key]
                break
        else:
            return None
    if not isinstance(payload, list) or not payload:
        return None

    steps: list[dict] = []
    for item in payload:
        if isinstance(item, str):
            steps.append({"description": item, "status": "pending"})
        elif isinstance(item, dict):
            desc = (
                item.get("description")
                or item.get("content")
                or item.get("task")
                or item.get("title")
            )
            if not desc:
                continue
            steps.append({
                "description": str(desc),
                "status": str(item.get("status", "pending")),
            })
    return steps or None


async def _emit_event(session_id: str, raw: dict) -> None:
    """Translate one astream_events v2 dict to an emitter Event and push it."""
    kind = raw.get("event", "")
    name = raw.get("name", "")

    if kind == "on_chat_model_stream":
        # Only stream tokens from the coordinator — subagent model outputs are
        # internal tool results and must not appear in the user-visible SSE stream.
        #
        # Subagents are invoked via .invoke() inside the task() tool. Their LLM
        # calls inherit the parent's callbacks, so they emit on_chat_model_stream
        # events with additional entries in parent_ids (one per nesting level).
        # Coordinator events have ≤3 parent_ids (graph run + node run + maybe 1 more).
        # Subagent events have 4+ parent_ids because they run inside a tool call.
        parent_ids = raw.get("parent_ids") or []
        if len(parent_ids) > 3:
            return  # subagent — skip
        chunk = raw.get("data", {}).get("chunk")
        if chunk and getattr(chunk, "content", None):
            await emit(session_id, TokenEvent(content=chunk.content, session_id=session_id))
        return

    if kind == "on_tool_start":
        input_data = raw.get("data", {}).get("input", {})
        if name in _USER_VISIBLE_TOOLS:
            command = input_data.get("command") if isinstance(input_data, dict) else None
            await emit(session_id, ToolCallEvent(tool=name, command=command, session_id=session_id))
            return
        if name == "task" and isinstance(input_data, dict):
            target = input_data.get("subagent_type") or "subagent"
            await emit(session_id, StatusEvent(
                phase="dispatching",
                message=f"→ {target}",
                session_id=session_id,
            ))
            return
        return

    if kind == "on_tool_end":
        if name in _USER_VISIBLE_TOOLS:
            output: Any = raw.get("data", {}).get("output", "")
            if hasattr(output, "content"):
                output = output.content
            await emit(session_id, ToolResultEvent(
                tool=name,
                output=str(output)[:500],
                session_id=session_id,
            ))
            return
        if name == "write_todos":
            steps = _normalise_todos(raw.get("data", {}).get("output"))
            if steps:
                await emit(session_id, PlanEvent(steps=steps, session_id=session_id))
            return
        if name == "write_file":
            input_data = raw.get("data", {}).get("input", {})
            path = input_data.get("file_path") if isinstance(input_data, dict) else None
            if path and path.startswith("/findings/"):
                stem = path.rsplit("/", 1)[-1].removesuffix(".md")
                await emit(session_id, StatusEvent(
                    phase="investigating",
                    message=f"{stem} findings saved",
                    session_id=session_id,
                ))


def _extract_pending_interrupt(state) -> dict | None:
    """Return the first HITL interrupt payload on the thread, or None."""
    for task in (state.tasks or ()):
        for intr in (task.interrupts or ()):
            val = intr.value if hasattr(intr, "value") else intr
            if isinstance(val, dict) and val.get("type") == "hitl":
                return val
    return None


async def run_session(
    user_message: str,
    session_id: str,
    user_id: str = "default",
    user_role: str = "admin",
    auto_approve: bool = False,
) -> None:
    """Run one turn of the deep agent and emit typed events to the per-session queue.

    Called via ``asyncio.create_task()`` from the FastAPI streaming endpoint.
    Always calls ``close_session()`` in finally so the SSE generator never blocks.
    """
    try:
        graph = await get_graph()

        await emit(session_id, StatusEvent(
            phase="loading",
            message="Thinking…",
            session_id=session_id,
        ))

        # "approve all" arms session-wide HITL bypass for this and future turns.
        if _is_auto_approve_request(user_message):
            auto_approve = True
            logger.info(f"run_session: HITL bypass enabled for session={session_id}")

        config: dict[str, Any] = {
            "configurable": {
                "thread_id": session_id,
                "session_id": session_id,
                "user_id": user_id,
                "user_role": user_role,
                "hitl_bypass": auto_approve,
            },
            # LangGraph's config-merge path can revert to the 25-step Pregel default
            # even when the graph was built with .with_config(recursion_limit=9_999).
            # Explicitly pass 100 here to cover the deepest subagent-nested scenarios.
            "recursion_limit": 100,
        }
        callbacks = get_langfuse_callbacks()
        if callbacks:
            config["callbacks"] = callbacks

        # ── HITL resume detection ────────────────────────────────────────────
        # If the thread is paused at an interrupt, treat user_message as
        # approve/deny and resume with Command(resume=approved). Skip the
        # snapshot pre-seed in that case — we're continuing a turn, not
        # starting one.
        prior_state = await graph.aget_state(config)
        pending = _extract_pending_interrupt(prior_state)

        if pending is not None:
            denied = _is_denial(user_message)
            input_data: Any = Command(resume=not denied)
            logger.info(
                f"run_session: resuming HITL thread={session_id} approved={not denied}"
            )
        else:
            # Pre-seed /snapshot.md so the agent starts informed about the cluster.
            fm = await seed_snapshot_state(graph, config)
            if fm:
                await emit(session_id, StatusEvent(
                    phase="snapshot",
                    message=(
                        f"Snapshot seeded: {fm['pod_count']} pods, "
                        f"issues={fm['has_issues']}, warnings={fm['has_warnings']}"
                    ),
                    session_id=session_id,
                ))
            input_data = {"messages": [HumanMessage(content=user_message)]}

        async for raw in graph.astream_events(input_data, config=config, version="v2"):
            await _emit_event(session_id, raw)

        # ── Surface any new HITL interrupt that arose during the stream ──────
        new_state = await graph.aget_state(config)
        new_pending = _extract_pending_interrupt(new_state)
        if new_pending is not None:
            await emit(session_id, HitlRequestEvent(
                risk_level=new_pending.get("risk_level", "medium"),
                command=new_pending.get("command", "destructive action"),
                stdin_yaml=new_pending.get("stdin"),
                session_id=session_id,
            ))

    except Exception as exc:
        logger.error(f"run_session error session={session_id}: {exc}", exc_info=False)
        await emit(session_id, ErrorEvent(
            session_id=session_id,
            error=_llm_error_hint(exc),
        ))
    finally:
        try:
            await close_session(session_id)
        except Exception:
            pass
