# app/orchestration/workflow.py
"""
KubeIntellect Workflow Orchestration

Graph construction, compilation, initialization, and execution entry-point.
All state/tool/agent/routing concerns live in their respective sub-modules;
this file wires them together and exposes the public API.

Public API (must stay importable from this module):
  - run_kubeintellect_workflow
  - reload_dynamic_tools_into_agent
"""

import asyncio
import time as _time
import uuid
from typing import AsyncGenerator, Dict, Any

# LangChain / LangGraph imports
from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

# App imports
from app.core.config import settings
from app.core.llm_gateway import get_supervisor_llm as get_llm, classify_llm_error
from app.orchestration.state import AGENT_MEMBERS, MAX_RECURSION_LIMIT, KubeIntellectState
from app.orchestration.tool_loader import load_all_tool_categories
from app.orchestration.agents import (
    get_agent_definitions,
    create_all_worker_agents,
    reload_dynamic_tools_into_agent as _reload_dynamic_tools_into_agent,
)
from app.orchestration.routing import (
    create_supervisor_chain,
    create_worker_nodes,
    supervisor_router_node_func,
)
from app.orchestration.diagnostics import (
    diagnostics_orchestrator_node,
    diagnostics_fan_out,
    diagnostics_logs_node,
    diagnostics_metrics_node,
    diagnostics_events_node,
    diagnostics_collect_node,
)
from app.utils.logger_config import setup_logging
from app.utils.metrics import workflow_duration_seconds, llm_stream_errors_total
from app.utils.otel_guard import async_safe_otel_ctx  # noqa: F401 — applied via except ValueError pattern below
from app.utils.postgres_checkpointer import get_checkpointer

logger = setup_logging(app_name="kubeintellect")


def _get_langfuse_handler(
    thread_id: str = None,
    user_id: str = None,
    query: str = None,
    tags: list = None,
):
    """Build a Langfuse CallbackHandler with per-request trace context.

    Langfuse v4: we use trace_context={"trace_id": <deterministic-id>} so that all
    LangGraph spans for a conversation are grouped under ONE trace, identified by
    thread_id (the conversation key).  Session/user attributes are set via
    update_current_span() inside execute_workflow_stream once LangGraph starts.

    We intentionally avoid propagate_attributes() here because it uses
    contextvars.attach() which breaks when the context manager is inside an async
    generator that gets closed (GeneratorExit) from a different async context.
    """
    if not settings.LANGFUSE_ENABLED:
        return []
    try:
        from app.core.llm_gateway import get_langfuse_callbacks, get_langfuse_client
        from langfuse.langchain import CallbackHandler
        callbacks = get_langfuse_callbacks()   # ensures client is initialised
        if not callbacks:
            return []
        client = get_langfuse_client()
        trace_id = client.create_trace_id(seed=thread_id) if (client and thread_id) else None
        trace_ctx = {"trace_id": trace_id} if trace_id else None
        return [CallbackHandler(trace_context=trace_ctx)]
    except Exception as e:
        logger.debug(f"Langfuse handler creation failed (tracing disabled for this request): {e}")
        return []


def _flush_langfuse():
    """Flush pending Langfuse spans — call after a workflow completes."""
    if not settings.LANGFUSE_ENABLED:
        return
    try:
        from app.core.llm_gateway import get_langfuse_client
        client = get_langfuse_client()
        if client:
            client.flush()
    except Exception as e:
        logger.debug(f"Langfuse flush failed: {e}")


#####################################################################
#                   Graph Construction                              #
#####################################################################

def create_workflow_graph(supervisor_chain, worker_nodes: Dict[str, Any]) -> StateGraph:
    """
    Create and configure the workflow graph.

    Args:
        supervisor_chain: The supervisor decision chain
        worker_nodes: Dictionary of worker node functions

    Returns:
        Configured StateGraph
    """
    graph = StateGraph(KubeIntellectState)

    # Add supervisor node — direct entry point
    def supervisor_func(state):
        return supervisor_router_node_func(state, supervisor_chain)

    graph.add_node("Supervisor", supervisor_func)

    # Add worker nodes
    for node_name, node_func in worker_nodes.items():
        graph.add_node(node_name, node_func)

    # Entry point: Supervisor
    graph.set_entry_point("Supervisor")

    # Add edges from workers back to supervisor
    for member in AGENT_MEMBERS:
        if member in worker_nodes:
            graph.add_edge(member, "Supervisor")

    # ---------------------------------------------------------------------------
    # DiagnosticsOrchestrator subgraph (LangGraph Send API fan-out)
    #
    #   Supervisor → DiagnosticsOrchestrator
    #                 ↓ [Send] (parallel)
    #   DiagnosticsLogs  DiagnosticsMetrics  DiagnosticsEvents
    #                 ↓ (barrier — all three must complete)
    #           DiagnosticsCollect
    #                 ↓
    #             Supervisor
    # ---------------------------------------------------------------------------
    graph.add_node("DiagnosticsOrchestrator", diagnostics_orchestrator_node)
    graph.add_node("DiagnosticsLogs",    diagnostics_logs_node)
    graph.add_node("DiagnosticsMetrics", diagnostics_metrics_node)
    graph.add_node("DiagnosticsEvents",  diagnostics_events_node)
    graph.add_node("DiagnosticsCollect", diagnostics_collect_node)

    # Fan-out via Send — LangGraph dispatches all three sub-nodes in parallel
    graph.add_conditional_edges("DiagnosticsOrchestrator", diagnostics_fan_out)

    # Barrier: all three signal nodes converge at DiagnosticsCollect
    graph.add_edge("DiagnosticsLogs",    "DiagnosticsCollect")
    graph.add_edge("DiagnosticsMetrics", "DiagnosticsCollect")
    graph.add_edge("DiagnosticsEvents",  "DiagnosticsCollect")

    # DiagnosticsCollect returns its result to the Supervisor
    graph.add_edge("DiagnosticsCollect", "Supervisor")

    # Add conditional edges from supervisor (includes DiagnosticsOrchestrator)
    conditional_map = {k: k for k in AGENT_MEMBERS if k in worker_nodes}
    conditional_map["DiagnosticsOrchestrator"] = "DiagnosticsOrchestrator"
    conditional_map["FINISH"] = END

    graph.add_conditional_edges(
        "Supervisor",
        lambda x: x["next"],
        conditional_map
    )

    return graph


