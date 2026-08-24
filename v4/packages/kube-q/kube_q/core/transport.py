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
from dataclasses import dataclass
from typing import Any, NamedTuple

import httpx

# ── Retry config ───────────────────────────────────────────────────────────────
QUERY_RETRY_DELAYS = (2, 5, 10)  # seconds between attempts (3 total)

# One source of truth for both health checks. `health()` is documented as a *fast* connectivity
# check on both clients, and the async one used to run on the 120 s query timeout instead.
HEALTH_TIMEOUT = 5.0
HEALTH_PATH = "/healthz"

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

@dataclass
class SseStats:
    """What a stream lost, for a caller that needs to know it lost something.

    `iter_sse` used to answer a malformed frame with `except JSONDecodeError: pass`, so a
    truncated or corrupted frame was indistinguishable from a frame the server never sent.
    Every command built on SSE inherited that, and the consequence is worst where the frame
    carries a verdict rather than prose: `kq replay`'s `replay_meta` holds `chain_valid`, so a
    dropped one turned "the audit chain is broken" into "the audit chain was never mentioned".

    Counting rather than raising is deliberate. Raising would abort the interactive chat
    stream — the highest-traffic surface in the product — on a single bad frame, when a partial
    answer is exactly what the user wants; logging alone would leave the loss unobservable to
    the caller, which is the defect itself. A caller that must be fail-closed inspects this and
    refuses; chat degrades and says so.
    """

    dropped_frames: int = 0
    first_error: str | None = None      # the first unparseable payload, truncated
    truncated_tail: bool = False        # the stream ended mid-frame, with no [DONE]

    @property
    def lossless(self) -> bool:
        return self.dropped_frames == 0 and not self.truncated_tail

    def _record_drop(self, payload: str) -> None:
        self.dropped_frames += 1
        if self.first_error is None:
            self.first_error = payload[:200]


def iter_sse(response: httpx.Response, stats: SseStats | None = None) -> Any:
    """Yield parsed SSE data objects. Handles multi-line data and [DONE] sentinel.

    Pass `stats` to learn what was lost; see `SseStats`. Without it the behaviour is exactly
    as before, so no existing caller changes.
    """
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
                        if stats is not None:
                            stats._record_drop(payload)
    # A stream that ends without the blank-line terminator leaves a frame in the buffer that
    # the loop above can never see. It is not yielded — that would change what callers
    # receive — but it is no longer lost in silence.
    if stats is not None and any(
        line.startswith("data:") and line[len("data:"):].strip() not in ("", "[DONE]")
        for line in buffer.splitlines()
    ):
        stats.truncated_tail = True


# ── Health check ──────────────────────────────────────────────────────────────

def health_status_reason(url: str, status_code: int, health_path: str) -> tuple[bool, str]:
    """Classify a health-endpoint *status code* into (ok, reason).

    Lives here, once, because there are two health checks — the sync `check_health` below and
    `AsyncKubeQClient.health` — and until 2026-08-24 each carried its own copy of this and of
    `health_failure_reason`. They had already drifted: the async copy reported a DNS failure as
    "Connection refused — nothing is listening at …", which names the wrong cause and sends the
    reader to check the port when the hostname is what does not resolve.
    """
    if status_code == 200:
        return True, ""
    if status_code == 401:
        return False, "Authentication required — set KUBE_Q_API_KEY or pass --api-key"
    return False, f"HTTP {status_code} from {url}{health_path}"


def health_failure_reason(url: str, exc: BaseException, timeout: float) -> str:
    """Classify a health-check *exception* into a reason. Counterpart of the above.

    Distinct from `describe_error` on purpose: this wording tells the reader what to go and fix
    (the hostname, the listening port, the timeout), and it names the timeout actually in force —
    the message used to say "5 s" whatever the caller passed.
    """
    if isinstance(exc, httpx.ConnectError):
        if any(k in str(exc) for k in _DNS_KEYWORDS):
            host = url.split("//")[-1].split("/")[0]
            return f"DNS resolution failed for '{host}' — check the hostname or /etc/hosts"
        return f"Connection refused — nothing is listening at {url}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Connection timed out — {url} did not respond within {timeout:g} s"
    return f"Unexpected error: {exc}"


