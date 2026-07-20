"""
transport.py — Rendering-free HTTP/SSE primitives for kube_q.core.

This module has zero UI dependencies. It provides:
  - HTTP client factory (_make_client)
  - Header / payload builders
  - SSE line parser (_iter_sse)
  - Health check
  - Namespace list fetch (used by CLI /ns command)

The streaming loop lives in kube_q.transport (CLI layer) because it
currently drives a Rich Live context.  It will move here once the
KubeQClient async iterator is implemented (Phase 1 Step 8).
"""

import json
import logging
from typing import Any

import httpx

# ── Retry config ───────────────────────────────────────────────────────────────
QUERY_RETRY_DELAYS = (2, 5, 10)  # seconds between attempts (3 total)

# ── DNS error keywords ─────────────────────────────────────────────────────────
_DNS_KEYWORDS = ("Name or service not known", "nodename nor servname", "getaddrinfo")

# ── Module logger ──────────────────────────────────────────────────────────────
_logger = logging.getLogger(__name__)

# ── Debug mode ────────────────────────────────────────────────────────────────
_debug: bool = False


def set_debug(enabled: bool) -> None:
    """Enable/disable debug HTTP logging."""
    global _debug
    _debug = enabled


# ── Error helpers ──────────────────────────────────────────────────────────────

def describe_error(url: str, exc: Exception) -> str:
    """Return a human-readable reason for a connection failure."""
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc)
        if any(k in msg for k in _DNS_KEYWORDS):
            host = url.split("//")[-1].split("/")[0]
            return f"DNS resolution failed for '{host}'"
        return f"Connection refused — nothing is listening at {url}"
    if isinstance(exc, httpx.TimeoutException):
        return "Request timed out — API did not respond in time"
    if isinstance(exc, httpx.ProxyError):
        return f"Proxy error: {exc}"
    if isinstance(exc, httpx.RemoteProtocolError):
        return f"Server closed the connection unexpectedly: {exc}"
    if isinstance(exc, httpx.NetworkError):
        return f"Network error: {exc}"
    return f"Unexpected error: {exc}"


# ── Debug event hooks ─────────────────────────────────────────────────────────

def _hook_request(request: httpx.Request) -> None:
    safe_headers = {k: v for k, v in request.headers.items() if k.lower() != "authorization"}
    body = request.content.decode("utf-8", errors="replace")
    _logger.debug("→ %s %s  headers=%s", request.method, request.url, safe_headers)
    if body:
        _logger.debug("  body=%s", body[:4000])


def _hook_response(response: httpx.Response) -> None:
    _logger.debug("← %d %s", response.status_code, response.url)


# ── Client factory ─────────────────────────────────────────────────────────────

def make_client(ca_cert: str | None, timeout: float = 120.0) -> httpx.Client:
    """Return a configured httpx.Client."""
    hooks: dict = {"request": [_hook_request], "response": [_hook_response]} if _debug else {}
    return httpx.Client(timeout=timeout, verify=ca_cert if ca_cert else True, event_hooks=hooks)


# ── Request builders ──────────────────────────────────────────────────────────

def build_headers(
    api_key: str | None,
    session_id: str,
    request_id: str,
    *,
    accept: str | None = None,
    auth_scheme: str = "bearer",
) -> dict[str, str]:
    """Build request headers.

    ``auth_scheme`` controls how the API key is sent:
      * ``"bearer"`` — ``Authorization: Bearer <key>`` (kube-q, OpenAI)
      * ``"api-key"`` — ``api-key: <key>`` (Azure OpenAI)
      * ``"none"`` — no auth header (even if api_key is set)
    """
    headers: dict[str, str] = {
        "X-Session-ID": session_id,
        "X-Request-ID": request_id,
    }
    if api_key and auth_scheme != "none":
        if auth_scheme == "api-key":
            headers["api-key"] = api_key
        else:  # "bearer" (default)
            headers["Authorization"] = f"Bearer {api_key}"
    if accept:
        headers["Accept"] = accept
    return headers


def build_payload(
    messages: list[dict],
    user: str,
    stream: bool,
    model: str = "kubeintellect-v2",
    auto_approve: bool = False,
) -> dict:
    payload: dict = {
        "model": model,
        "messages": [messages[-1]],
        "stream": stream,
        "user": user,
    }
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if auto_approve:
        payload["auto_approve"] = True
    return payload


# ── SSE parser ────────────────────────────────────────────────────────────────

def iter_sse(response: httpx.Response) -> Any:
    """Yield parsed SSE data objects. Handles multi-line data and [DONE] sentinel."""
    buffer = ""
    for raw_chunk in response.iter_text():
        buffer += raw_chunk
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            for line in block.splitlines():
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        pass


# ── Health check ──────────────────────────────────────────────────────────────

def check_health(
    url: str,
    *,
    api_key: str | None = None,
    ca_cert: str | None = None,
    timeout: float = 5.0,
    health_path: str | None = "/healthz",
    auth_scheme: str = "bearer",
) -> tuple[bool, str]:
    """Check API reachability. Returns (ok, reason).

    If ``health_path`` is None, returns (True, "") without making a network call —
    used for backends (OpenAI, Azure) that don't expose a health endpoint.
    """
    if health_path is None:
        return True, ""
    headers: dict[str, str] = {}
    if api_key:
        if auth_scheme == "api-key":
            headers["api-key"] = api_key
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    _logger.debug("check_health url=%s path=%s", url, health_path)
    try:
        with make_client(ca_cert, timeout=timeout) as client:
            r = client.get(f"{url}{health_path}", headers=headers)
        if r.status_code == 200:
            return True, ""
        if r.status_code == 401:
            return False, "Authentication required — set KUBE_Q_API_KEY or pass --api-key"
        return False, f"HTTP {r.status_code} from {url}{health_path}"
    except httpx.ConnectError as e:
        msg = str(e)
        if any(k in msg for k in _DNS_KEYWORDS):
            host = url.split("//")[-1].split("/")[0]
            return False, f"DNS resolution failed for '{host}' — check the hostname or /etc/hosts"
        return False, f"Connection refused — nothing is listening at {url}"
    except httpx.TimeoutException:
        return False, f"Connection timed out — {url} did not respond within 5 s"
    except Exception as e:
        return False, f"Unexpected error: {e}"


# ── Namespace fetch ───────────────────────────────────────────────────────────

def fetch_namespaces(
    url: str,
    user_id: str,
    *,
    api_key: str | None = None,
    ca_cert: str | None = None,
    timeout: float = 3.0,
) -> list[str] | None:
    """Fetch the list of known namespaces from the backend.

    Returns the namespace list on success, or None if the backend is
    unreachable / returns an unexpected response.
    """
    req_headers: dict[str, str] = {"X-User-ID": user_id}
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"
    try:
        with make_client(ca_cert, timeout=timeout) as client:
            r = client.get(f"{url}/v1/namespaces", headers=req_headers)
        if r.status_code == 200:
            return r.json().get("namespaces", [])
        return None
    except Exception:
        return None
