"""
client.py — KubeQClient and AsyncKubeQClient: SDK entry points for kube_q.

Usage (sync)::

    from kube_q.core.client import KubeQClient
    from kube_q.core.events import TokenEvent, FinalEvent

    client = KubeQClient(url="http://localhost:8000", api_key="...")

    # Non-streaming
    result = client.query("why are my pods failing?")
    print(result["text"])

    # Streaming (sync iterator)
    for event in client.stream("list all pods"):
        match event:
            case TokenEvent(data=d): print(d.content, end="", flush=True)
            case FinalEvent():       break

Usage (async)::

    from kube_q.core.client import AsyncKubeQClient

    client = AsyncKubeQClient(url="http://localhost:8000", api_key="...")

    async for event in client.stream("list all pods"):
        match event:
            case TokenEvent(data=d): print(d.content, end="", flush=True)
            case FinalEvent():       break
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx

from kube_q.core.events import Event, parse_event
from kube_q.core.transport import (
    QUERY_RETRY_DELAYS,
    build_headers,
    build_payload,
    check_health,
    describe_error,
    iter_sse,
    make_client,
)

_logger = logging.getLogger(__name__)


class KubeQClient:
    """Thin SDK client around the KubeIntellect HTTP API.

    Parameters
    ----------
    url:
        Base URL of the KubeIntellect API (no trailing slash).
    api_key:
        Bearer token for authentication.  ``None`` for unauthenticated use.
    ca_cert:
        Path to a custom CA certificate bundle for TLS verification.
    timeout:
        Per-request timeout in seconds.
    model:
        Model name sent in every request.
    """

    def __init__(
        self,
        url: str = "https://api.kubeintellect.com",
        *,
        api_key: str | None = None,
        ca_cert: str | None = None,
        timeout: float = 120.0,
        model: str = "kubeintellect-v2",
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.ca_cert = ca_cert
        self.timeout = timeout
        self.model = model

    # ── Health ────────────────────────────────────────────────────────────────

    def health(self) -> tuple[bool, str]:
        """Return (ok, reason). Fast connectivity check."""
        return check_health(
            self.url, api_key=self.api_key, ca_cert=self.ca_cert, timeout=5.0
        )

    # ── Non-streaming query ───────────────────────────────────────────────────

    def query(
        self,
        text: str,
        *,
        session_id: str | None = None,
        user: str = "sdk-user",
        namespace: str | None = None,
    ) -> dict:
        """Send a single non-streaming query and return the raw response dict.

        Returns a dict with keys: text, hitl_pending, action_id, usage.
        On connection failure after retries, text is empty string.
        """
        sid = session_id or str(uuid.uuid4())
        request_id = f"req-{uuid.uuid4()}"
        content = text
        if namespace:
            content = f"[context: namespace={namespace}] {content}"

        messages = [{"role": "user", "content": content}]
        payload = build_payload(messages, user, False, self.model)
        headers = build_headers(self.api_key, sid, request_id)


        import httpx

        with make_client(self.ca_cert, timeout=self.timeout) as client:
            for attempt in range(len(QUERY_RETRY_DELAYS) + 1):
                try:
                    resp = client.post(
                        f"{self.url}/v1/chat/completions", json=payload, headers=headers
                    )
                    if resp.status_code not in (200, 401):
                        _logger.warning("HTTP %d from %s", resp.status_code, self.url)
                        return {"text": "", "hitl_pending": False, "action_id": None,
                                "usage": None}
                    if resp.status_code == 401:
                        _logger.error("Authentication required")
                        return {"text": "", "hitl_pending": False, "action_id": None,
                                "usage": None}

                    data = resp.json()
                    choice = data["choices"][0]
                    response_text = choice["message"]["content"]
                    hitl_pending = choice.get("hitl_required", False)
                    action_id = choice.get("action_id") if hitl_pending else None
                    return {
                        "text": response_text,
                        "hitl_pending": hitl_pending,
                        "action_id": action_id,
                        "usage": data.get("usage"),
                    }
                except httpx.TransportError as exc:
                    reason = describe_error(self.url, exc)
                    _logger.warning("attempt %d failed: %s", attempt, reason)
                    if attempt < len(QUERY_RETRY_DELAYS):
                        time.sleep(QUERY_RETRY_DELAYS[attempt])

        return {"text": "", "hitl_pending": False, "action_id": None, "usage": None}

    # ── Streaming (sync SSE iterator) ─────────────────────────────────────────

    def stream(
        self,
        text: str,
        *,
        session_id: str | None = None,
        user: str = "sdk-user",
        namespace: str | None = None,
        request_id: str | None = None,
    ) -> Iterator[Event]:
        """Yield typed ``Event`` objects from a streaming query.

        Unrecognised or malformed SSE events are silently skipped.
        Retries up to ``len(QUERY_RETRY_DELAYS)`` times on transport errors,
        but only if no events have been yielded yet (partial streams are not
        retried to avoid duplicate delivery).
        """
        sid = session_id or str(uuid.uuid4())
        rid = request_id or f"req-{uuid.uuid4()}"
        content = text
        if namespace:
            content = f"[context: namespace={namespace}] {content}"

        messages = [{"role": "user", "content": content}]
        payload = build_payload(messages, user, True, self.model)
        headers = build_headers(self.api_key, sid, rid, accept="text/event-stream")

        last_exc: httpx.TransportError | None = None
        with make_client(self.ca_cert, timeout=self.timeout) as client:
            for attempt in range(len(QUERY_RETRY_DELAYS) + 1):
                yielded_any = False
                try:
                    with client.stream(
                        "POST",
                        f"{self.url}/v1/chat/completions",
                        json=payload,
                        headers=headers,
                    ) as resp:
                        resp.raise_for_status()
                        for raw in iter_sse(resp):
                            event = _parse_sse_chunk(raw)
                            if event is not None:
                                yielded_any = True
                                yield event
                    return  # clean end of stream
                except httpx.TransportError as exc:
                    if yielded_any:
                        raise  # partial stream: can't retry without duplicate delivery
                    last_exc = exc
                    reason = describe_error(self.url, exc)
                    _logger.warning("stream attempt %d failed: %s", attempt, reason)
                    if attempt < len(QUERY_RETRY_DELAYS):
                        time.sleep(QUERY_RETRY_DELAYS[attempt])
        if last_exc is not None:
            raise last_exc


# ── AsyncKubeQClient ──────────────────────────────────────────────────────────

class AsyncKubeQClient:
    """Async variant of KubeQClient for use in async frameworks (web servers, notebooks).

    Parameters are identical to :class:`KubeQClient`.

    Example::

        client = AsyncKubeQClient(url="http://localhost:8000")

        async for event in client.stream("why are my pods failing?"):
            match event:
                case TokenEvent(data=d): print(d.content, end="", flush=True)
                case FinalEvent():       break
    """

    def __init__(
        self,
        url: str = "https://api.kubeintellect.com",
        *,
        api_key: str | None = None,
        ca_cert: str | None = None,
        timeout: float = 120.0,
        model: str = "kubeintellect-v2",
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.ca_cert = ca_cert
        self.timeout = timeout
        self.model = model

    def _make_async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            verify=self.ca_cert if self.ca_cert else True,
        )

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> tuple[bool, str]:
        """Return (ok, reason). Fast connectivity check."""
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with self._make_async_client() as client:
                r = await client.get(f"{self.url}/healthz", headers=headers)
            if r.status_code == 200:
                return True, ""
            if r.status_code == 401:
                return False, "Authentication required — set KUBE_Q_API_KEY or pass --api-key"
            return False, f"HTTP {r.status_code} from {self.url}/healthz"
        except httpx.ConnectError as e:
            return False, f"Connection refused — nothing is listening at {self.url}: {e}"
        except httpx.TimeoutException:
            return False, f"Connection timed out — {self.url} did not respond"
        except Exception as e:
            return False, f"Unexpected error: {e}"

    # ── Non-streaming query ───────────────────────────────────────────────────

    async def query(
        self,
        text: str,
        *,
        session_id: str | None = None,
        user: str = "sdk-user",
        namespace: str | None = None,
    ) -> dict:
        """Send a single non-streaming query and return the raw response dict.

        Returns a dict with keys: text, hitl_pending, action_id, usage.
        On connection failure after retries, text is empty string.
        """
        sid = session_id or str(uuid.uuid4())
        request_id = f"req-{uuid.uuid4()}"
        content = f"[context: namespace={namespace}] {text}" if namespace else text
        messages = [{"role": "user", "content": content}]
        payload = build_payload(messages, user, False, self.model)
        headers = build_headers(self.api_key, sid, request_id)

        async with self._make_async_client() as client:
            for attempt in range(len(QUERY_RETRY_DELAYS) + 1):
                try:
                    resp = await client.post(
                        f"{self.url}/v1/chat/completions", json=payload, headers=headers
                    )
                    if resp.status_code == 401:
                        _logger.error("Authentication required")
                        return {"text": "", "hitl_pending": False, "action_id": None,
                                "usage": None}
                    if resp.status_code != 200:
                        _logger.warning("HTTP %d from %s", resp.status_code, self.url)
                        return {"text": "", "hitl_pending": False, "action_id": None,
                                "usage": None}
                    data = resp.json()
                    choice = data["choices"][0]
                    response_text = choice["message"]["content"]
                    hitl_pending = choice.get("hitl_required", False)
                    action_id = choice.get("action_id") if hitl_pending else None
                    return {
                        "text": response_text,
                        "hitl_pending": hitl_pending,
                        "action_id": action_id,
                        "usage": data.get("usage"),
                    }
                except httpx.TransportError as exc:
                    reason = describe_error(self.url, exc)
                    _logger.warning("async query attempt %d failed: %s", attempt, reason)
                    if attempt < len(QUERY_RETRY_DELAYS):
                        import asyncio
                        await asyncio.sleep(QUERY_RETRY_DELAYS[attempt])

        return {"text": "", "hitl_pending": False, "action_id": None, "usage": None}

    # ── Streaming ─────────────────────────────────────────────────────────────

    async def stream(
        self,
        text: str,
        *,
        session_id: str | None = None,
        user: str = "sdk-user",
        namespace: str | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """Yield typed ``Event`` objects from a streaming query (async generator).

        Retries on transport errors only if no events have been yielded yet.
        """
        import asyncio

        sid = session_id or str(uuid.uuid4())
        rid = request_id or f"req-{uuid.uuid4()}"
        content = f"[context: namespace={namespace}] {text}" if namespace else text
        messages = [{"role": "user", "content": content}]
        payload = build_payload(messages, user, True, self.model)
        headers = build_headers(self.api_key, sid, rid, accept="text/event-stream")

        async with self._make_async_client() as client:
            for attempt in range(len(QUERY_RETRY_DELAYS) + 1):
                yielded_any = False
                try:
                    async with client.stream(
                        "POST",
                        f"{self.url}/v1/chat/completions",
                        json=payload,
                        headers=headers,
                    ) as resp:
                        resp.raise_for_status()
                        async for raw in _aiter_sse(resp):
                            event = _parse_sse_chunk(raw)
                            if event is not None:
                                yielded_any = True
                                yield event
                    return  # clean end of stream
                except httpx.TransportError as exc:
                    if yielded_any:
                        raise
                    reason = describe_error(self.url, exc)
                    _logger.warning("async stream attempt %d failed: %s", attempt, reason)
                    if attempt < len(QUERY_RETRY_DELAYS):
                        await asyncio.sleep(QUERY_RETRY_DELAYS[attempt])


# ── Shared SSE helpers ────────────────────────────────────────────────────────

def _parse_sse_chunk(raw: dict) -> Event | None:
    """Convert one raw SSE dict (from iter_sse) into a typed Event or None."""
    # ki_event side-channel wrapper
    ki = raw.get("ki_event")
    if ki:
        return parse_event(ki)

    # Standard OpenAI streaming chunk → token event
    choices = raw.get("choices", [])
    if choices:
        delta = choices[0].get("delta", {})
        content_chunk = delta.get("content")
        if content_chunk:
            return parse_event({"type": "token", "data": {"content": content_chunk}})

    # Usage at end of stream → usage event
    if "usage" in raw and not choices:
        return parse_event({"type": "usage", "data": raw["usage"]})

    return None


async def _aiter_sse(response: httpx.Response) -> AsyncIterator[dict]:  # type: ignore[return]
    """Async SSE parser — mirrors iter_sse but for httpx async streaming."""
    import json

    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            for line in block.splitlines():
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        yield json.loads(payload)
                    except Exception:
                        pass
