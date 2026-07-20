# app/agents/tools/tools_lib/log_store_tools.py
"""
Loki log store tools for the LogsAgent.

These tools let KubeIntellect query the Loki HTTP API (LogQL) for historical
and aggregated logs. They complement get_pod_logs (live K8s API) with:
  - Historical logs from pods that have since restarted or been deleted
  - Cross-pod log aggregation by namespace or label
  - Kubernetes lifecycle event logs from kubernetes-event-exporter

Loki must be deployed first: make install-loki-kind
"""

from typing import Optional

import requests
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.core.config import settings
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ─── Input schemas ────────────────────────────────────────────────────────────

class QueryLokiLogsInput(BaseModel):
    namespace: Optional[str] = Field(
        default=None,
        description="Kubernetes namespace to filter logs. Omit to search all namespaces.",
    )
    pod_name: Optional[str] = Field(
        default=None,
        description="Pod name (exact or substring). Omit to search all pods in the namespace.",
    )
    app_label: Optional[str] = Field(
        default=None,
        description="Value of the 'app' label. Use this to query all replicas of a deployment.",
    )
    search_text: Optional[str] = Field(
        default=None,
        description="Text to search for in log lines (case-sensitive substring filter).",
    )
    start_time: str = Field(
        default="1h ago",
        description="Start of the time range. Examples: '1h ago', '30m ago', '2026-03-26T00:00:00Z'.",
    )
    end_time: str = Field(
        default="now",
        description="End of the time range. Use 'now' for the current time.",
    )
    limit: int = Field(
        default=100,
        description="Maximum number of log lines to return (1–500).",
        ge=1,
        le=500,
    )


