# app/orchestration/diagnostics.py
"""
DiagnosticsOrchestrator — parallel multi-signal debugging node.

Architecture (LangGraph Send API fan-out):

  Supervisor
    → DiagnosticsOrchestrator        (extract query params)
      → [Send] DiagnosticsLogs       (parallel)
      → [Send] DiagnosticsMetrics    (parallel)
      → [Send] DiagnosticsEvents     (parallel)
        → DiagnosticsCollect         (barrier — runs after all three complete)
          → Supervisor

Each signal node calls Kubernetes tool functions directly (no LLM round-trip),
with a per-signal timeout. Any signal failure (timeout, tool error, missing tool)
surfaces as a structured error in DiagnosticsResult — never silently dropped.
"""

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Send

from app.orchestration.schemas import DiagnosticsResult, SignalResult
from app.utils.logger_config import setup_logging
from app.utils.metrics import agent_invocations_total

logger = setup_logging(app_name="kubeintellect")

_NAMESPACE_RE = re.compile(
    r"\b(?:namespace|ns)[:\s=/]+([a-z0-9][a-z0-9\-]*)", re.IGNORECASE
)
_POD_RE = re.compile(
    r"\bpods?[:/=]+([a-z0-9][a-z0-9\-\.]*)", re.IGNORECASE
)
_SIGNAL_TIMEOUT_S = 15.0   # per-signal wall-clock timeout


# ---------------------------------------------------------------------------
# Query context extraction helpers
# ---------------------------------------------------------------------------

def _extract_params(messages: list) -> Dict[str, Optional[str]]:
    """
    Extract namespace and optional pod_name from the latest HumanMessage.
    Falls back to "default" namespace when none is found.
    """
    query = ""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            query = str(msg.content)
            break

    ns_match = _NAMESPACE_RE.search(query)
    pod_match = _POD_RE.search(query)

    return {
        "namespace": ns_match.group(1) if ns_match else "default",
        "pod_name": pod_match.group(1) if pod_match else None,
        "original_query": query,
    }


# ---------------------------------------------------------------------------
# DiagnosticsOrchestrator — fan-out setup node
# ---------------------------------------------------------------------------

def diagnostics_orchestrator_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract query context and prepare state for signal fan-out.
    The actual parallel dispatch happens via the diagnostics_fan_out conditional edge.
    """
    params = _extract_params(state.get("messages", []))
    logger.info(
        "DiagnosticsOrchestrator: extracting params",
        extra={"namespace": params["namespace"], "pod_name": params["pod_name"]},
    )
    return {
        # Serialise params so signal sub-nodes can parse them from state.
        "diagnostics_query": json.dumps(params),
        # Clear any results from a prior DiagnosticsOrchestrator invocation in this
        # conversation — signal nodes will overwrite these with fresh data.
        "diagnostics_logs_result": None,
        "diagnostics_metrics_result": None,
        "diagnostics_events_result": None,
    }


def diagnostics_fan_out(state: Dict[str, Any]) -> List[Send]:
    """
    Conditional edge function — dispatches to three signal sub-nodes in parallel
    using LangGraph's Send API.  Each sub-node receives the diagnostics_query so
    it knows which namespace / pod to query.
    """
    dq = state.get("diagnostics_query")
    return [
        Send("DiagnosticsLogs",    {"diagnostics_query": dq}),
        Send("DiagnosticsMetrics", {"diagnostics_query": dq}),
        Send("DiagnosticsEvents",  {"diagnostics_query": dq}),
    ]


# ---------------------------------------------------------------------------
# Signal sub-nodes (async — enables per-signal timeout via asyncio.wait_for)
# ---------------------------------------------------------------------------

async def diagnostics_logs_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch pod logs or list error pods for the given namespace.
    Writes result to diagnostics_logs_result in main graph state.
    """
    params = json.loads(state.get("diagnostics_query") or "{}")
    namespace = params.get("namespace", "default")
    pod_name = params.get("pod_name")

    try:
        from app.agents.tools.tools_lib.pod_tools import get_pod_logs, list_error_pods

        loop = asyncio.get_event_loop()
        if pod_name:
            raw = await asyncio.wait_for(
                loop.run_in_executor(
                    None, lambda: get_pod_logs(pod_name, namespace, tail_lines=50)
                ),
                timeout=_SIGNAL_TIMEOUT_S,
            )
        else:
            raw_dict = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: list_error_pods(namespace)),
                timeout=_SIGNAL_TIMEOUT_S,
            )
            raw = json.dumps(raw_dict) if isinstance(raw_dict, dict) else str(raw_dict)

        result = {"signal": "logs", "success": True, "data": str(raw)[:2000], "error": None}
        logger.info("DiagnosticsLogs: success", extra={"namespace": namespace, "pod": pod_name})
    except asyncio.TimeoutError:
        result = {
            "signal": "logs", "success": False, "data": None,
            "error": f"Timeout after {_SIGNAL_TIMEOUT_S}s",
        }
        logger.warning("DiagnosticsLogs: timeout", extra={"namespace": namespace})
    except Exception as exc:
        result = {"signal": "logs", "success": False, "data": None, "error": str(exc)}
        logger.warning("DiagnosticsLogs: error", extra={"namespace": namespace, "error": str(exc)})

    return {"diagnostics_logs_result": result}


