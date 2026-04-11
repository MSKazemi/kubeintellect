# app/agents/tools/tools_lib/prometheus_query_tools.py
"""
Prometheus query tools for the MetricsAgent.

These tools let KubeIntellect query the Prometheus HTTP API for real-time and
historical metrics (PromQL). They complement the existing kubectl-top based
metrics_tools.py with time-range queries and aggregate metrics.

Prometheus must be deployed first: make install-prometheus-kind
"""

from typing import Optional

import requests
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.core.config import settings
from app.utils.logger_config import setup_logging

logger = setup_logging(app_name="kubeintellect")


# ─── Input schemas ────────────────────────────────────────────────────────────

class QueryPrometheusInput(BaseModel):
    query: str = Field(
        ...,
        description=(
            "PromQL query string. Examples: "
            "'sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)', "
            "'kube_pod_status_phase{phase=\"Running\"}'"
        ),
    )
    time_range: Optional[str] = Field(
        default=None,
        description=(
            "Optional look-back duration for instant queries (e.g. '5m', '1h', '24h'). "
            "When set, the query is wrapped in a rate() or avg_over_time() context hint — "
            "the caller is responsible for including the range in the PromQL expression."
        ),
    )


class QueryPrometheusRangeInput(BaseModel):
    query: str = Field(..., description="PromQL query string.")
    start: str = Field(
        ...,
        description="Start time in RFC3339 or Unix timestamp. Examples: '2026-03-26T00:00:00Z', '1h ago'.",
    )
    end: str = Field(
        ...,
        description="End time in RFC3339 or Unix timestamp. Use 'now' for the current time.",
    )
    step: str = Field(
        default="60s",
        description="Query resolution step (e.g. '60s', '5m', '1h').",
    )


# ─── Tool functions ───────────────────────────────────────────────────────────

def query_prometheus(query: str, time_range: Optional[str] = None) -> str:
    """
    Execute an instant PromQL query against Prometheus and return results.

    Returns a formatted string with the metric results or an error message.
    """
    url = f"{settings.PROMETHEUS_URL.rstrip('/')}/api/v1/query"
    params = {"query": query}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return f"Error: Prometheus returned status '{data.get('status')}': {data.get('error', 'unknown error')}"

        results = data.get("data", {}).get("result", [])
        if not results:
            return f"No data returned for query: {query}"

        lines = [f"Prometheus query: {query}", f"Results ({len(results)} series):"]
        for r in results[:50]:  # cap at 50 series to avoid prompt overflow
            metric_labels = ", ".join(f"{k}={v}" for k, v in r.get("metric", {}).items())
            value = r.get("value", [None, "N/A"])[1]
            lines.append(f"  [{metric_labels}] = {value}")
        if len(results) > 50:
            lines.append(f"  ... and {len(results) - 50} more series (narrow the query to see all)")
        return "\n".join(lines)

    except requests.exceptions.ConnectionError:
        return (
            f"Error: Cannot reach Prometheus at {settings.PROMETHEUS_URL}. "
            "Ensure kube-prometheus-stack is deployed (make install-prometheus-kind)."
        )
    except requests.exceptions.Timeout:
        return "Error: Prometheus query timed out."
    except Exception as e:
        return f"Error: {e}"


def query_prometheus_range(query: str, start: str, end: str, step: str = "60s") -> str:
    """
    Execute a range PromQL query against Prometheus and return a summary.

    Returns aggregated min/max/avg per series over the time range.
    """
    url = f"{settings.PROMETHEUS_URL.rstrip('/')}/api/v1/query_range"

    # Resolve relative start/end times
    import time as _time
    now = int(_time.time())
    _duration_map = {"m": 60, "h": 3600, "d": 86400}

    def _resolve(ts: str) -> str:
        if ts == "now":
            return str(now)
        if ts.endswith((" ago",)) or (len(ts) > 1 and ts[-1] in _duration_map and ts[:-1].isdigit()):
            ts_clean = ts.replace(" ago", "").strip()
            unit = ts_clean[-1]
            amount = int(ts_clean[:-1])
            return str(now - amount * _duration_map.get(unit, 1))
        return ts

    params = {
        "query": query,
        "start": _resolve(start),
        "end": _resolve(end) if end != "now" else str(now),
        "step": step,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return f"Error: Prometheus returned status '{data.get('status')}': {data.get('error', 'unknown error')}"

        results = data.get("data", {}).get("result", [])
        if not results:
            return f"No data returned for range query: {query} [{start} → {end}]"

        lines = [f"Prometheus range query: {query}", f"Period: {start} → {end} (step={step})", f"Series: {len(results)}"]
        for r in results[:20]:
            metric_labels = ", ".join(f"{k}={v}" for k, v in r.get("metric", {}).items())
            values = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            if values:
                lines.append(
                    f"  [{metric_labels}] min={min(values):.4f} avg={sum(values)/len(values):.4f} max={max(values):.4f}"
                )
            else:
                lines.append(f"  [{metric_labels}] no numeric values")
        if len(results) > 20:
            lines.append(f"  ... and {len(results) - 20} more series")
        return "\n".join(lines)

    except requests.exceptions.ConnectionError:
        return (
            f"Error: Cannot reach Prometheus at {settings.PROMETHEUS_URL}. "
            "Ensure kube-prometheus-stack is deployed (make install-prometheus-kind)."
        )
    except Exception as e:
        return f"Error: {e}"


# ─── Tool instances ───────────────────────────────────────────────────────────

query_prometheus_tool = StructuredTool.from_function(
    func=query_prometheus,
    name="query_prometheus",
    description=(
        "Execute an instant PromQL query against Prometheus. "
        "Use for current CPU/memory usage, error rates, pod counts, and any metric "
        "available in the cluster. Returns up to 50 labeled series with their current values."
    ),
    args_schema=QueryPrometheusInput,
)

query_prometheus_range_tool = StructuredTool.from_function(
    func=query_prometheus_range,
    name="query_prometheus_range",
    description=(
        "Execute a range PromQL query against Prometheus over a time window. "
        "Returns min/avg/max per series. Use for trend analysis, capacity planning, "
        "or spotting spikes. Example start: '1h ago', end: 'now', step: '5m'."
    ),
    args_schema=QueryPrometheusRangeInput,
)

prometheus_query_tools = [query_prometheus_tool, query_prometheus_range_tool]