def compile_workflow_graph(graph: StateGraph, checkpointer: AsyncPostgresSaver):
    """
    Compile the workflow graph with checkpointing and interrupts.

    Args:
        graph: The StateGraph to compile
        checkpointer: Async Postgres checkpointer (already set up by caller)

    Returns:
        Compiled workflow application
    """
    try:
        # interrupt_before gates every listed agent node before it executes.
        # Structural HITL is reserved for agents that synthesise or apply code/YAML:
        #   - CodeGenerator : generates and registers dynamic Python tools
        #   - Apply         : applies synthesised YAML to the cluster
        #
        # Agents that handle both reads AND writes (Lifecycle, RBAC, Execution,
        # Deletion) are NOT listed here — structural HITL cannot distinguish a
        # read from a write, so gating them breaks read-only queries (e.g. "list pods").
        # Those agents handle write confirmation conversationally via their system
        # prompts, consistent with the Deletion agent's existing design.
        app = graph.compile(
            checkpointer=checkpointer,
            interrupt_before=[
                "CodeGenerator",
                "Apply",
            ],
        )
        logger.info(
            "KubeIntellect Workflow compiled with AsyncPostgresSaver — "
            "HITL interrupt_before: CodeGenerator, Apply"
        )
        return app
    except Exception as e:
        logger.error(f"Failed to compile KubeIntellect Workflow: {e}", exc_info=True)
        return None


#####################################################################
#                   Main Initialization                            #
#####################################################################

def initialize_workflow(checkpointer: AsyncPostgresSaver):
    """
    Initialize the complete workflow system (sync; checkpointer is already set up).

    Args:
        checkpointer: AsyncPostgresSaver instance, fully set up by initialize_workflow_async().

    Returns:
        Tuple of (compiled_app, worker_agents) or (None, None) on failure
    """
    # Initialize LLM
    llm = get_llm()
    if not llm:
        return None, None

    # Load all tool categories
    tool_categories = load_all_tool_categories()

    # Get agent definitions
    agent_definitions = get_agent_definitions(tool_categories)

    # Create worker agents
    worker_agents = create_all_worker_agents(llm, agent_definitions)

    # Create supervisor chain — pass tool_categories so the prompt auto-generates
    # an agent capability manifest from the actual tool lists (no manual sync needed).
    supervisor_chain = create_supervisor_chain(llm, tool_categories)
    if not supervisor_chain:
        return None, worker_agents

    # Create worker nodes
    worker_nodes = create_worker_nodes(worker_agents)

    graph = create_workflow_graph(supervisor_chain, worker_nodes)
    app = compile_workflow_graph(graph, checkpointer)

    return app, worker_agents


# These are populated by initialize_workflow_async() at FastAPI startup.
# They are None/empty until startup completes — requests arriving before
# startup should not be served (readiness probe prevents this).
kubeintellect_app = None
worker_agents: Dict[str, Any] = {}

# Pool reference kept for clean shutdown
_langgraph_pool = None


async def initialize_workflow_async() -> None:
    """
    Async entrypoint — creates the AsyncPostgresSaver connection pool, runs
    LangGraph's schema migration (setup()), then builds the workflow graph.
    Must be called from FastAPI's startup event handler.
    """
    global kubeintellect_app, worker_agents, _langgraph_pool

    from psycopg_pool import AsyncConnectionPool

    logger.info("Creating AsyncConnectionPool for LangGraph checkpointing …")
    pool = AsyncConnectionPool(
        conninfo=settings.POSTGRES_DSN,
        max_size=settings.POSTGRES_POOL_MAX_CONN,
        kwargs={"autocommit": True, "prepare_threshold": 0},
        open=False,
    )
    await pool.open()
    _langgraph_pool = pool

    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()  # creates langgraph_checkpoint* tables if not present
    logger.info("AsyncPostgresSaver ready — LangGraph state is now durable in PostgreSQL")

    # Create reflection_memories table if it doesn't exist.
    from app.services.reflection_memory_service import setup_schema as _setup_reflection_schema
    await _setup_reflection_schema(pool)

    # Create conversation_context table if it doesn't exist.
    from app.services.conversation_context_service import setup_schema as _setup_context_schema
    await _setup_context_schema(pool)

    # Create failure_patterns table if it doesn't exist.
    from app.services.failure_pattern_service import FailurePatternService as _FPS
    await _FPS.setup_schema(pool)

    # Create user_preferences table if it doesn't exist.
    from app.services.user_preference_service import UserPreferenceService as _UPS
    await _UPS.setup_schema(pool)

    kubeintellect_app, worker_agents = initialize_workflow(checkpointer)


