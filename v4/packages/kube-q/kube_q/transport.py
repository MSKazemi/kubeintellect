"""
cli_transport.py — HTTP communication: SSE streaming, non-streaming query, health check.

Rendering-free primitives live in kube_q.core.transport.
This module adds the Rich Live rendering layer on top.
"""

import json
import logging
import time
import uuid
from collections.abc import Callable

import httpx
from rich.console import Group, RenderableType
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text

from kube_q.cli.renderer import (
    _plain_output,
    console,
    error_timestamp,
    plan_panel,
    print_response,
    render_error_event,
    render_plan,
    render_status,
    render_tool_call,
)
from kube_q.core.transport import (
    QUERY_RETRY_DELAYS,
    build_headers,
    build_payload,
    check_health,  # noqa: F401 (re-export for callers)
    describe_error,
    iter_sse,
    make_client,
)

# ── Module logger ──────────────────────────────────────────────────────────────
_logger = logging.getLogger(__name__)

# ── Legacy aliases kept for callers that import from kube_q.transport ─────────
_QUERY_RETRY_DELAYS = QUERY_RETRY_DELAYS
_make_client = make_client
_build_payload = build_payload
_request_headers = build_headers
_describe_error = describe_error
_iter_sse = iter_sse


def _stream_once(
    url: str,
    payload: dict,
    headers: dict,
    client: httpx.Client,
    on_status: Callable[[dict, Live, bool], None] = render_status,
    on_tool_call: Callable[[dict], None] = render_tool_call,
    on_error: Callable[[dict], None] = render_error_event,
    on_plan: Callable[[dict], None] = render_plan,
    chat_path: str = "/v1/chat/completions",
    agent_name: str = "kube-q",
) -> tuple[str, bool, str | None, dict | None]:
    """One streaming attempt. Raises httpx.TransportError on connection failure.

    Returns (full_text, hitl_pending, action_id, usage_dict).
    Rendering callbacks are injected so core transport logic has no Rich deps.
    """
    full_text = ""
    hitl_pending = False
    action_id: str | None = None
    last_usage: dict | None = None
    pending_plan: dict | None = None  # persisted (re-printed) after Live exits
    t0 = time.monotonic()

    # The live region is a group of [plan panel (optional), body]. The body is a
    # spinner until the first token, then the streaming markdown. Keeping the plan
    # in the group lets its step icons update in real time as the server emits
    # successive plan events; we collapse it out of the final frame so it isn't
    # duplicated by the persistent re-print below.
    plan_renderable: Panel | None = None
    body: RenderableType = Spinner(
        "dots", text=Text(f" {agent_name} is thinking…", style="cyan")
    )

    def _compose() -> RenderableType:
        return Group(plan_renderable, body) if plan_renderable is not None else body

    with Live(
        _compose(),
        console=console,
        refresh_per_second=12,
        transient=True,
    ) as live:
        with client.stream("POST", f"{url}{chat_path}",
                           json=payload, headers=headers) as resp:
            if resp.status_code == 401:
                live.stop()
                console.print(
                    "[red]Authentication required.[/red] "
                    "Set [bold]KUBE_Q_API_KEY[/bold] or pass "
                    "[bold]--api-key[/bold] with a valid key.\n"
                    "[dim]Ask your system administrator for an API key.[/dim]"
                )
                return "", False, None, None
            if resp.status_code != 200:
                body = resp.read().decode()
                live.stop()
                console.print(f"[red][HTTP {resp.status_code}] {body}[/red]")
                return "", False, None, None

            first_token = True
            for event in iter_sse(resp):
                _logger.debug("sse event: %s", event)

                # ── Side-channel ki_event ────────────────────────────────────
                ki = event.get("ki_event")
                if ki:
                    kind = ki.get("type")
                    if kind == "status":
                        msg = ki.get("message") or ki.get("phase") or ""
                        if msg and not first_token:
                            # Spinner is gone — drop a dim breadcrumb above the area.
                            console.print(f"[muted]⚙ {msg}[/muted]")
                        elif msg:
                            body = Spinner(
                                "dots",
                                text=Text.assemble((" ", ""), (msg, "dim cyan")),
                            )
                            live.update(_compose())
                    elif kind == "tool_call":
                        on_tool_call(ki)
                    elif kind == "plan":
                        # Render live (group updates in place) AND remember it so
                        # the final state is re-printed persistently after Live.
                        pending_plan = ki
                        panel = plan_panel(ki)
                        if panel is not None:
                            plan_renderable = panel
                            live.update(_compose())
                    elif kind == "error":
                        on_error(ki)
                    elif kind == "usage":
                        last_usage = ki
                        _logger.debug("usage captured from ki_event: %s", ki)
                    if "usage" in ki:
                        last_usage = ki["usage"]
                        _logger.debug("usage captured from ki_event.usage: %s", last_usage)
                    continue

                # ── Standard OpenAI streaming format ──────────────────────────
                if "usage" in event:
                    last_usage = event["usage"]
                    _logger.debug("usage captured from standard event: %s", last_usage)
                choices = event.get("choices", [])
                if not choices:
                    continue
                choice = choices[0]
                content = choice.get("delta", {}).get("content", "")
                finish = choice.get("finish_reason")
                if content:
                    if first_token:
                        live.transient = False
                        first_token = False
                    full_text += content
                    body = Markdown(full_text) if not _plain_output else Text(full_text)
                    live.update(_compose())
                if finish == "stop":
                    if choice.get("hitl_required"):
                        hitl_pending = True
                        action_id = choice.get("action_id")
                    elif "🛑" in full_text:
                        hitl_pending = True
                        console.print(
                            "[dim]Warning: HITL triggered via emoji fallback "
                            "— server should be upgraded to send hitl_required.[/dim]"
                        )

        # Collapse the plan out of the final live frame so it isn't shown twice
        # — it is re-printed persistently below the answer via on_plan().
        if plan_renderable is not None:
            live.update(body)

    elapsed = time.monotonic() - t0
    if last_usage is None:
        _logger.debug(
            "stream completed without usage data — "
            "server may not support stream_options.include_usage"
        )
    if pending_plan is not None:
        on_plan(pending_plan)
    if full_text:
        total_tokens = last_usage.get("total_tokens") if last_usage else None
        token_str = f" · {total_tokens:,} tokens" if total_tokens else ""
        console.print(
            f"[bold cyan]{agent_name}[/bold cyan]  [dim]({elapsed:.1f}s{token_str})[/dim]"
        )
        console.print()
    return full_text, hitl_pending, action_id, last_usage


