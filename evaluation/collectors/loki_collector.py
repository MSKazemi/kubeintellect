"""
Loki log collector.

Queries Loki for all structured log lines emitted by KubeIntellect during a
specific session, extracting error counts, kubectl commands, and HITL events.
"""
from __future__ import annotations

import json
import os
import re

import httpx

from ..models import LokiLogLine, LokiLogs


def _empty(session_id: str, *, available: bool) -> LokiLogs:
    return LokiLogs(
        session_id=session_id,
        lines=[],
        error_count=0,
        warning_count=0,
        kubectl_commands=[],
        hitl_events=[],
        available=available,
    )


_KUBECTL_RE = re.compile(r"kubectl\s+\S+(?:\s+[^\n]{0,200})?")
_HITL_KEYWORDS = ("hitl", "interrupt", "approval required", "approve", "risk_level")


class LokiCollector:
    def __init__(self):
        self.loki_url = os.getenv("LOKI_URL", "").rstrip("/")
        self.enabled = bool(self.loki_url)

    async def collect(self, session_id: str, start_ts: float, end_ts: float) -> LokiLogs:
        if not self.enabled:
            return _empty(session_id, available=False)

        # Add a 10-second buffer on each side to catch async log flushes
        start_ns = int((start_ts - 10) * 1e9)
        end_ns = int((end_ts + 10) * 1e9)

        # LogQL: filter by app label and session_id string presence
        query = f'{{app="kubeintellect"}} |= "{session_id}"'

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.get(
                    f"{self.loki_url}/loki/api/v1/query_range",
                    params={
                        "query": query,
                        "start": start_ns,
                        "end": end_ns,
                        "limit": 1000,
                        "direction": "forward",
                    },
                )
                if resp.status_code != 200:
                    return _empty(session_id, available=False)
                result = resp.json()
            except Exception:
                return _empty(session_id, available=False)

        lines: list[LokiLogLine] = []
        kubectl_commands: list[str] = []
        hitl_events: list[str] = []

        for stream in result.get("data", {}).get("result", []):
            for ts_str, log_text in stream.get("values", []):
                level, message = _parse_log_line(log_text)
                lines.append(LokiLogLine(
                    timestamp_ns=int(ts_str),
                    level=level,
                    message=message,
                ))

                kubectl_m = _KUBECTL_RE.search(message)
                if kubectl_m:
                    kubectl_commands.append(kubectl_m.group(0)[:300])

                msg_lower = message.lower()
                if any(kw in msg_lower for kw in _HITL_KEYWORDS):
                    hitl_events.append(message[:300])

        return LokiLogs(
            session_id=session_id,
            lines=lines,
            error_count=sum(1 for l in lines if l.level == "ERROR"),
            warning_count=sum(1 for l in lines if l.level == "WARNING"),
            kubectl_commands=kubectl_commands,
            hitl_events=hitl_events,
            available=True,
        )


def _parse_log_line(raw: str) -> tuple[str, str]:
    """Return (level, message) from a raw log line (JSON or plain text)."""
    try:
        obj = json.loads(raw)
        level = (obj.get("level") or obj.get("levelname") or "INFO").upper()
        message = obj.get("message") or obj.get("msg") or raw
        return level, str(message)
    except Exception:
        pass

    upper = raw.upper()
    if "ERROR" in upper[:20]:
        return "ERROR", raw
    if "WARNING" in upper[:20] or "WARN" in upper[:20]:
        return "WARNING", raw
    return "INFO", raw
