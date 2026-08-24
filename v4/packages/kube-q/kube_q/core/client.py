"""
client.py — KubeQClient and AsyncKubeQClient: SDK entry points for kube_q.

Usage (sync)::

    from kube_q.core.client import KubeQClient
    from kube_q.core.events import TokenEvent, FinalEvent, HitlRequestEvent

    client = KubeQClient(url="http://localhost:8000", api_key="...")

    # Non-streaming
    result = client.query("why are my pods failing?")
    print(result["text"])

    # Streaming (sync iterator)
    for event in client.stream("list all pods"):
        match event:
            case TokenEvent(data=d): print(d.content, end="", flush=True)
            # The agent has stopped for a human. `d.approval_id` is what resumes it: send the
            # approval as the next message on the same session id.
            case HitlRequestEvent(data=d): print(f"\nAPPROVAL NEEDED ({d.risk}): {d.action}")
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
    HEALTH_PATH,
    HEALTH_TIMEOUT,
    QUERY_RETRY_DELAYS,
    SseStats,
    build_headers,
    build_payload,
    check_health,
    describe_error,
    health_failure_reason,
    health_status_reason,
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
        # What the most recent `stream()` lost, or None if it has not run. `SseStats` says a
        # caller that must be fail-closed "inspects this and refuses" — true of the CLI, which
        # owns its own SseStats, and never true of the SDK, where `stats` was a local nobody
        # could reach. Set before the first yield so a caller that breaks early can still read it.
        self.last_stream_stats: SseStats | None = None

    # ── Health ────────────────────────────────────────────────────────────────

    def health(self) -> tuple[bool, str]:
        """Return (ok, reason). Fast connectivity check."""
        return check_health(
            self.url, api_key=self.api_key, ca_cert=self.ca_cert,
            timeout=HEALTH_TIMEOUT, health_path=HEALTH_PATH,
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
                        stats = SseStats()
                        self.last_stream_stats = stats
                        try:
                            for raw in iter_sse(resp, stats):
                                for event in _parse_sse_events(raw):
                                    yielded_any = True
                                    yield event
                        finally:
                        # `finally`, not a trailing call: the documented usage in
                        # `docs/sdk.md` and in this class's own docstring is
                        # `case FinalEvent(): break`, and a break closes the generator at the
                        # `yield` — so the trailing call ran on a full drain and on nothing else.
                        # A stream that dropped a frame before the final event left no record
                        # anywhere, for exactly the pattern the docs tell people to write.
                            _warn_if_lossy(stats)
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
        self.last_stream_stats: SseStats | None = None

    def _make_async_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            verify=self.ca_cert if self.ca_cert else True,
        )

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> tuple[bool, str]:
        """Return (ok, reason). Fast connectivity check.

        Shares its two classifiers with the sync `KubeQClient.health` rather than re-deciding.
        This method used to hand-roll them, and had drifted on all three points: a DNS failure
        was reported as "Connection refused — nothing is listening at …" (the wrong cause, and
        the wrong thing to go and fix); the timeout message dropped the duration that the sync
        side had been corrected to include; and "fast" ran on `self.timeout`, the *query*
        timeout, which defaults to 120 s against the sync side's 5 s.
        """
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        timeout = HEALTH_TIMEOUT
        try:
            async with httpx.AsyncClient(
                timeout=timeout, verify=self.ca_cert if self.ca_cert else True
            ) as client:
                r = await client.get(f"{self.url}{HEALTH_PATH}", headers=headers)
            return health_status_reason(self.url, r.status_code, HEALTH_PATH)
        except Exception as e:  # noqa: BLE001 — every failure is a health verdict, never a raise
            return False, health_failure_reason(self.url, e, timeout)

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

        # Carried out of the loop for the same reason the sync `stream` carries it: an async
        # generator that falls off the end of its retry loop **returns**, and a return from an
        # async generator is a clean end-of-stream. The caller's `async for` would then complete
        # with zero events and no exception — the server never answered, and the SDK reported
        # that as "the server answered, and had nothing to say". Measured 2026-08-24 against a
        # refused connection: sync raised `ConnectError`, async yielded 0 events silently, and
        # `docs/sdk.md` claimed both "give up and raise".
        last_exc: httpx.TransportError | None = None
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
                        stats = SseStats()
                        self.last_stream_stats = stats
                        try:
                            async for raw in _aiter_sse(resp, stats):
                                for event in _parse_sse_events(raw):
                                    yielded_any = True
                                    yield event
                        finally:
                            _warn_if_lossy(stats)  # see the sync twin: a `break` closes us here
                    return  # clean end of stream
                except httpx.TransportError as exc:
                    if yielded_any:
                        raise  # partial stream: can't retry without duplicate delivery
                    last_exc = exc
                    reason = describe_error(self.url, exc)
                    _logger.warning("async stream attempt %d failed: %s", attempt, reason)
                    if attempt < len(QUERY_RETRY_DELAYS):
                        await asyncio.sleep(QUERY_RETRY_DELAYS[attempt])
        if last_exc is not None:
            raise last_exc


# ── Shared SSE helpers ────────────────────────────────────────────────────────

def _parse_sse_events(raw: dict) -> list[Event]:
    """Convert one raw SSE dict (from iter_sse) into zero or more typed Events.

    A list rather than a single event because the approval gate arrives on a chunk that also
    carries the human-readable message: the caller needs the prose *and* the machine-readable
    signal, and dropping either one loses something real.
    """
    # ki_event side-channel wrapper
    ki = raw.get("ki_event")
    if ki:
        parsed = parse_event(ki)
        return [parsed] if parsed is not None else []

    events: list[Event] = []
    choices = raw.get("choices", [])
    if choices:
        choice = choices[0]
        # Standard OpenAI streaming chunk → token event
        content_chunk = choice.get("delta", {}).get("content")
        if content_chunk:
            token = parse_event({"type": "token", "data": {"content": content_chunk}})
            if token is not None:
                events.append(token)
        # Approval gate. The server merges these fields onto the choice rather than sending a
        # `ki_event`, so a parser that inspects only `ki_event` and `delta.content` can never
        # produce the HitlRequestEvent this package exports -- the caller is left regexing
        # markdown to notice that the agent is waiting for a human.
        if choice.get("hitl_required"):
            gate = parse_event({
                "type": "hitl_request",
                "data": {
                    "action": choice.get("human_summary", "") or "",
                    "risk": choice.get("risk_level", "") or "",
                    "approval_id": choice.get("action_id", "") or "",
                },
            })
            if gate is not None:
                events.append(gate)
    elif "usage" in raw:
        # Usage at end of stream → usage event
        usage = parse_event({"type": "usage", "data": raw["usage"]})
        if usage is not None:
            events.append(usage)

    return events


def _warn_if_lossy(stats: SseStats) -> None:
    """Log what a stream lost. An SDK consumer gets a record instead of silence."""
    if stats.lossless:
        return
    _logger.warning(
        "sse stream was lossy: %d unparseable frame(s)%s%s",
        stats.dropped_frames,
        ", stream ended mid-frame" if stats.truncated_tail else "",
        f" — first: {stats.first_error!r}" if stats.first_error else "",
    )


def _parse_sse_chunk(raw: dict) -> Event | None:
    """First event for one raw SSE dict, or None. Kept for callers predating the list form."""
    events = _parse_sse_events(raw)
    return events[0] if events else None


async def _aiter_sse(
    response: httpx.Response, stats: SseStats | None = None
) -> AsyncIterator[dict]:  # type: ignore[return]
    """Async SSE parser — mirrors iter_sse but for httpx async streaming.

    Including its loss accounting: this is a second hand-written copy of the same parser, so
    an `SseStats` threaded through only the sync one would report a clean stream on every
    async call. See `SseStats` in kube_q.core.transport.
    """
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
                        if stats is not None:
                            stats._record_drop(payload)
    if stats is not None and any(
        line.startswith("data:") and line[len("data:"):].strip() not in ("", "[DONE]")
        for line in buffer.splitlines()
    ):
        stats.truncated_tail = True