# ── Public query functions ─────────────────────────────────────────────────────

def stream_query(
    url: str,
    messages: list[dict],
    session_id: str,
    user: str,
    *,
    api_key: str | None = None,
    ca_cert: str | None = None,
    timeout: float = 120.0,
    request_id: str | None = None,
    model: str = "kubeintellect-v2",
    chat_path: str = "/v1/chat/completions",
    auth_scheme: str = "bearer",
    auto_approve: bool = False,
    agent_name: str = "kube-q",
) -> tuple[str, bool, str | None, dict | None]:
    """Send a streaming chat request. Returns (full_text, hitl_pending, action_id, usage)."""
    if request_id is None:
        request_id = f"req-{uuid.uuid4()}"
    _logger.info(
        "stream_query session=%s user=%s request=%s url=%s",
        session_id, user, request_id, url,
    )
    payload = build_payload(messages, user, True, model, auto_approve=auto_approve)
    headers = build_headers(
        api_key, session_id, request_id,
        accept="text/event-stream", auth_scheme=auth_scheme,
    )

    with make_client(ca_cert, timeout=timeout) as client:
        for attempt in range(len(QUERY_RETRY_DELAYS) + 1):
            if attempt > 0:
                delay = QUERY_RETRY_DELAYS[attempt - 1]
                console.print(
                    f"[dim]  Retrying in {delay}s… "
                    f"(attempt {attempt}/{len(QUERY_RETRY_DELAYS)})[/dim]"
                )
                time.sleep(delay)
            try:
                return _stream_once(url, payload, headers, client, chat_path=chat_path,
                                    agent_name=agent_name)
            except httpx.TransportError as exc:
                reason = describe_error(url, exc)
                if attempt == 0:
                    console.print(
                        f"\n{error_timestamp()}[red]Disconnected:[/red] {reason}"
                    )
                else:
                    console.print(f"{error_timestamp()}[dim]    → {reason}[/dim]")

    console.print(
        f"{error_timestamp()}[red]  All retries failed.[/red] "
        "[dim]Check your connection and API URL, then try again.[/dim]"
    )
    return "", False, None, None


