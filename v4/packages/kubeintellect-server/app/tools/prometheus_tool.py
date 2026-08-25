"""
query_prometheus — instant and range PromQL queries against the cluster Prometheus.

If range_minutes=0 (default): instant query against /api/v1/query.
If range_minutes>0:           range query against /api/v1/query_range,
                              returning min/avg/max per series over that window.

Output is capped at 6 000 chars to stay within LLM context budgets.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from langchain_core.tools import tool

from app.core.config import settings
from app.tools.output_policy import mark_unavailable
from app.tools.namespace_guard import (
    all_withheld_message,
    blocked_namespace_in_query,
    drop_blocked_series,
    protected_message,
    withheld_note,
)
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


def _fmt_range(query: str, span: str, results: list[dict]) -> str:
    lines = [
        f"PromQL ({span}): {query}",
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


def _fmt_scalar(query: str, result_type: str, raw: Any) -> str:
    """Render a `scalar`/`string` result — a bare `[timestamp, "value"]` pair."""
    if isinstance(raw, list) and len(raw) == 2:
        return f"PromQL ({result_type}): {query}\n  = {raw[1]}"
    return f"PromQL ({result_type}): {query}\n  (unreadable {result_type} result)"


def _base_url() -> str | None:
    """Normalise PROMETHEUS_URL to a scheme-qualified base, or None if unset."""
    if not settings.PROMETHEUS_URL:
        return None
    prom_url = settings.PROMETHEUS_URL.strip()
    if not prom_url.startswith(("http://", "https://")):
        prom_url = f"http://{prom_url}"
        logger.warning(f"PROMETHEUS_URL missing protocol — using {prom_url}")
    return prom_url.rstrip("/")


def _query_typed(promql: str, range_minutes: int) -> tuple[str, Any, str | None]:
    """Run a Prometheus query and return (resultType, raw result, error-string-or-None).

    **Prometheus says what shape it returned; nothing here may guess it.** `/api/v1/query`
    answers with a `vector` for an ordinary instant query, a **`matrix`** when the expression
    carries a range selector (`metric[5m]` — the docstring's own examples use that form), and a
    `scalar`/`string` for `time()`, `scalar(...)` or a string literal. The last two are not a
    list of series at all: the result is a bare `[timestamp, "value"]` pair.

    Fixed 2026-08-20 — `range_minutes` used to pick the renderer, so a matrix from an instant
    query was read with `r["value"]` (which a matrix entry does not have) and every series
    printed `= N/A` over live data, while a scalar reached the namespace filter and raised
    `AttributeError: 'int' object has no attribute 'get'`.

    Never raises — errors come back as the third tuple element.
    """
    base_url = _base_url()
    if base_url is None:
        return "", None, mark_unavailable(
            "Prometheus is not configured. Set PROMETHEUS_URL in ~/.kubeintellect/.env and restart.",
            "Prometheus is not configured.",
        )
    logger.debug(f"query_prometheus: {promql!r} range_minutes={range_minutes}")
    try:
        with httpx.Client(timeout=15.0) as client:
            if range_minutes <= 0:
                resp = client.get(f"{base_url}/api/v1/query", params={"query": promql})
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
            return "", None, f"Prometheus HTTP {resp.status_code}: {resp.text[:500]}"
        data = resp.json()
        if data.get("status") != "success":
            return "", None, f"Prometheus error: {data.get('error', 'unknown')}"
        payload = data.get("data", {})
        return str(payload.get("resultType", "")), payload.get("result", []), None
    except httpx.ConnectError:
        return "", None, mark_unavailable(
            f"Cannot reach Prometheus at {base_url}. "
            "Is kube-prometheus-stack deployed? (make install-prometheus-kind)",
            "Prometheus cannot be reached.",
        )
    except httpx.TimeoutException:
        return "", None, "Prometheus query timed out (15s)."
    except Exception as exc:
        return "", None, f"Prometheus error: {exc}"


def _query_raw(promql: str, range_minutes: int) -> tuple[list[dict], str | None]:
    """(result series, error-string-or-None) — for consumers that project labelled series.

    A `scalar`/`string` answer is reported as an **error**, not as an empty list: these callers
    feed verdicts, and "no series" and "an answer of a shape I cannot project" are different
    facts (the lens of pass 79). The detector engine's trend predicates always use range
    queries, so this can only be reached by a hand-written scalar expression.
    """
    result_type, result, error = _query_typed(promql, range_minutes)
    if error is not None:
        return [], error
    if not isinstance(result, list) or any(not isinstance(r, dict) for r in result):
        return [], (
            f"Prometheus returned a '{result_type or 'unknown'}' result, which carries no "
            "labelled series. Wrap the expression so it yields a vector or matrix."
        )
    return result, None


def query_prometheus_series(promql: str, range_minutes: int) -> tuple[list[dict], str | None]:
    """Result series **and** the error, for deterministic (non-LLM) consumers.

    Returns the Prometheus `data.result` list (each series has `metric` labels and `values`
    [[ts, "value"], ...]) together with an error string, or None when the query really ran.
    "No data" and "I could not ask" are different answers and this is where a detector gets
    to tell them apart — `[]` alone cannot.
    """
    return _query_raw(promql, range_minutes)


def query_prometheus_range_raw(promql: str, range_minutes: int) -> list[dict]:
    """Series only, error discarded. **Never decide health with this.**

    Kept for callers that genuinely only want data. An unreachable, unconfigured or erroring
    Prometheus is indistinguishable here from a healthy cluster with nothing to report — which
    is exactly how a security detector came to read `sandbox_escape_attempts = 0` off a
    connection refusal. Use `query_prometheus_series` anywhere the answer feeds a verdict.
    """
    results, _err = query_prometheus_series(promql, range_minutes)
    return results


@tool
def query_prometheus(promql: str, range_minutes: int = 0) -> str:
    """Query Prometheus for cluster metrics using PromQL.

    NOT for: events, warnings, resource specs (limits/requests), endpoints, or
    "is this service reachable". Those are kubectl questions. Prometheus stores
    *metrics over time* — usage, rates, restart counts — never spec or events.

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
    blocked_ns = blocked_namespace_in_query(promql)
    if blocked_ns:
        logger.warning(
            f"query_prometheus: refused — query selects blocked namespace {blocked_ns!r}"
        )
        return protected_message(blocked_ns)

    result_type, raw, error = _query_typed(promql, range_minutes)
    if error is not None:
        return error

    # A scalar/string answer is a single `[timestamp, "value"]` pair, not a series list. It
    # carries no labels, so there is nothing for the namespace filter to read — and handing it
    # to the filter is what used to raise AttributeError out of the tool.
    if result_type in ("scalar", "string"):
        return _fmt_scalar(promql, result_type, raw)

    results, dropped = drop_blocked_series(
        [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else [], "metric"
    )
    if not results:
        return (
            all_withheld_message(dropped) if dropped else f"No data for query: {promql}"
        )

    # `matrix` from an instant query means the expression carried a range selector.
    if result_type == "matrix":
        span = f"range {range_minutes}m" if range_minutes > 0 else "range selector"
        output = _fmt_range(promql, span, results)
    else:
        output = _fmt_instant(promql, results)

    if dropped:
        output += withheld_note(dropped)

    if len(output) > _OUTPUT_CAP:
        output = output[:_OUTPUT_CAP] + "\n... [truncated — use a more specific query]"
    return output


def _auto_step(range_minutes: int) -> str:
    """Choose a sensible step size so the response has ~100 data points."""
    seconds_per_point = max((range_minutes * 60) // 100, 15)
    if seconds_per_point < 60:
        return f"{seconds_per_point}s"
    return f"{seconds_per_point // 60}m"