async def close_langgraph_checkpointer() -> None:
    """Close the AsyncConnectionPool on application shutdown."""
    global _langgraph_pool
    if _langgraph_pool is not None:
        await _langgraph_pool.close()
        _langgraph_pool = None
        logger.info("AsyncConnectionPool closed")


# Semaphore that caps the number of simultaneously in-flight workflow executions.
# Prevents Azure OpenAI TPM exhaustion under load; value is read from settings so
# it can be tuned per environment without a code change.
_workflow_semaphore: asyncio.Semaphore | None = None


def _get_workflow_semaphore() -> asyncio.Semaphore:
    """Return (and lazily create) the per-event-loop workflow semaphore."""
    global _workflow_semaphore
    if _workflow_semaphore is None:
        _workflow_semaphore = asyncio.Semaphore(settings.MAX_CONCURRENT_WORKFLOWS)
    return _workflow_semaphore


# ---------------------------------------------------------------------------
# reload_dynamic_tools_into_agent — public re-export (used by code_generator_tools.py)
# Wraps the agents-module version, passing the module-level worker_agents dict.
# ---------------------------------------------------------------------------

def reload_dynamic_tools_into_agent() -> bool:
    """
    Reload dynamic tools from PVC and update the DynamicToolsExecutor agent.
    This should be called after a new tool is registered to make it immediately available.
    """
    global worker_agents
    return _reload_dynamic_tools_into_agent(worker_agents)


__all__ = [
    "kubeintellect_app", "worker_agents",
    "run_kubeintellect_workflow", "reload_dynamic_tools_into_agent",
    "initialize_workflow_async", "close_langgraph_checkpointer",
]


#####################################################################
#                   Workflow Execution                             #
#####################################################################