class QueryLokiEventsInput(BaseModel):
    namespace: Optional[str] = Field(
        default=None,
        description="Kubernetes namespace to filter events. Omit for all namespaces.",
    )
    reason: Optional[str] = Field(
        default=None,
        description=(
            "K8s event reason to filter on. Common values: "
            "'OOMKilling', 'BackOff', 'FailedScheduling', 'Killing', 'Pulled', 'Started'."
        ),
    )
    start_time: str = Field(
        default="1h ago",
        description="Start of the time range. Examples: '1h ago', '24h ago'.",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Maximum number of events to return.",
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _resolve_time_ns(ts: str) -> str:
    """Convert relative time strings to nanosecond Unix timestamps for Loki."""
    import time as _time
    now_ns = int(_time.time() * 1e9)
    if ts == "now":
        return str(now_ns)
    _unit_map = {"s": int(1e9), "m": 60 * int(1e9), "h": 3600 * int(1e9), "d": 86400 * int(1e9)}
    ts_clean = ts.replace(" ago", "").strip()
    if len(ts_clean) > 1 and ts_clean[-1] in _unit_map and ts_clean[:-1].isdigit():
        amount = int(ts_clean[:-1])
        return str(now_ns - amount * _unit_map[ts_clean[-1]])
    # Assume RFC3339 — return as-is; Loki accepts both
    return ts


# ─── Tool functions ───────────────────────────────────────────────────────────

def query_loki_logs(
    namespace: Optional[str] = None,
    pod_name: Optional[str] = None,
    app_label: Optional[str] = None,
    search_text: Optional[str] = None,
    start_time: str = "1h ago",
    end_time: str = "now",
    limit: int = 100,
) -> str:
    """
    Query historical logs from Loki using LogQL.

    Builds a LogQL stream selector from the provided filters and returns
    up to `limit` log lines sorted newest-first.
    """
    # Build stream selector
    selectors = []
    if namespace:
        selectors.append(f'namespace="{namespace}"')
    if app_label:
        selectors.append(f'app="{app_label}"')
    elif pod_name:
        selectors.append(f'pod=~".*{pod_name}.*"')

    if not selectors:
        selectors = ['job=~".+"']  # match all streams as a fallback

    stream_selector = "{" + ", ".join(selectors) + "}"

    # Add line filter
    if search_text:
        logql = f'{stream_selector} |= `{search_text}`'
    else:
        logql = stream_selector

    url = f"{settings.LOKI_URL.rstrip('/')}/loki/api/v1/query_range"
    params = {
        "query": logql,
        "start": _resolve_time_ns(start_time),
        "end": _resolve_time_ns(end_time),
        "limit": min(limit, 500),
        "direction": "backward",
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        streams = data.get("data", {}).get("result", [])
        if not streams:
            return (
                f"No logs found in Loki for query: {logql}\n"
                f"Time range: {start_time} → {end_time}\n"
                "Check that Promtail is running and Loki is deployed (make install-loki-kind)."
            )

        lines = [f"Loki query: {logql}", f"Time range: {start_time} → {end_time}", ""]
        total_lines = 0
        for stream in streams:
            stream_labels = stream.get("stream", {})
            label_str = ", ".join(f"{k}={v}" for k, v in stream_labels.items())
            lines.append(f"--- Stream: {label_str} ---")
            for ts, line in stream.get("values", []):
                lines.append(line)
                total_lines += 1
            lines.append("")

        lines.insert(2, f"Returned {total_lines} log lines across {len(streams)} stream(s).")
        return "\n".join(lines)

    except requests.exceptions.ConnectionError:
        return (
            f"Error: Cannot reach Loki at {settings.LOKI_URL}. "
            "Ensure loki-stack is deployed (make install-loki-kind)."
        )
    except requests.exceptions.Timeout:
        return "Error: Loki query timed out. Try a shorter time range or smaller limit."
    except Exception as e:
        return f"Error: {e}"


def query_loki_events(
    namespace: Optional[str] = None,
    reason: Optional[str] = None,
    start_time: str = "1h ago",
    limit: int = 50,
) -> str:
    """
    Query Kubernetes lifecycle events from Loki.

    Events are shipped by kubernetes-event-exporter (deployed via make install-event-exporter-kind).
    Unlike kubectl get events, Loki retains events beyond the default 1-hour K8s TTL.
    """
    selectors = ['app="event-exporter"']
    if namespace:
        selectors.append(f'namespace="{namespace}"')

    stream_selector = "{" + ", ".join(selectors) + "}"

    # Filter for specific reason if requested
    logql = stream_selector
    if reason:
        logql += f' |= `"reason":"{reason}"'

    url = f"{settings.LOKI_URL.rstrip('/')}/loki/api/v1/query_range"
    params = {
        "query": logql,
        "start": _resolve_time_ns(start_time),
        "end": _resolve_time_ns("now"),
        "limit": min(limit, 200),
        "direction": "backward",
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        streams = data.get("data", {}).get("result", [])
        if not streams:
            return (
                f"No events found in Loki.\n"
                f"Namespace filter: {namespace or 'all'}, Reason filter: {reason or 'all'}\n"
                "Check that kubernetes-event-exporter is running (make install-event-exporter-kind)."
            )

        import json as _json
        lines = ["Kubernetes events from Loki (via event-exporter)", f"Time range: {start_time} → now", ""]
        total = 0
        for stream in streams:
            for ts, line in stream.get("values", []):
                try:
                    evt = _json.loads(line)
                    ns = evt.get("namespace", "")
                    evt_reason = evt.get("reason", "")
                    message = evt.get("message", "")
                    involved = evt.get("involvedObject", {})
                    kind = involved.get("kind", "")
                    obj_name = involved.get("name", "")
                    lines.append(f"[{ns}/{kind}/{obj_name}] {evt_reason}: {message}")
                except Exception:
                    lines.append(line)
                total += 1

        lines.insert(2, f"Returned {total} events.")
        return "\n".join(lines)

    except requests.exceptions.ConnectionError:
        return (
            f"Error: Cannot reach Loki at {settings.LOKI_URL}. "
            "Ensure loki-stack is deployed (make install-loki-kind)."
        )
    except Exception as e:
        return f"Error: {e}"


# ─── Tool instances ───────────────────────────────────────────────────────────

query_loki_logs_tool = StructuredTool.from_function(
    func=query_loki_logs,
    name="query_loki_logs",
    description=(
        "Query historical logs from Loki for any pod or namespace. "
        "Unlike get_pod_logs, this works even if the pod has restarted or been deleted. "
        "Supports filtering by namespace, pod name, app label, and search text. "
        "Requires Loki to be deployed (make install-loki-kind)."
    ),
    args_schema=QueryLokiLogsInput,
)

query_loki_events_tool = StructuredTool.from_function(
    func=query_loki_events,
    name="query_loki_events",
    description=(
        "Query historical Kubernetes lifecycle events from Loki (via kubernetes-event-exporter). "
        "Events persist beyond the default 1-hour K8s TTL. "
        "Useful for debugging OOMKills, scheduling failures, CrashLoopBackOffs that happened in the past. "
        "Filter by namespace and/or event reason (e.g. 'OOMKilling', 'BackOff', 'FailedScheduling')."
    ),
    args_schema=QueryLokiEventsInput,
)

log_store_tools = [query_loki_logs_tool, query_loki_events_tool]
