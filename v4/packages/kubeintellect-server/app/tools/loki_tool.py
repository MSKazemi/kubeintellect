"""
query_loki — LogQL log queries against the cluster Loki instance.

Covers everything kubectl logs cannot:
  - Cross-pod / cross-namespace log aggregation
  - Historical logs beyond the running container's buffer
  - Structured log filtering by label, level, or regex
  - Metric queries (log rate, error rate over time)

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
_LOG_LINE_CAP = 200


@tool
def query_loki(logql: str, limit: int = 100, since: str = "1h") -> str:
    """Query Loki for container and application logs using LogQL.

    Prefer this over `kubectl logs` when you need:
    - Logs from multiple pods at once (e.g. all replicas of a deployment)
    - Historical logs from crashed / restarted containers
    - Structured filtering (by log level, JSON field, regex)
    - Log volume / error rate metrics over time

    Args:
        logql: A LogQL expression. Examples:
            {namespace="production", pod=~"payment-api.*"}
            {namespace="staging"} |= "ERROR"
            {app="nginx"} | json | status >= 500
            {namespace="production"} |~ "timeout|connection refused"
            rate({namespace="production"} |= "error" [5m])
        limit: Maximum number of log lines to return (default 100, max 500).
            Ignored for metric queries (those returning rate/count data).
        since: How far back to look. Examples: "15m", "1h", "6h", "24h".
            Default is "1h".

    Returns:
        Formatted log lines or metric values, capped at 6 000 characters.
    """
    if not settings.LOKI_URL:
        return "Loki is not configured. Set LOKI_URL in ~/.kubeintellect/.env and restart."

    loki_url = settings.LOKI_URL.strip()
    if not loki_url.startswith(("http://", "https://")):
        loki_url = f"http://{loki_url}"
        logger.warning(f"LOKI_URL missing protocol — using {loki_url}")
    base_url = loki_url.rstrip("/")
    limit = min(limit, 500)
    logger.debug(f"query_loki: {logql!r} limit={limit} since={since}")

    now_ns = int(time.time() * 1e9)
    start_ns = now_ns - _parse_duration_ns(since)

    try:
        with httpx.Client(timeout=15.0) as client:
            # Metric queries (start with rate/count/sum/avg) go to /query_range
            if _is_metric_query(logql):
                output = _range_query(client, base_url, logql, start_ns, now_ns)
            else:
                output = _log_query(client, base_url, logql, limit, start_ns, now_ns)

    except httpx.ConnectError:
        return (
            f"Cannot reach Loki at {base_url}. "
            "Is Loki deployed? (make install-loki-kind)"
        )
    except httpx.TimeoutException:
        return "Loki query timed out (15s)."
    except Exception as exc:
        return f"Loki error: {exc}"

    if len(output) > _OUTPUT_CAP:
        output = output[:_OUTPUT_CAP] + "\n... [truncated — use a narrower query or shorter since]"
    return output


# ── Internal helpers ──────────────────────────────────────────────────────────

def _is_metric_query(logql: str) -> bool:
    """Return True for LogQL metric expressions (start with an aggregation function)."""
    stripped = logql.strip().lower()
    return any(stripped.startswith(fn) for fn in ("rate(", "count_over_time(", "sum(", "avg(", "max(", "min(", "bytes_rate(", "bytes_over_time("))


def _parse_duration_ns(since: str) -> int:
    """Convert a duration string like '1h', '30m', '24h' to nanoseconds."""
    since = since.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    unit = since[-1]
    try:
        amount = int(since[:-1])
        return amount * units.get(unit, 3600) * int(1e9)
    except (ValueError, KeyError):
        return 3600 * int(1e9)  # default 1h


def _log_query(
    client: httpx.Client,
    base_url: str,
    logql: str,
    limit: int,
    start_ns: int,
    end_ns: int,
) -> str:
    resp = client.get(
        f"{base_url}/loki/api/v1/query_range",
        params={
            "query": logql,
            "limit": limit,
            "start": start_ns,
            "end": end_ns,
            "direction": "backward",
        },
    )
    if resp.status_code != 200:
        return f"Loki HTTP {resp.status_code}: {resp.text[:500]}"

    data = resp.json()
    streams = data.get("data", {}).get("result", [])
    if not streams:
        return f"No logs found for: {logql}"

    lines = [f"LogQL: {logql}", f"Streams: {len(streams)}"]
    total = 0
    for stream in streams:
        labels = ", ".join(f"{k}={v}" for k, v in stream.get("stream", {}).items())
        lines.append(f"\n[{labels}]")
        for ts_ns, log_line in stream.get("values", []):
            ts = _fmt_ts(int(ts_ns))
            lines.append(f"  {ts}  {log_line}")
            total += 1
            if total >= _LOG_LINE_CAP:
                lines.append(f"  ... (capped at {_LOG_LINE_CAP} lines — use a shorter since or lower limit)")
                return "\n".join(lines)

    return "\n".join(lines)


def _range_query(
    client: httpx.Client,
    base_url: str,
    logql: str,
    start_ns: int,
    end_ns: int,
) -> str:
    duration_s = (end_ns - start_ns) // int(1e9)
    step = max(duration_s // 100, 15)

    resp = client.get(
        f"{base_url}/loki/api/v1/query_range",
        params={
            "query": logql,
            "start": start_ns,
            "end": end_ns,
            "step": f"{step}s",
        },
    )
    if resp.status_code != 200:
        return f"Loki HTTP {resp.status_code}: {resp.text[:500]}"

    data = resp.json()
    results = data.get("data", {}).get("result", [])
    if not results:
        return f"No metric data for: {logql}"

    lines = [f"LogQL (metric): {logql}", f"Series: {len(results)}"]
    for r in results[:20]:
        labels = ", ".join(f"{k}={v}" for k, v in r.get("metric", {}).items())
        values = [float(v[1]) for v in r.get("values", []) if v[1] not in ("NaN",)]
        if values:
            lines.append(
                f"  [{labels}]  min={min(values):.4f}  "
                f"avg={sum(values)/len(values):.4f}  max={max(values):.4f}"
            )
    return "\n".join(lines)


def _fmt_ts(ts_ns: int) -> str:
    """Format a nanosecond timestamp as HH:MM:SS."""
    import datetime
    dt = datetime.datetime.fromtimestamp(ts_ns / 1e9, tz=datetime.timezone.utc)
    return dt.strftime("%H:%M:%S")