def check_health(
    url: str,
    *,
    api_key: str | None = None,
    ca_cert: str | None = None,
    timeout: float = HEALTH_TIMEOUT,
    health_path: str | None = HEALTH_PATH,
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
        return health_status_reason(url, r.status_code, health_path)
    except Exception as e:  # noqa: BLE001 — every failure is a health verdict, never a raise
        return False, health_failure_reason(url, e, timeout)


# ── Namespace fetch ───────────────────────────────────────────────────────────

class NamespaceListing(NamedTuple):
    """The namespaces the backend will show us, and whether that is all of them.

    `names is None` means we could not find out. `complete is False` means the backend told us
    it removed some — a protected namespace is still a namespace, and *absent from this list*
    then stops being evidence that it does not exist. Keeping the two apart is the entire point:
    the caller's only wrong move is to report a withheld namespace as missing.
    """

    names: list[str] | None
    complete: bool = True


def namespace_verdict(listing: "NamespaceListing", namespace: str) -> bool | None:
    """Does `namespace` exist? `True`, `False`, or **`None` — could not be established.**

    The third answer is the one that has to survive. A caller that collapses it into `False`
    tells an operator a namespace does not exist when the truth is that we were not shown it:
    the backend was unreachable, or it withheld the namespace by policy and said so. Only a
    definite absence from a listing that claims to be whole is evidence of absence.
    """
    if listing.names is None:
        return None
    if namespace in listing.names:
        return True
    return False if listing.complete else None


def fetch_namespace_listing(
    url: str,
    user_id: str,
    *,
    api_key: str | None = None,
    ca_cert: str | None = None,
    timeout: float = 3.0,
) -> NamespaceListing:
    """Fetch the namespaces the backend will show us, and whether the list is the whole set.

    The backend filters `KUBECTL_BLOCKED_NAMESPACES` out of this listing and, since 2026-08-24,
    says so in a `withheldByPolicy` field. Before that it removed them in silence, and this
    client — which is careful enough to keep three states rather than two — had no way to reach
    the third one, so `/ns monitoring` reported a namespace that exists as **not found**.
    """
    req_headers: dict[str, str] = {"X-User-ID": user_id}
    if api_key:
        req_headers["Authorization"] = f"Bearer {api_key}"
    try:
        with make_client(ca_cert, timeout=timeout) as client:
            r = client.get(f"{url}/v1/namespaces", headers=req_headers)
        if r.status_code == 200:
            body = r.json()
            names = body.get("namespaces")
            # Only a real list is an answer. A 200 whose body we cannot read is "unknown", not
            # "zero" — the caller treats an empty list as proof a namespace does not exist.
            if not isinstance(names, list):
                return NamespaceListing(None)
            # An older backend has no such field, and its listing may well be short without
            # saying so. Absent marker ⇒ assumed complete, which is what this client did before
            # the field existed; the server it ships with always sends it.
            return NamespaceListing(names, not str(body.get("withheldByPolicy") or ""))
        return NamespaceListing(None)
    except Exception:
        return NamespaceListing(None)


def fetch_namespaces(
    url: str,
    user_id: str,
    *,
    api_key: str | None = None,
    ca_cert: str | None = None,
    timeout: float = 3.0,
) -> list[str] | None:
    """The namespaces the backend will show us, or None if we could not find out.

    Kept as the name every caller already imports. A caller that must not confuse *withheld*
    with *absent* wants :func:`fetch_namespace_listing` instead — this one cannot tell you.
    """
    return fetch_namespace_listing(
        url, user_id, api_key=api_key, ca_cert=ca_cert, timeout=timeout
    ).names


# ── Server-side error text ────────────────────────────────────────────────────

def server_detail(response) -> str | None:
    """The server's own `detail` for a response, normalised, or None if it said nothing.

    One implementation, because there were two: `explain()` below read it out of an exception's
    response and `replay_cmd._detail` read it out of a response directly, and only the first
    knew that FastAPI sends validation errors as a *list* of dicts — so the other rendered a 422
    as `[{'type': 'missing', ...}]`. Any command that inspects a status code before raising
    needs this form; `explain()` is only reachable once something has raised.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body is still an error body
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, list):                      # FastAPI validation errors
        detail = "; ".join(
            str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail
        )
    return detail if isinstance(detail, str) and detail.strip() else None


def explain(exc: BaseException) -> str:
    """The server's own words for a failure, not just the status line.

    `str(httpx.HTTPStatusError)` renders as "Client error '403 Forbidden' for url ...", which
    tells an operator that something was refused but never *why* — the reason the server took
    the trouble to put in `detail` is discarded. Falls back to `str(exc)` whenever there is no
    readable detail, so a transport error or an HTML error page still prints something.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    detail = server_detail(response)
    if detail is None:
        return str(exc)
    return f"{detail} (HTTP {response.status_code})"
