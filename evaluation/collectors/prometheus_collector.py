"""
Prometheus metrics collector.

Queries Prometheus for aggregate latency and error-rate metrics during an
evaluation window. Used for run-level stats, not per-query scoring.
"""
from __future__ import annotations

import os

import httpx

from ..models import PrometheusMetrics


def _empty(*, available: bool) -> PrometheusMetrics:
    return PrometheusMetrics(p95_latency_ms=None, error_rate=None, request_count=None, available=available)


class PrometheusCollector:
    def __init__(self):
        self.prom_url = os.getenv("PROMETHEUS_URL", "").rstrip("/")
        self.enabled = bool(self.prom_url)

    async def collect_window(self, start_ts: float, end_ts: float) -> PrometheusMetrics:
        """Collect aggregate metrics for a time window (full eval run)."""
        if not self.enabled:
            return _empty(available=False)

        step = max(15, int((end_ts - start_ts) / 60))

        async with httpx.AsyncClient(timeout=15.0) as client:
            p95 = await self._scalar(
                client,
                'histogram_quantile(0.95, rate(http_request_duration_seconds_bucket{path="/v1/chat/completions"}[5m]))',
                start_ts, end_ts, step,
            )
            err_rate = await self._scalar(
                client,
                'rate(http_requests_total{path="/v1/chat/completions",status=~"5.."}[5m])',
                start_ts, end_ts, step,
            )
            req_count = await self._scalar(
                client,
                'increase(http_requests_total{path="/v1/chat/completions"}[1h])',
                end_ts, end_ts, step,
            )

        return PrometheusMetrics(
            p95_latency_ms=p95 * 1000 if p95 is not None else None,
            error_rate=err_rate,
            request_count=int(req_count) if req_count is not None else None,
            available=True,
        )

    async def _scalar(
        self,
        client: httpx.AsyncClient,
        query: str,
        start: float,
        end: float,
        step: int,
    ) -> float | None:
        try:
            resp = await client.get(
                f"{self.prom_url}/api/v1/query_range",
                params={"query": query, "start": start, "end": end, "step": step},
            )
            if resp.status_code != 200:
                return None
            data = resp.json().get("data", {}).get("result", [])
            if not data:
                return None
            # Take the last value from the first series
            values = data[0].get("values", [])
            if not values:
                return None
            return float(values[-1][1])
        except Exception:
            return None