async def run_kubeintellect_workflow(
    messages: list,
    conversation_id: str = None,
    user_id: str = None,
    stream: bool = False,
    resume: bool = False,
) -> AsyncGenerator[dict, None]:
    """
    Execute the KubeIntellect workflow with HITL support and streaming.

    This function handles:
    - HITL checkpointing and resume functionality
    - Streaming responses for real-time feedback
    - Error handling and recovery
    - Conversation state management

    Args:
        messages: List of LangChain message objects representing conversation history
        conversation_id: Conversation identifier for checkpointing
        user_id: User identifier for user-level operations
        stream: Whether to stream responses (currently always streams)
        resume: Whether this is resuming from a checkpoint

    Yields:
        Dict with 'type' and 'data' keys for different event types:
        - 'content_delta': Streaming content from agents
        - 'breakpoint': HITL approval requests
        - 'workflow_complete': Final completion message
        - 'error': Error information
    """
    semaphore = _get_workflow_semaphore()
    if semaphore._value <= 0:
        logger.warning(
            f"Workflow semaphore at capacity ({settings.MAX_CONCURRENT_WORKFLOWS} concurrent workflows). "
            "Rejecting new request."
        )
        yield {
            "type": "error",
            "data": (
                "The system is currently handling too many requests. "
                "Please wait a moment and try again."
            ),
        }
        return

    await semaphore.acquire()
    try:
        if not kubeintellect_app:
            logger.error("KubeIntellect Workflow is not available")
            yield {"type": "error", "data": "KubeIntellect Workflow is not available"}
            return

        # Log conversation context
        logger.info(f"Workflow starting with {len(messages)} messages in conversation")

        # Extract user query text for Langfuse trace metadata (first HumanMessage)
        from langchain_core.messages import HumanMessage as _HM
        _user_query = next(
            (m.content for m in reversed(messages) if isinstance(m, _HM) and m.content),
            None,
        )

        # Prepare workflow configuration
        config = {"recursion_limit": MAX_RECURSION_LIMIT}
        primary_id = conversation_id or user_id
        thread_id = f"thread_{conversation_id}" if conversation_id else (
            f"thread_{user_id}" if user_id else f"thread_{uuid.uuid4()}"
        )
        config["configurable"] = {"thread_id": thread_id}

        # Attach per-request Langfuse callbacks so every node in the graph is traced.
        # Uses trace_context with a deterministic trace_id (seeded by thread_id) so all
        # spans for a conversation are grouped under one Langfuse trace.
        # We intentionally avoid propagate_attributes() here — it uses contextvars.attach()
        # which raises ValueError when the async generator is closed from a different context.
        _tags = ["hitl_resume" if resume else "new_request", "kubernetes"]
        langfuse_cbs = _get_langfuse_handler(
            thread_id=thread_id,
            user_id=user_id,
            query=_user_query,
            tags=_tags,
        )
        if langfuse_cbs:
            config["callbacks"] = langfuse_cbs

        pg_checkpointer = get_checkpointer()

        # Handle resume from checkpoint
        if resume and primary_id:
            # Mark that we're resuming so we can skip the interrupt we just approved.
            # Stored in configurable (not top-level) to prevent OTel/Langfuse from
            # treating the boolean as a span attribute and logging a type-mismatch warning.
            config.setdefault("configurable", {})["_resuming_from_approval"] = True
            async for event in handle_workflow_resume(
                kubeintellect_app, pg_checkpointer, primary_id, config
            ):
                yield event
                if event["type"] in ["workflow_complete", "breakpoint"]:
                    # Persist working context on resume completion too
                    if event["type"] == "workflow_complete" and conversation_id and _langgraph_pool:
                        try:
                            from app.services.conversation_context_service import (
                                extract_context_from_messages as _extract_ctx,
                                save_context as _save_ctx,
                            )
                            _wf_messages = event.get("messages", [])
                            _ctx = _extract_ctx(_wf_messages)
                            if _ctx:
                                await _save_ctx(conversation_id, user_id, _ctx, _langgraph_pool)
                        except Exception as _ctx_exc:
                            logger.debug("Could not save conversation context (resume): %s", _ctx_exc)
                    _flush_langfuse()
                    return

        # Load all memory context in parallel: reflection lessons, failure-pattern
        # match, and user preferences.  Renders into a single pinned SystemMessage
        # (≤ 400 tokens) and fires detect_and_save() as a background task.
        from app.services.memory_orchestrator import MemoryOrchestrator
        _memory_ctx = await MemoryOrchestrator.build_context(
            user_id=user_id,
            query=_user_query,
            conversation_history=messages,
            current_namespace=None,   # not yet known at request start; H1 uses DB history
            current_cluster=None,
            pool=_langgraph_pool,
        )
        if _memory_ctx.pinned_message:
            messages = [_memory_ctx.pinned_message] + list(messages)

        # Execute normal workflow
        initial_state = {
            "messages": messages,
            "next": "",
            "intermediate_steps": [],
            "supervisor_cycles": 0,
            "agent_results": {},
            "last_tool_error": None,
            "task_complete": None,
            "reflection_memory": [],   # rendered into pinned_message; suppress routing.py re-injection
            "plan": None,
            "plan_step": 0,
            "task_plan": None,
            "plan_execution_state": None,
            "steps_taken": [],
        }

        # Increment times_seen for the matched failure pattern (non-blocking).
        if _memory_ctx.matched_pattern_id and _langgraph_pool:
            try:
                from app.services.failure_pattern_service import FailurePatternService as _FPS
                asyncio.create_task(
                    _FPS.update_seen(_memory_ctx.matched_pattern_id, _langgraph_pool)
                )
            except Exception as _fp_upd_exc:
                logger.debug("failure_pattern: update_seen task error (non-fatal): %s", _fp_upd_exc)

        async for event in execute_workflow_stream(
            kubeintellect_app, pg_checkpointer, initial_state, config, primary_id, thread_id
        ):
            yield event
            if event["type"] in ["workflow_complete", "breakpoint"]:
                # Persist working context (namespace, resource) for this conversation
                # so it survives message-window trimming on future turns.
                if event["type"] == "workflow_complete" and conversation_id and _langgraph_pool:
                    try:
                        from app.services.conversation_context_service import (
                            extract_context_from_messages as _extract_ctx,
                            save_context as _save_ctx,
                        )
                        _wf_messages = event.get("messages", [])
                        _ctx = _extract_ctx(_wf_messages)
                        # B5: if a deletion confirmation is pending, persist the confirmation
                        # message text so it survives the graph state wipe and is restored
                        # as a pinned context note on the user's next (confirming) request.
                        if event.get("deletion_confirmation_pending") and _wf_messages:
                            _del_msg = next(
                                (
                                    str(m.content)
                                    for m in reversed(_wf_messages)
                                    if isinstance(m, AIMessage) and getattr(m, "name", None) == "Deletion"
                                ),
                                None,
                            )
                            if _del_msg:
                                _ctx["pending_deletion"] = _del_msg
                                logger.info(
                                    "B5: persisting pending_deletion context for conversation %s",
                                    conversation_id,
                                )
                        elif not event.get("deletion_confirmation_pending"):
                            # Clear any stale pending_deletion from a prior turn.
                            _ctx.setdefault("pending_deletion", None)
                        if _ctx:
                            await _save_ctx(conversation_id, user_id, _ctx, _langgraph_pool)
                            logger.debug(
                                "Saved working context for conversation %s: %s",
                                conversation_id, _ctx,
                            )
                    except Exception as _ctx_exc:
                        logger.debug("Could not save conversation context (non-critical): %s", _ctx_exc)
                _flush_langfuse()
                return

    except GeneratorExit:
        # Properly handle generator cleanup
        logger.debug("Workflow generator exited - expected behavior")
        raise
    except Exception as e:
        logger.critical(f"Critical error during workflow execution: {e}", exc_info=True)
        try:
            yield {"type": "error", "data": "An error occurred while processing your request. Please try again or rephrase your question."}
        except Exception as yield_error:
            # If we can't even yield an error, log it but don't crash
            logger.critical(f"Failed to yield error message: {yield_error}", exc_info=True)
    finally:
        semaphore.release()