def non_stream_query(
    url: str,
    messages: list[dict],
    session_id: str,
    user: str,
    *,
    api_key: str | None = None,
    ca_cert: str | None = None,
    timeout: float = 120.0,
    request_id: str | None = None,
    model: str = "kubeintellect-v2",
    chat_path: str = "/v1/chat/completions",
    auth_scheme: str = "bearer",
    agent_name: str = "kube-q",
) -> tuple[str, bool, str | None, dict | None]:
    """Send a non-streaming chat request.

    Returns (response_text, hitl_pending, action_id, usage).
    """
    if request_id is None:
        request_id = f"req-{uuid.uuid4()}"
    _logger.info(
        "non_stream_query session=%s user=%s request=%s url=%s",
        session_id, user, request_id, url,
    )
    payload = build_payload(messages, user, False, model)
    headers = build_headers(api_key, session_id, request_id, auth_scheme=auth_scheme)

    with make_client(ca_cert, timeout=timeout) as client:
        for attempt in range(len(QUERY_RETRY_DELAYS) + 1):
            if attempt > 0:
                delay = QUERY_RETRY_DELAYS[attempt - 1]
                console.print(
                    f"[dim]  Retrying in {delay}s… "
                    f"(attempt {attempt}/{len(QUERY_RETRY_DELAYS)})[/dim]"
                )
                time.sleep(delay)
            try:
                t0 = time.monotonic()
                resp = client.post(
                    f"{url}{chat_path}", json=payload, headers=headers
                )
                elapsed = time.monotonic() - t0

                if resp.status_code == 401:
                    console.print(
                        "[red]Authentication required.[/red] "
                        "Set [bold]KUBE_Q_API_KEY[/bold] or pass "
                        "[bold]--api-key[/bold] with a valid key.\n"
                        "[dim]Ask your system administrator for an API key.[/dim]"
                    )
                    return "", False, None, None
                if resp.status_code != 200:
                    console.print(f"[red][HTTP {resp.status_code}] {resp.text}[/red]")
                    return "", False, None, None

                try:
                    data = resp.json()
                    _logger.debug("non_stream response body=%s", resp.text[:4000])
                except json.JSONDecodeError as e:
                    console.print(f"[red]Invalid JSON from server: {e}[/red]")
                    _logger.error("non_stream invalid JSON: %s — body=%s", e, resp.text[:500])
                    return "", False, None, None

                try:
                    choice_data = data["choices"][0]
                    text = choice_data["message"]["content"]
                except (KeyError, IndexError) as e:
                    console.print(
                        f"[red]Unexpected response structure (missing {e}): {data}[/red]"
                    )
                    return "", False, None, None

                hitl_pending = choice_data.get("hitl_required", False)
                action_id = choice_data.get("action_id") if hitl_pending else None
                usage: dict | None = data.get("usage")

                if not hitl_pending and "🛑" in text:
                    hitl_pending = True
                    console.print(
                        "[dim]Warning: HITL triggered via emoji fallback "
                        "— server should be upgraded to send hitl_required.[/dim]"
                    )

                total_tokens = usage.get("total_tokens") if usage else None
                token_str = f" · {total_tokens:,} tokens" if total_tokens else ""
                console.print(
                    f"[bold cyan]{agent_name}:[/bold cyan]  [dim]({elapsed:.1f}s{token_str})[/dim]"
                )
                print_response(text)
                return text, hitl_pending, action_id, usage

            except httpx.TransportError as exc:
                reason = describe_error(url, exc)
                if attempt == 0:
                    console.print(
                        f"\n{error_timestamp()}[red]Disconnected:[/red] {reason}"
                    )
                else:
                    console.print(f"{error_timestamp()}[dim]    → {reason}[/dim]")

    console.print(
        f"{error_timestamp()}[red]  All retries failed.[/red] "
        "[dim]Check your connection and API URL, then try again.[/dim]"
    )
    return "", False, None, None
