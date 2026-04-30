"""
KubeIntellect SSE client for the evaluation harness.

Sends a query to POST /v1/chat/completions, consumes the full SSE stream,
and returns a structured EvalResult with all events, timing, and extracted signals.
"""
from __future__ import annotations

import json
import os
import time
import uuid

import httpx

from .models import EvalResult, StreamEvent


class KubeIntellectClient:
    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 180.0,
    ):
        self.api_url = (api_url or os.getenv("KUBEINTELLECT_URL", "http://api.kubeintellect.local")).rstrip("/")
        self.api_key = api_key or os.getenv("KUBEINTELLECT_API_KEY", "")
        self.timeout = timeout

    async def query(
        self,
        messages: list[dict],
        query_id: str,
        session_id: str | None = None,
        auto_approve: bool = True,
    ) -> EvalResult:
        if session_id is None:
            session_id = f"eval-{query_id}-{uuid.uuid4().hex[:8]}"
        return await self._send(
            messages=messages,
            query_id=query_id,
            session_id=session_id,
            auto_approve=auto_approve,
        )

    async def query_followup(
        self,
        follow_up_text: str,
        query_id: str,
        session_id: str,
        auto_approve: bool = True,
    ) -> EvalResult:
        """Send a follow-up message on an existing session.

        Server resumes the LangGraph thread automatically: if paused at HITL it
        resolves to Command(resume=True); otherwise it starts a new turn on
        the same checkpointed thread. With auto_approve=true the server sets
        hitl_bypass=True so the agent acts on the follow-up without needing
        approval-string detection.
        """
        return await self._send(
            messages=[{"role": "user", "content": follow_up_text}],
            query_id=query_id,
            session_id=session_id,
            auto_approve=auto_approve,
        )

    async def _send(
        self,
        messages: list[dict],
        query_id: str,
        session_id: str,
        auto_approve: bool,
    ) -> EvalResult:
        start_ts = time.time()
        events: list[StreamEvent] = []
        final_text = ""
        had_hitl = False
        had_error = False
        error_message: str | None = None
        tool_calls: list[str] = []
        tool_errors: list[str] = []
        status_phases: list[str] = []
        hitl_commands: list[str] = []

        payload = {
            "messages": messages,
            "stream": True,
            "auto_approve": auto_approve,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-Session-ID": session_id,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.api_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    # Skip empty lines and heartbeat comments
                    if not line or line.startswith(":"):
                        continue

                    if not line.startswith("data: "):
                        continue

                    raw = line[6:]  # strip "data: "

                    if raw == "[DONE]":
                        break

                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    # Protocol handshake frame
                    if frame.get("object") == "stream.start":
                        events.append(StreamEvent(type="start", data=frame))
                        continue

                    # Side-channel KI events (status, tool_call, tool_result, plan, error)
                    if "ki_event" in frame:
                        ki = frame["ki_event"]
                        etype = ki.get("type", "unknown")
                        events.append(StreamEvent(type=etype, data=ki))

                        if etype == "tool_call":
                            tool_name = ki.get("tool", "unknown")
                            tool_calls.append(tool_name)

                        elif etype == "tool_result":
                            output = ki.get("output", "")
                            # Heuristic: tool output starting with "Error" signals failure
                            if output and (output.startswith("Error") or "error:" in output.lower()[:80]):
                                tool_errors.append(ki.get("tool", "unknown"))

                        elif etype == "status":
                            phase = ki.get("phase", "")
                            if phase:
                                status_phases.append(phase)

                        elif etype == "error":
                            had_error = True
                            error_message = ki.get("error", "unknown error")

                        continue

                    # Content / HITL frames (choices array)
                    choices = frame.get("choices", [])
                    if not choices:
                        continue

                    choice = choices[0]

                    # HITL approval request embedded in content frame
                    if choice.get("hitl_required"):
                        had_hitl = True
                        cmd = choice.get("human_summary", "")
                        if cmd:
                            hitl_commands.append(cmd)
                        events.append(StreamEvent(type="hitl_request", data=choice))
                        continue

                    delta = choice.get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        final_text += content
                        events.append(StreamEvent(type="token", data={"content": content}))

                    # Error embedded as content with finish_reason="stop"
                    finish_reason = choice.get("finish_reason")
                    if finish_reason == "stop" and "\n\n**Error:**" in content:
                        had_error = True
                        error_message = content.strip()

        end_ts = time.time()
        return EvalResult(
            session_id=session_id,
            query_id=query_id,
            messages=messages,
            final_text=final_text.strip(),
            events=events,
            had_hitl=had_hitl,
            had_error=had_error,
            error_message=error_message,
            latency_ms=(end_ts - start_ts) * 1000,
            start_ts=start_ts,
            end_ts=end_ts,
            tool_calls=tool_calls,
            tool_errors=tool_errors,
            status_phases=status_phases,
            hitl_commands=hitl_commands,
        )

    def query_sync(
        self,
        query: str,
        session_id: str,
        auto_approve: bool = True,
    ) -> tuple[str, float, bool]:
        """Synchronous single-query send for csv/stress modes.

        Returns (response_text, latency_seconds, had_hitl).
        On error returns ("ERROR: ...", latency, False).
        """
        url     = f"{self.api_url}/v1/chat/completions"
        payload = {"messages": [{"role": "user", "content": query}], "stream": True, "auto_approve": auto_approve}
        headers = {"Accept": "text/event-stream", "X-Session-ID": session_id}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        start    = time.monotonic()
        parts: list[str] = []
        had_hitl = False

        try:
            with httpx.Client(timeout=self.timeout) as http:
                with http.stream("POST", url, json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    buf = ""
                    for raw in resp.iter_text():
                        buf += raw
                        while "\n\n" in buf:
                            block, buf = buf.split("\n\n", 1)
                            for line in block.splitlines():
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if data == "[DONE]":
                                    break
                                try:
                                    chunk = json.loads(data)
                                    for ch in chunk.get("choices") or []:
                                        parts.append(ch.get("delta", {}).get("content", ""))
                                        if ch.get("hitl_required"):
                                            had_hitl = True
                                except json.JSONDecodeError:
                                    pass
        except Exception as exc:
            return f"ERROR: {exc}", time.monotonic() - start, False

        return "".join(parts), time.monotonic() - start, had_hitl