async def handle_workflow_resume(
    app, pg_checkpointer, primary_id: str, config: dict
) -> AsyncGenerator[dict, None]:
    """Handle resuming workflow from a checkpoint."""
    # Declared before the outer try so it is always in scope for the except handlers.
    _partial_chars: int = 0
    try:
        thread_id = config.get("configurable", {}).get("thread_id")
        if not thread_id:
            logger.warning(f"No thread_id found in config for primary_id={primary_id}")
            return

        checkpoint = pg_checkpointer.load_checkpoint(primary_id, thread_id)
        if not checkpoint:
            logger.warning(f"No checkpoint found for primary_id={primary_id}, thread_id={thread_id}")
            return

        stored_config, stored_state = checkpoint
        # Preserve Langfuse callbacks across config merge — stored checkpoints do not
        # carry callback objects (they are not serialisable).
        _preserved_callbacks = config.get("callbacks")
        config.update(stored_config)
        if _preserved_callbacks:
            config["callbacks"] = _preserved_callbacks

        logger.info(f"Resuming workflow from checkpoint for thread_id: {thread_id}")

        # Get current state from LangGraph's checkpointer
        current_langgraph_state = await app.aget_state(config)
        if current_langgraph_state and hasattr(current_langgraph_state, 'values'):
            # If we're at an interrupt, we need to continue past it
            # LangGraph will automatically continue when we call astream with the same config
            logger.info(f"Resuming from LangGraph state, task_id: {current_langgraph_state.task_id if hasattr(current_langgraph_state, 'task_id') else 'N/A'}")

        # Resume execution - LangGraph will continue from the saved checkpoint
        accumulated_state = stored_state.get("accumulated_state", {})
        workflow_interrupted = False

        try:
            # Check if we're currently at an interrupt point
            current_state = await app.aget_state(config)
            is_at_interrupt = current_state and hasattr(current_state, 'next') and len(current_state.next) > 0

            # If we're at an interrupt and resuming (which means user just approved),
            # we need to continue past it. LangGraph should do this automatically, but if it doesn't,
            # we'll handle it by checking the next nodes.
            if is_at_interrupt:
                next_nodes = current_state.next if hasattr(current_state, 'next') else []
                # If we're resuming and the next node is CodeGenerator (which we just approved),
                # we should continue past the interrupt without asking again
                if "CodeGenerator" in next_nodes:
                    logger.info(f"Resuming past approved interrupt ({next_nodes}) - continuing workflow")
                    # Clear the checkpoint to prevent loop, then continue
                    try:
                        pg_checkpointer.delete_checkpoint(primary_id, thread_id)
                        logger.info("Cleared checkpoint before continuing past approved interrupt")
                    except Exception as e:
                        logger.warning(f"Could not clear checkpoint: {e}")

            # Continue execution from checkpoint - passing None tells LangGraph to continue from saved state
            # If we're at an interrupt, LangGraph will automatically continue past it when we call astream
            # Langfuse 4.x (_detach_observation in CallbackHandler) catches and silences the
            # cross-context ValueError natively. _OtelContextCleanupFilter (logger_config.py)
            # and safe_otel_ctx (otel_guard.py) remain as defence-in-depth.
            _streamed_nodes: set = set()
            _last_streamed_node: str = ""
            async for event_type, event_data in app.astream(
                None, config=config, stream_mode=["messages", "updates"]
            ):
                # Token-level streaming
                if event_type == "messages":
                    message_chunk, metadata = event_data
                    node_name = metadata.get("langgraph_node", "")
                    if (
                        node_name and
                        node_name not in ("Supervisor", "DiagnosticsCollect", "DiagnosticsOrchestrator") and
                        hasattr(message_chunk, "content") and
                        message_chunk.content
                    ):
                        if _last_streamed_node and node_name != _last_streamed_node:
                            yield {"type": "content_delta", "data": "\n\n---\n\n"}
                        _last_streamed_node = node_name
                        _streamed_nodes.add(node_name)
                        _partial_chars += len(message_chunk.content)
                        yield {"type": "content_delta", "data": message_chunk.content}
                    continue

                if event_type != "updates":
                    continue

                for node_name, state_delta in event_data.items():
                    # If we hit an interrupt during resume, check if it's the same one we just approved
                    if node_name == "__interrupt__":
                        # If we're resuming from approval, skip this interrupt and continue
                        if config.get("configurable", {}).get("_resuming_from_approval"):
                            logger.info("Detected interrupt during resume from approval - continuing past it (already approved)")
                            # Clear the resume flag and continue past the interrupt
                            config.get("configurable", {}).pop("_resuming_from_approval", None)
                            # Use LangGraph's update_state to continue past the interrupt
                            # Get the next nodes and continue execution
                            current_state = await app.aget_state(config)
                            if current_state and hasattr(current_state, 'next') and current_state.next:
                                # Continue execution by calling astream again with updated config
                                # This will skip the interrupt and continue to the next node
                                logger.info(f"Continuing past interrupt to nodes: {current_state.next}")
                                # The interrupt will be automatically skipped when we continue
                                continue

                        current_state = await app.aget_state(config)
                        next_nodes = current_state.next if (current_state and hasattr(current_state, 'next')) else []

                        # If CodeGenerator is in next nodes and we're resuming, this is the interrupt we just approved
                        # Continue past it without asking for approval again
                        if "CodeGenerator" in next_nodes and config.get("configurable", {}).get("_resuming_from_approval"):
                            logger.info("Detected CodeGenerator interrupt during resume - continuing past it (already approved)")
                            # Don't save checkpoint or yield breakpoint - just continue
                            continue

                        # Otherwise, it's a NEW interrupt - ask for approval
                        logger.warning("New interrupt detected during resume - this may require another approval")
                        workflow_interrupted = True
                        # Save checkpoint for the new interrupt
                        pg_checkpointer.save_checkpoint(
                            primary_id, thread_id, config, {
                                "accumulated_state": accumulated_state,
                            }
                        )
                        yield {
                            "type": "breakpoint",
                            "data": create_approval_message(accumulated_state)
                        }
                        return

                    # Process state updates
                    if isinstance(state_delta, dict):
                        update_accumulated_state(accumulated_state, state_delta)

                        # Only emit per-message content for nodes that did NOT
                        # already stream token-by-token (avoids duplicate output).
                        if "messages" in state_delta and node_name not in _streamed_nodes:
                            for message in state_delta["messages"]:
                                if should_yield_message(message):
                                    content = str(message.content).strip()
                                    logger.info(f"Resume yielding content from {message.name}: {content[:100]}...")
                                    yield {"type": "content_delta", "data": content}

        except GeneratorExit:
            # Properly handle generator cleanup
            logger.debug("Workflow resume generator exited - expected behavior")
            raise  # Re-raise to properly signal generator exit

        # Clean up checkpoint if completed successfully
        if not workflow_interrupted:
            try:
                pg_checkpointer.delete_checkpoint(primary_id, thread_id)
                logger.info(f"Cleared checkpoint after successful resume for primary_id={primary_id}")
            except Exception as e:
                logger.warning(f"Could not clear checkpoint: {e}")
            try:
                await app.checkpointer.adelete_thread(thread_id)
                logger.debug(f"Cleared LangGraph graph state after resume for thread_id={thread_id}")
            except Exception as e:
                logger.warning(f"Could not clear LangGraph graph state after resume for thread_id={thread_id}: {e}")

        # Yield final message
        final_messages = accumulated_state.get("messages", [])
        final_content = extract_final_message_content(final_messages)
        agents_invoked = list({
            msg.name for msg in final_messages
            if hasattr(msg, "name") and msg.name
            and msg.name not in ("Supervisor", "System", "KubeIntellect", "SystemError")
        })
        yield {
            "type": "workflow_complete",
            "data": final_content,
            "agents": agents_invoked,
            "messages": final_messages,  # used by run_kubeintellect_workflow for context extraction
        }

    except GeneratorExit:
        # Re-raise GeneratorExit to properly handle cleanup
        raise
    except Exception as e:
        llm_err = classify_llm_error(e)
        approx_tokens = max(1, _partial_chars // 4) if _partial_chars > 0 else 0
        logger.error(
            f"LLM stream error during resume: type={llm_err.error_type}",
            exc_info=True,
            extra={"llm.error.type": llm_err.error_type, "llm.stream.partial_tokens": approx_tokens},
        )
        llm_stream_errors_total.labels(error_type=llm_err.error_type).inc()
        user_msg = llm_err.user_message
        if approx_tokens > 0:
            user_msg = f"[Response interrupted after ~{approx_tokens} tokens] {user_msg}"
        # Note: if a write operation was in progress when the stream failed,
        # the user should verify cluster state manually.
        try:
            yield {"type": "error", "data": user_msg}
        except Exception as yield_error:
            logger.critical(f"Failed to yield error message during resume: {yield_error}", exc_info=True)


async def execute_workflow_stream(
    app, pg_checkpointer, initial_state: dict, config: dict,
    primary_id: str, thread_id: str
) -> AsyncGenerator[dict, None]:
    """Execute the main workflow stream."""
    accumulated_state = dict(initial_state)
    workflow_interrupted = False
    _wf_start = _time.time()
    # Track nodes that already emitted token-level chunks so the subsequent
    # "updates" event for the same node doesn't re-emit the full content.
    _streamed_nodes: set = set()
    # Track the last node that streamed to detect agent transitions.
    _last_streamed_node: str = ""
    # Character count of content already yielded — used to add context to error
    # messages when the stream is interrupted mid-response (approx tokens ≈ chars // 4).
    _partial_chars: int = 0
    # Track whether we've already emitted a plan_preview for this workflow run.
    _plan_preview_emitted: bool = False

    # _OtelContextCleanupFilter (logger_config.py) and async_safe_otel_ctx (otel_guard.py)
    # suppress the benign "Token was created in a different Context" ValueError that can
    # surface when Langfuse spans cross asyncio Task boundaries inside the astream() generator.
    # If the ValueError escapes the generator, the `except ValueError` handler below catches it.
    # TODO(root-cause): structural fix is queue-based single-Task OTel isolation (PR 2) —
    # see company/outputs/decisions/2026-04-03-observability_logging.md.
    try:
        async for event_type, event_data in app.astream(
            initial_state, config=config, stream_mode=["messages", "updates"]
        ):
            # ----------------------------------------------------------
            # Token-level streaming: yield each LLM token as it arrives.
            # event_data is (AIMessageChunk, metadata_dict).
            # ----------------------------------------------------------
            if event_type == "messages":
                message_chunk, metadata = event_data
                node_name = metadata.get("langgraph_node", "")
                if (
                    node_name and
                    node_name not in ("Supervisor", "DiagnosticsCollect", "DiagnosticsOrchestrator") and
                    hasattr(message_chunk, "content") and
                    message_chunk.content
                ):
                    # Inject a separator when streaming switches to a different agent.
                    if _last_streamed_node and node_name != _last_streamed_node:
                        yield {"type": "content_delta", "data": "\n\n---\n\n"}
                    _last_streamed_node = node_name
                    _streamed_nodes.add(node_name)
                    _partial_chars += len(message_chunk.content)
                    yield {"type": "content_delta", "data": message_chunk.content}
                continue

            # ----------------------------------------------------------
            # Node-completion updates: handle interrupts and state.
            # event_data is {node_name: state_delta}.
            # ----------------------------------------------------------
            if event_type != "updates":
                continue

            for node_name, state_delta in event_data.items():
                logger.debug(
                    f"Workflow event from '{node_name}': "
                    f"{list(state_delta.keys()) if isinstance(state_delta, dict) else type(state_delta)}"
                )

                # Handle interrupts — CodeGenerator breakpoints pause here
                if node_name == "__interrupt__":
                    workflow_interrupted = True
                    if primary_id:
                        await save_checkpoint_state(
                            pg_checkpointer, primary_id, thread_id, config,
                            accumulated_state, hitl_type="code_generator",
                        )
                    yield {
                        "type": "breakpoint",
                        "data": create_approval_message(accumulated_state),
                    }
                    return

                # Process state updates
                if isinstance(state_delta, dict):
                    update_accumulated_state(accumulated_state, state_delta)

                    # Detect when the Supervisor commits a new plan and emit plan_preview.
                    # Fires at most once per workflow run (first plan commit only).
                    if (
                        not _plan_preview_emitted
                        and node_name == "Supervisor"
                        and (state_delta.get("task_plan") or state_delta.get("plan"))
                    ):
                        _plan_preview_emitted = True
                        _preview_text = _format_plan_preview(
                            state_delta.get("task_plan"),
                            state_delta.get("plan"),
                        )
                        if _preview_text:
                            yield {"type": "plan_preview", "data": _preview_text}

                    # Only emit per-message content for nodes that did NOT
                    # already stream token-by-token (avoids duplicate output).
                    if "messages" in state_delta and node_name not in _streamed_nodes:
                        for message in state_delta["messages"]:
                            if should_yield_message(message):
                                content = str(message.content).strip()
                                logger.info(f"Yielding content from {message.name}: {content[:100]}...")
                                yield {"type": "content_delta", "data": content}

    except GeneratorExit:
        # Properly handle generator cleanup - re-raise to signal proper exit
        logger.debug("Workflow generator exited - expected behavior")
        raise  # Re-raise GeneratorExit to properly signal generator cleanup
    except ValueError as ve:
        # Suppress the benign OTel cross-context ValueError that can surface from the
        # astream() generator when Langfuse spans cross asyncio Task boundaries.
        # Any other ValueError is treated as a real LLM error and reported with context.
        _OTEL_BENIGN = "Token was created in a different Context"
        if _OTEL_BENIGN not in str(ve):
            llm_err = classify_llm_error(ve)
            approx_tokens = max(1, _partial_chars // 4) if _partial_chars > 0 else 0
            logger.error(
                f"LLM stream error: type={llm_err.error_type}",
                exc_info=True,
                extra={"llm.error.type": llm_err.error_type, "llm.stream.partial_tokens": approx_tokens},
            )
            llm_stream_errors_total.labels(error_type=llm_err.error_type).inc()
            user_msg = llm_err.user_message
            if approx_tokens > 0:
                user_msg = f"[Response interrupted after ~{approx_tokens} tokens] {user_msg}"
            try:
                yield {"type": "error", "data": user_msg}
            except Exception as yield_error:
                logger.critical(f"Failed to yield error message during execution: {yield_error}", exc_info=True)
            return
        logger.debug(
            "otel_ctx_cleanup_suppressed",
            extra={"event": "otel_ctx_cleanup_suppressed"},
        )
    except Exception as e:
        llm_err = classify_llm_error(e)
        approx_tokens = max(1, _partial_chars // 4) if _partial_chars > 0 else 0
        logger.error(
            f"LLM stream error: type={llm_err.error_type}",
            exc_info=True,
            extra={"llm.error.type": llm_err.error_type, "llm.stream.partial_tokens": approx_tokens},
        )
        llm_stream_errors_total.labels(error_type=llm_err.error_type).inc()
        user_msg = llm_err.user_message
        if approx_tokens > 0:
            user_msg = f"[Response interrupted after ~{approx_tokens} tokens] {user_msg}"
        try:
            yield {"type": "error", "data": user_msg}
        except Exception as yield_error:
            logger.critical(f"Failed to yield error message during execution: {yield_error}", exc_info=True)
        return

    # Clean up checkpoint after successful completion
    if not workflow_interrupted and primary_id:
        try:
            pg_checkpointer.delete_checkpoint(primary_id, thread_id)
            logger.info(f"Cleared checkpoint after completion for primary_id={primary_id}")
        except Exception as e:
            logger.warning(f"Could not clear checkpoint: {e}")

    # Yield final completion message.
    # Use the actual graph state (via get_state) to capture messages that the supervisor
    # injected directly into LangGraph state (e.g. welcome/out-of-scope responses) —
    # these don't appear in stream_mode="updates" events for the Supervisor node.
    try:
        final_graph_state = await app.aget_state(config)
        if final_graph_state and final_graph_state.values:
            final_messages = list(final_graph_state.values.get("messages", []))
        else:
            final_messages = accumulated_state.get("messages", [])
    except Exception as e:
        logger.warning(f"Could not read final graph state, falling back to accumulated state: {e}")
        final_messages = accumulated_state.get("messages", [])

    # B5: Detect deletion-confirmation-pending FINISH before wiping graph state.
    # When the Deletion agent issues a confirmation prompt and the workflow returns FINISH,
    # preserve graph state so the user's "confirm" reply has context. Signal callers via
    # the workflow_complete event so they can persist the pending deletion to the DB.
    _deletion_confirmation_pending = False
    if not workflow_interrupted and final_messages:
        _last_fm = final_messages[-1]
        if (
            isinstance(_last_fm, AIMessage)
            and _last_fm.content
            and getattr(_last_fm, "name", None) == "Deletion"
        ):
            _lc_del = str(_last_fm.content).lower()
            if "confirm" in _lc_del and ("cancel" in _lc_del or "abort" in _lc_del):
                _deletion_confirmation_pending = True
                logger.info(
                    "B5: deletion-confirmation-pending detected — "
                    "skipping adelete_thread to preserve graph state for confirmation reply."
                )

    # Delete LangGraph's own graph state for this thread to prevent message accumulation
    # across subsequent requests in the same conversation. Without this, each new request
    # appends to the prior run's accumulated messages (via add_messages reducer), causing
    # the clarification-loop guard to fire spuriously on messages from previous turns.
    # Skip deletion when a deletion confirmation is pending (B5 fix).
    if not workflow_interrupted and not _deletion_confirmation_pending:
        try:
            await app.checkpointer.adelete_thread(thread_id)
            logger.debug(f"Cleared LangGraph graph state for thread_id={thread_id}")
        except Exception as e:
            logger.warning(f"Could not clear LangGraph graph state for thread_id={thread_id}: {e}")

    final_content = extract_final_message_content(final_messages)
    agents_invoked = list({
        msg.name for msg in final_messages
        if hasattr(msg, "name") and msg.name
        and msg.name not in ("Supervisor", "System", "KubeIntellect", "SystemError")
    })
    workflow_duration_seconds.observe(_time.time() - _wf_start)
    yield {
        "type": "workflow_complete",
        "data": final_content,
        "agents": agents_invoked,
        "messages": final_messages,  # used by run_kubeintellect_workflow for context extraction
        "deletion_confirmation_pending": _deletion_confirmation_pending,
    }


from app.orchestration.schemas import format_plan_preview as _format_plan_preview  # noqa: E402


def update_accumulated_state(accumulated_state: dict, state_delta: dict):
    """Update accumulated state with new state delta."""
    for key, value_delta in state_delta.items():
        if key in accumulated_state:
            if (key in ["messages", "intermediate_steps"] and
                    isinstance(accumulated_state[key], list) and
                    isinstance(value_delta, list)):
                accumulated_state[key].extend(value_delta)
            else:
                accumulated_state[key] = value_delta
        else:
            accumulated_state[key] = value_delta


def should_yield_message(message) -> bool:
    """Determine if a message should be yielded to the user."""
    return (
        isinstance(message, AIMessage) and
        message.name not in ("Supervisor", "DiagnosticsOrchestrator") and
        message.content and
        not str(message.content).strip().startswith("Agent action completed")
    )


def extract_final_message_content(messages: list) -> str:
    """Extract the final meaningful message content."""
    if not messages:
        return "Workflow processing complete. Review conversation history for details."

    # Find last meaningful message
    for message in reversed(messages):
        if (
            isinstance(message, AIMessage) and
            message.name not in ("Supervisor", "SystemError", "DiagnosticsOrchestrator") and
            message.content and
            not str(message.content).strip().startswith("Agent action completed")
        ):
            return str(message.content)

    # Fallback to last message if no meaningful one found
    if messages and isinstance(messages[-1], AIMessage) and messages[-1].content:
        return str(messages[-1].content)

    return "Workflow processing complete. Review conversation history for details."


async def save_checkpoint_state(
    pg_checkpointer, primary_id: str, thread_id: str,
    config: dict, accumulated_state: dict,
    hitl_type: str = "code_generator",
):
    """Save current workflow state to checkpoint.

    Only accumulated_state is persisted — LangGraph's own AsyncPostgresSaver holds
    the authoritative state and is re-fetched via app.aget_state() on resume.
    Storing current_state.values here caused pickle failures (_thread.RLock inside
    LangChain message objects) and was never read back on resume.
    """
    try:
        pg_checkpointer.save_checkpoint(
            primary_id, thread_id, config, {
                "accumulated_state": accumulated_state,
                "hitl_type": hitl_type,
            }
        )
        logger.info(f"Saved checkpoint for primary_id={primary_id} (hitl_type={hitl_type})")
    except Exception as e:
        logger.warning(f"Could not save checkpoint for primary_id={primary_id}: {e}")


def extract_codegen_context(accumulated_state: dict) -> str:
    """Extract a brief description of what CodeGenerator is about to do from the message history."""
    messages = accumulated_state.get("messages", [])
    # Walk backwards through messages to find the last meaningful AI/supervisor message
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if not content or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        # Skip very short messages
        if len(content) < 20:
            continue
        # Truncate long messages
        if len(content) > 500:
            content = content[:500] + "..."
        return content
    return ""


def create_approval_message(accumulated_state: dict | None = None) -> str:
    """Create the approval request message for HITL breakpoints (CodeGenerator / Apply)."""
    agent_name = "CodeGenerator"
    if accumulated_state:
        agent_name = accumulated_state.get("next", "CodeGenerator") or "CodeGenerator"

    if agent_name == "Apply":
        description = "apply synthesised YAML to the cluster"
    else:
        description = "generate new code/tools using the CodeGenerator agent"

    context_section = ""
    if accumulated_state:
        context = extract_codegen_context(accumulated_state)
        if context:
            context_section = f"**What the agent wants to do:**\n{context}\n\n"
    return (
        f"🛑 **APPROVAL REQUIRED** ({agent_name})\n\n"
        f"The workflow wants to {description}.\n\n"
        f"{context_section}"
        "**To approve:** Type 'approve', 'yes', or 'continue'\n"
        "**To deny:** Type 'deny', 'no', or 'cancel'\n\n"
        "Do you approve this action?"
    )


def create_tool_completion_message() -> str:
    """Create the tool completion message."""
    return (
        "✅ Tool registration completed!\n\n"
        "The requested tool has been created and registered. "
        "To use it, **please re-issue your request or reload the application/session** "
        "so the new tool is available to the agents."
    )