async def diagnostics_metrics_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch CPU + memory usage for pods in the given namespace.
    Writes result to diagnostics_metrics_result in main graph state.
    """
    params = json.loads(state.get("diagnostics_query") or "{}")
    namespace = params.get("namespace", "default")

    try:
        from app.agents.tools.tools_lib.metrics_tools import (
            get_pod_cpu_usage,
            get_pod_memory_usage,
        )

        loop = asyncio.get_event_loop()
        cpu_raw, mem_raw = await asyncio.wait_for(
            asyncio.gather(
                loop.run_in_executor(None, lambda: get_pod_cpu_usage(namespace)),
                loop.run_in_executor(None, lambda: get_pod_memory_usage(namespace)),
            ),
            timeout=_SIGNAL_TIMEOUT_S,
        )

        combined = (
            f"CPU usage:\n{json.dumps(cpu_raw) if isinstance(cpu_raw, dict) else str(cpu_raw)}\n\n"
            f"Memory usage:\n{json.dumps(mem_raw) if isinstance(mem_raw, dict) else str(mem_raw)}"
        )
        result = {"signal": "metrics", "success": True, "data": combined[:2000], "error": None}
        logger.info("DiagnosticsMetrics: success", extra={"namespace": namespace})
    except asyncio.TimeoutError:
        result = {
            "signal": "metrics", "success": False, "data": None,
            "error": f"Timeout after {_SIGNAL_TIMEOUT_S}s",
        }
        logger.warning("DiagnosticsMetrics: timeout", extra={"namespace": namespace})
    except Exception as exc:
        result = {"signal": "metrics", "success": False, "data": None, "error": str(exc)}
        logger.warning("DiagnosticsMetrics: error", extra={"namespace": namespace, "error": str(exc)})

    return {"diagnostics_metrics_result": result}


async def diagnostics_events_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fetch warning events for the given namespace.
    Writes result to diagnostics_events_result in main graph state.
    """
    params = json.loads(state.get("diagnostics_query") or "{}")
    namespace = params.get("namespace", "default")

    try:
        from app.agents.tools.tools_lib.namespace_tools import get_namespace_warning_events

        loop = asyncio.get_event_loop()
        raw_dict = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: get_namespace_warning_events(namespace)),
            timeout=_SIGNAL_TIMEOUT_S,
        )
        raw = json.dumps(raw_dict) if isinstance(raw_dict, dict) else str(raw_dict)
        result = {"signal": "events", "success": True, "data": raw[:2000], "error": None}
        logger.info("DiagnosticsEvents: success", extra={"namespace": namespace})
    except asyncio.TimeoutError:
        result = {
            "signal": "events", "success": False, "data": None,
            "error": f"Timeout after {_SIGNAL_TIMEOUT_S}s",
        }
        logger.warning("DiagnosticsEvents: timeout", extra={"namespace": namespace})
    except Exception as exc:
        result = {"signal": "events", "success": False, "data": None, "error": str(exc)}
        logger.warning("DiagnosticsEvents: error", extra={"namespace": namespace, "error": str(exc)})

    return {"diagnostics_events_result": result}


# ---------------------------------------------------------------------------
# DiagnosticsCollect — barrier / aggregation node
# ---------------------------------------------------------------------------

def diagnostics_collect_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Runs after all three signal sub-nodes complete (LangGraph barrier).
    Assembles a structured DiagnosticsResult from the three signal results.
    Any missing signal (None state key) is surfaced as a structured error —
    never silently dropped.
    """
    params = json.loads(state.get("diagnostics_query") or "{}")
    namespace = params.get("namespace", "default")
    pod_name = params.get("pod_name")

    signals: List[SignalResult] = []
    for state_key, signal_name in [
        ("diagnostics_logs_result",    "logs"),
        ("diagnostics_metrics_result", "metrics"),
        ("diagnostics_events_result",  "events"),
    ]:
        raw = state.get(state_key)
        if raw is None:
            # Signal node did not write a result (e.g. uncaught exception in node).
            # Surface as structured error per the decision contract.
            signals.append(SignalResult(
                signal=signal_name,
                success=False,
                data=None,
                error=f"{signal_name} signal not collected — node may have failed silently",
            ))
            logger.error(
                "DiagnosticsCollect: signal result missing",
                extra={"signal": signal_name, "namespace": namespace},
            )
        else:
            signals.append(SignalResult(**raw))

    partial_failure = any(not s.success for s in signals)
    overall_success = any(s.success for s in signals)

    diag_result = DiagnosticsResult(
        agent_name="DiagnosticsOrchestrator",
        success=overall_success,
        raw_output="",   # filled after building the message
        namespace=namespace,
        pod_name=pod_name,
        signals=signals,
        partial_failure=partial_failure,
    )
    summary = diag_result.to_supervisor_message()
    diag_result = diag_result.model_copy(update={"raw_output": summary})

    # Merge into agent_results (preserve results from other agents in this turn).
    existing_results: dict = state.get("agent_results") or {}
    updated_results = {**existing_results, "DiagnosticsOrchestrator": diag_result.model_dump()}

    # Append step summary (capped at 20 entries, matching worker_node_factory convention).
    existing_steps: list = list(state.get("steps_taken") or [])
    step_entry = (
        f"DiagnosticsOrchestrator: parallel diagnostics for namespace={namespace}"
        + (f" pod={pod_name}" if pod_name else "")
        + (" [partial failure]" if partial_failure else "")
    )
    updated_steps = (existing_steps + [step_entry])[-20:]

    agent_invocations_total.labels(agent="DiagnosticsOrchestrator").inc()

    logger.info(
        "DiagnosticsCollect: aggregated",
        extra={
            "namespace": namespace,
            "pod": pod_name,
            "partial_failure": partial_failure,
            "signals_ok": sum(1 for s in signals if s.success),
        },
    )

    return {
        "messages": [AIMessage(content=summary, name="DiagnosticsOrchestrator")],
        "agent_results": updated_results,
        # DiagnosticsOrchestrator is always a triage step, never the final step.
        # Never set task_complete=True so the supervisor always continues with
        # targeted follow-up investigation based on the triage findings.
        "task_complete": False,
        "steps_taken": updated_steps,
    }
