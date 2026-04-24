"""
query_prometheus — instant and range PromQL queries against the cluster Prometheus.

If range_minutes=0 (default): instant query against /api/v1/query.
If range_minutes>0:           range query against /api/v1/query_range,
                              returning min/avg/max per series over that window.

Output is capped at 6 000 chars to stay within LLM context budgets.
"""
from __future__ import annotations

import time

import httpx
from langchain_core.tools import tool

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_OUTPUT_CAP = 6_000
_INSTANT_SERIES_CAP = 50
_RANGE_SERIES_CAP = 20


def _fmt_instant(query: str, results: list[dict]) -> str:
    lines = [f"PromQL (instant): {query}", f"Series: {len(results)}"]
    for r in results[:_INSTANT_SERIES_CAP]:
        labels = ", ".join(f"{k}={v}" for k, v in r.get("metric", {}).items())
        value = r.get("value", [None, "N/A"])[1]
        lines.append(f"  [{labels}] = {value}")
    if len(results) > _INSTANT_SERIES_CAP:
        lines.append(f"  ... {len(results) - _INSTANT_SERIES_CAP} more series (narrow the query)")
    return "\n".join(lines)


def _fmt_range(query: str, range_minutes: int, results: list[dict]) -> str:
    lines = [
        f"PromQL (range {range_minutes}m): {query}",
        f"Series: {len(results)}",
    ]
    for r in results[:_RANGE_SERIES_CAP]:
        labels = ", ".join(f"{k}={v}" for k, v in r.get("metric", {}).items())
        values = [float(v[1]) for v in r.get("values", []) if v[1] not in ("NaN", "+Inf", "-Inf")]
        if values:
            lines.append(
                f"  [{labels}]  min={min(values):.4f}  "
                f"avg={sum(values)/len(values):.4f}  max={max(values):.4f}"
            )
        else:
            lines.append(f"  [{labels}] no numeric values")
    if len(results) > _RANGE_SERIES_CAP:
        lines.append(f"  ... {len(results) - _RANGE_SERIES_CAP} more series")
    return "\n".join(lines)


@tool
def query_prometheus(promql: str, range_minutes: int = 0) -> str:
    """Query Prometheus for cluster metrics using PromQL.

    Args:
        promql: A PromQL expression. Examples:
            sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)
            kube_pod_status_phase{namespace="production",phase="Running"}
            container_memory_working_set_bytes{pod=~"payment-api.*"}
            kube_deployment_status_replicas_unavailable{namespace="staging"}
        range_minutes: If 0 (default), runs an instant query returning current values.
            If >0, runs a range query over the last N minutes and returns
            min/avg/max per series. Only use range_minutes>0 when the user
            explicitly asks about historical data or trends — not for current status.
            Example: range_minutes=60 → last hour of data.

    Returns:
        Formatted metric results, capped at 6 000 characters.
    """
    if not settings.PROMETHEUS_URL:
        return "Prometheus is not configured. Set PROMETHEUS_URL in ~/.kubeintellect/.env and restart."

    prom_url = settings.PROMETHEUS_URL.strip()
    if not prom_url.startswith(("http://", "https://")):
        prom_url = f"http://{prom_url}"
        logger.warning(f"PROMETHEUS_URL missing protocol — using {prom_url}")
    base_url = prom_url.rstrip("/")
    logger.debug(f"query_prometheus: {promql!r} range_minutes={range_minutes}")

    try:
        with httpx.Client(timeout=15.0) as client:
            if range_minutes <= 0:
                resp = client.get(
                    f"{base_url}/api/v1/query",
                    params={"query": promql},
                )
            else:
                now = int(time.time())
                resp = client.get(
                    f"{base_url}/api/v1/query_range",
                    params={
                        "query": promql,
                        "start": now - range_minutes * 60,
                        "end": now,
                        "step": _auto_step(range_minutes),
                    },
                )

        if resp.status_code != 200:
            return f"Prometheus HTTP {resp.status_code}: {resp.text[:500]}"

        data = resp.json()
        if data.get("status") != "success":
            return f"Prometheus error: {data.get('error', 'unknown')}"

        results = data.get("data", {}).get("result", [])
        if not results:
            return f"No data for query: {promql}"

        output = (
            _fmt_instant(promql, results)
            if range_minutes <= 0
            else _fmt_range(promql, range_minutes, results)
        )

    except httpx.ConnectError:
        return (
            f"Cannot reach Prometheus at {base_url}. "
            "Is kube-prometheus-stack deployed? (make install-prometheus-kind)"
        )
    except httpx.TimeoutException:
        return "Prometheus query timed out (15s)."
    except Exception as exc:
        return f"Prometheus error: {exc}"

    if len(output) > _OUTPUT_CAP:
        output = output[:_OUTPUT_CAP] + f"\n... [truncated — use a more specific query]"
    return output


def _auto_step(range_minutes: int) -> str:
    """Choose a sensible step size so the response has ~100 data points."""
    seconds_per_point = max((range_minutes * 60) // 100, 15)
    if seconds_per_point < 60:
        return f"{seconds_per_point}s"
    return f"{seconds_per_point // 60}m"
