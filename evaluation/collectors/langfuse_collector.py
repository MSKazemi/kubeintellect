"""
Langfuse trace collector.

Queries the Langfuse REST API to pull per-session traces and observations,
extracting LLM call metrics and tool span details for offline analysis.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from ..models import LangfuseGeneration, LangfuseToolSpan, LangfuseTrace


def _empty(session_id: str, *, available: bool) -> LangfuseTrace:
    return LangfuseTrace(
        trace_id="",
        session_id=session_id,
        total_tokens=0,
        total_latency_ms=0.0,
        generations=[],
        tool_spans=[],
        has_error=False,
        error_messages=[],
        available=available,
    )


class LangfuseCollector:
    def __init__(self):
        self.public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
        self.secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
        host = os.getenv("LANGFUSE_HOST", "").rstrip("/")
        # Strip trailing port if it's embedded in the host for the web UI
        self.base_url = f"{host}/api/public" if host else ""
        self.enabled = bool(self.public_key and self.secret_key and self.base_url)

    def get(self, path: str, **params: Any) -> Any:
        """Synchronous Langfuse REST call. Used by CLI tools (analyze_trace.py)."""
        if not self.enabled:
            raise RuntimeError("Langfuse not configured. Set LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST.")
        url = f"{self.base_url}{path}"
        r = httpx.get(url, auth=(self.public_key, self.secret_key), params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    async def collect(self, session_id: str) -> LangfuseTrace:
        if not self.enabled:
            return _empty(session_id, available=False)

        auth = (self.public_key, self.secret_key)

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Find traces for this session
            resp = await client.get(
                f"{self.base_url}/traces",
                params={"sessionId": session_id, "limit": 5},
                auth=auth,
            )
            if resp.status_code != 200:
                return _empty(session_id, available=False)

            traces = resp.json().get("data", [])
            if not traces:
                return _empty(session_id, available=True)

            trace_id = traces[0]["id"]

            # Fetch all observations (spans + generations) for the trace.
            # Langfuse caps limit at 100; paginate if the trace has more.
            all_obs: list[dict] = []
            page = 1
            while True:
                obs_resp = await client.get(
                    f"{self.base_url}/observations",
                    params={"traceId": trace_id, "limit": 100, "page": page},
                    auth=auth,
                )
                if obs_resp.status_code != 200:
                    break
                batch = obs_resp.json().get("data", [])
                all_obs.extend(batch)
                if len(batch) < 100:
                    break
                page += 1

        generations: list[LangfuseGeneration] = []
        tool_spans: list[LangfuseToolSpan] = []
        total_tokens = 0
        errors: list[str] = []

        for obs in all_obs:
            obs_type = obs.get("type", "")
            # Langfuse v4 reports latency in ms already
            latency_ms = float(obs.get("latency") or 0.0)

            if obs_type == "GENERATION":
                usage = obs.get("usage") or {}
                prompt_tokens = usage.get("input") or 0
                completion_tokens = usage.get("output") or 0
                total_tokens += prompt_tokens + completion_tokens
                generations.append(LangfuseGeneration(
                    name=obs.get("name") or "",
                    model=obs.get("model") or "",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    latency_ms=latency_ms,
                ))
                msg = obs.get("statusMessage")
                if msg:
                    errors.append(f"generation '{obs.get('name')}': {msg}")

            elif obs_type in ("SPAN", "TOOL"):
                name = obs.get("name") or ""
                # Include spans/tools that look like tool invocations
                if any(kw in name.lower() for kw in ("tool", "kubectl", "prometheus", "loki")):
                    output = obs.get("output")
                    output_str = str(output)[:500] if output else None
                    err = obs.get("statusMessage")
                    tool_spans.append(LangfuseToolSpan(
                        name=name,
                        input_args=obs.get("input") or {},
                        output=output_str,
                        error=err,
                        latency_ms=latency_ms,
                    ))
                    if err:
                        errors.append(f"tool span '{name}': {err}")

        total_latency_ms = sum(g.latency_ms for g in generations)

        return LangfuseTrace(
            trace_id=trace_id,
            session_id=session_id,
            total_tokens=total_tokens,
            total_latency_ms=total_latency_ms,
            generations=generations,
            tool_spans=tool_spans,
            has_error=bool(errors),
            error_messages=errors,
            available=True,
        )
