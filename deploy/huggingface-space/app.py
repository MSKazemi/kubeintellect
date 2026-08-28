"""KubeIntellect — Hugging Face Space.

A thin Gradio client over the public, read-only KubeIntellect demo API. Every
answer comes from a real Kubernetes cluster queried live: the backend fans out
to kubectl, Prometheus and Loki subagents and streams its work back over an
OpenAI-compatible SSE endpoint.

The Space renders the `ki_event` side channel (status / plan / tool_call /
tool_result) as collapsible activity blocks, and the assistant's tokens as the
answer. What the agent is allowed to do is decided by the role the configured
key holds, so this page asks the server for that role (`GET /v1/auth/whoami`)
and states it rather than asserting one: on the public demo it comes back
`readonly`, and a write is refused by RBAC before it executes — the transcript
shows the exact command the agent wanted to run and the denial it got back.
"""

from __future__ import annotations

import html
import json
import os
import uuid
from collections import OrderedDict
from collections.abc import Iterator

import gradio as gr
import httpx

try:  # provided by the ZeroGPU runtime; absent when running locally
    import spaces
except ImportError:
    spaces = None

API_BASE = os.environ.get("KI_API_BASE", "https://api.kubeintellect.com").rstrip("/")
# Public read-only key for the shared demo cluster — the same one printed in the
# project README. Override with a Space secret to point at your own deployment.
API_KEY = os.environ.get("KI_API_KEY", "ki-ro-dev")

CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 300.0

# Phase → label shown while the agent works.
PHASE_LABELS = {
    "loading": "Loading conversation context",
    "snapshot": "Fetching cluster snapshot",
    "analyzing": "Analyzing your request",
    "routing": "Routing to subagents",
    "synthesizing": "Correlating evidence",
}

if spaces is not None:
    # This app is pure I/O — it holds no model and needs no GPU. But a Gradio
    # Space on the free tier is pinned to ZeroGPU hardware, whose startup check
    # aborts with "No @spaces.GPU function detected" unless at least one
    # decorated function exists. This probe exists solely to satisfy that check;
    # nothing calls it, so no GPU is ever allocated.
    @spaces.GPU(duration=1)
    def _zerogpu_startup_probe() -> str:
        return "ok"


# Browser session (Gradio `session_hash`) → KubeIntellect agent thread id. The
# backend keys its LangGraph thread — and therefore multi-turn context and HITL
# resume — off `X-Session-ID`, so each visitor needs a stable id of their own.
_MAX_SESSIONS = 500
_SESSIONS: OrderedDict[str, str] = OrderedDict()


def _session_id(request: gr.Request | None) -> str:
    """Return this visitor's agent thread id, creating one on first use."""
    key = getattr(request, "session_hash", None) or "anonymous"
    sid = _SESSIONS.get(key)
    if sid is None:
        sid = str(uuid.uuid4())
        _SESSIONS[key] = sid
        while len(_SESSIONS) > _MAX_SESSIONS:
            _SESSIONS.popitem(last=False)  # evict the oldest visitor
    _SESSIONS.move_to_end(key)
    return sid


# ── What this key may actually do ─────────────────────────────────────────────
# The page used to assert `readonly` in fixed HTML. `KI_API_KEY` is configurable and the
# prefix is only a naming convention, so a self-hoster pointing this app at their own
# deployment with an operator key got a page stating the opposite of the truth, next to an
# agent that would then execute writes. Ask the server instead; if it will not answer, say
# that, and never fall back to the flattering assumption.

_ROLE_UNKNOWN = "unknown"
_role_cache: str | None = None


def key_role() -> str:
    """Return the role the configured key holds, or `_ROLE_UNKNOWN`.

    Cached on success only: the key does not change while the process lives, but a probe
    that failed because the backend was restarting must be retried rather than frozen into
    the page. A server too old to serve `/v1/auth/whoami` answers 404 and lands here too.
    """
    global _role_cache
    if _role_cache is not None:
        return _role_cache
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=8.0)) as client:
            response = client.get(
                f"{API_BASE}/v1/auth/whoami",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
        response.raise_for_status()
        role = str(response.json().get("role") or "").strip()
    except (httpx.HTTPError, ValueError):
        return _ROLE_UNKNOWN
    if not role:
        return _ROLE_UNKNOWN
    _role_cache = role
    return role


# ── Live cluster panel ────────────────────────────────────────────────────────
# Ground truth only. `/v1/namespaces` is a real, LLM-free read of the cluster, so
# it is safe to present as status. Anything the model *says* (pod counts, health)
# stays in the chat, where it is visibly the agent's answer rather than telemetry.


def cluster_panel() -> str:
    """Render the live namespace list, or an honest error box."""
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            response = client.get(
                f"{API_BASE}/v1/namespaces",
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
        response.raise_for_status()
        namespaces = response.json().get("namespaces") or []
    except (httpx.HTTPError, ValueError):
        return (
            '<div class="ki-cluster ki-cluster--down">'
            '<span class="ki-dot ki-dot--down"></span>'
            "<strong>Demo cluster unreachable.</strong>"
            "<span class=\"ki-muted\">The backend may be restarting — try again shortly.</span>"
            "</div>"
        )

    role = key_role()
    # "read-only" here was the same unchecked claim as the footer's, in three fewer words.
    access = "read-only" if role == "readonly" else html.escape(role)
    chips = "".join(f'<code class="ki-ns">{html.escape(ns)}</code>' for ns in namespaces)
    return (
        '<div class="ki-cluster">'
        '<div class="ki-cluster-head">'
        '<span class="ki-dot"></span>'
        f"<strong>Live demo cluster</strong>"
        f'<span class="ki-muted">{len(namespaces)} namespaces · {access}</span>'
        "</div>"
        f'<div class="ki-ns-row">{chips}</div>'
        "</div>"
    )


# ── Streaming ─────────────────────────────────────────────────────────────────


def _sse_lines(response: httpx.Response) -> Iterator[dict]:
    """Yield decoded JSON payloads from an SSE stream, skipping the DONE sentinel."""
    for raw in response.iter_lines():
        if not raw or not raw.startswith("data: "):
            continue
        data = raw[len("data: ") :].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            # A malformed frame should degrade the transcript, not kill the stream.
            continue


def _error_block(title: str, detail: str) -> gr.ChatMessage:
    return gr.ChatMessage(
        role="assistant",
        content=detail or "",
        metadata={"title": f"⚠️ {title}"},
    )


def _event_block(event: dict) -> gr.ChatMessage | None:
    """Map one `ki_event` frame to a collapsible activity block."""
    etype = event.get("type")

    if etype == "status":
        phase = event.get("phase", "")
        label = PHASE_LABELS.get(phase, event.get("message") or phase or "Working")
        return gr.ChatMessage(
            role="assistant",
            content=event.get("message") or "",
            metadata={"title": f"🔄 {label}", "status": "done"},
        )

    if etype == "plan":
        steps = event.get("steps") or []
        body = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        return gr.ChatMessage(
            role="assistant",
            content=body,
            metadata={"title": "🧭 Plan", "status": "done"},
        )

    if etype == "tool_call":
        return gr.ChatMessage(
            role="assistant",
            content=f"```\n{event.get('message', '')}\n```",
            metadata={"title": f"🛠️ {event.get('tool', 'tool')}", "status": "done"},
        )

    if etype == "tool_result":
        output = event.get("output") or ""
        if len(output) > 4000:
            output = output[:4000] + "\n… [truncated]"
        return gr.ChatMessage(
            role="assistant",
            content=f"```\n{output}\n```",
            metadata={"title": f"📄 {event.get('tool', 'tool')} output", "status": "done"},
        )

    if etype == "error":
        return _error_block("Agent error", event.get("message") or event.get("error") or "")

    return None  # unknown event types are ignored by design


def _turn(message: str, sid: str) -> Iterator[list]:
    """Stream one turn as a growing list of chat messages."""
    activity: list[gr.ChatMessage] = []
    answer = ""

    def snapshot() -> list:
        out = list(activity)
        if answer:
            out.append(gr.ChatMessage(role="assistant", content=answer))
        return out

    payload = {
        "model": "kubeintellect",
        "messages": [{"role": "user", "content": message}],
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "X-Session-ID": sid,  # ties the turn to the agent's thread
    }

    activity.append(
        gr.ChatMessage(
            role="assistant",
            content="Contacting the demo cluster…",
            metadata={"title": "⏳ Connecting", "status": "pending"},
        )
    )
    yield snapshot()

    timeout = httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream(
                "POST", f"{API_BASE}/v1/chat/completions", json=payload, headers=headers
            ) as response:
                if response.status_code != 200:
                    response.read()
                    activity[-1] = _error_block(
                        f"The demo API returned HTTP {response.status_code}.",
                        response.text[:500],
                    )
                    yield snapshot()
                    return

                activity.pop()  # drop the "Connecting" placeholder

                for chunk in _sse_lines(response):
                    event = chunk.get("ki_event")
                    if event:
                        block = _event_block(event)
                        if block is not None:
                            activity.append(block)
                            yield snapshot()
                        continue

                    for choice in chunk.get("choices") or []:
                        token = (choice.get("delta") or {}).get("content")
                        if token:
                            answer += token
                            yield snapshot()

    except httpx.TimeoutException:
        activity.append(
            _error_block(
                "The demo cluster took too long to answer.",
                "This is a small shared cluster — please try again in a moment.",
            )
        )
        yield snapshot()
        return
    except httpx.HTTPError as exc:
        activity.append(_error_block("Could not reach the demo API.", str(exc)))
        yield snapshot()
        return

    if not answer and not activity:
        activity.append(
            _error_block("The demo API closed the stream without answering.", "Please retry.")
        )
    yield snapshot()


def _last_user_message(history: list) -> str:
    for item in reversed(history or []):
        role = item.get("role") if isinstance(item, dict) else getattr(item, "role", None)
        if role == "user":
            content = (
                item.get("content") if isinstance(item, dict) else getattr(item, "content", "")
            )
            return str(content or "").strip()
    return ""


def submit_user(message: str, history: list) -> tuple:
    """Move the typed message into the transcript and clear the box."""
    message = (message or "").strip()
    if not message:
        return "", history or []
    return "", (history or []) + [gr.ChatMessage(role="user", content=message)]


def stream_bot(history: list, request: gr.Request) -> Iterator[list]:
    """Answer the last user message, streaming activity then the answer."""
    message = _last_user_message(history)
    if not message:
        yield history or []
        return
    base = list(history or [])
    for partial in _turn(message, _session_id(request)):
        yield base + partial


def reset(request: gr.Request) -> None:
    """Start a fresh agent thread when the user clears the conversation."""
    key = getattr(request, "session_hash", None) or "anonymous"
    _SESSIONS.pop(key, None)


# ── UI ────────────────────────────────────────────────────────────────────────

LOGO = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="52" height="52"
     role="img" aria-label="KubeIntellect">
  <defs>
    <linearGradient id="kiG" x1="30" y1="30" x2="226" y2="226" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5"/><stop offset="100%" stop-color="#06b6d4"/>
    </linearGradient>
  </defs>
  <rect width="256" height="256" rx="58" fill="#0b1020"/>
  <rect x="1" y="1" width="254" height="254" rx="57" fill="none" stroke="#1e2642" stroke-width="1.5"/>
  <path d="M 53,96 L 86,128 L 53,160" fill="none" stroke="url(#kiG)" stroke-width="14"
        stroke-linecap="round" stroke-linejoin="round"/>
  <text x="104" y="170" font-family="'JetBrains Mono','SF Mono',monospace"
        font-weight="700" font-size="112" fill="url(#kiG)">ki</text>
  <rect x="214" y="141" width="18" height="30" rx="2" fill="url(#kiG)"/>
</svg>
"""

HEADER = f"""
<div class="ki-header">
  <div class="ki-brand">{LOGO}
    <div>
      <h1>KubeIntellect</h1>
      <p>Human-governed AI SRE for Kubernetes — ask a <strong>real cluster</strong> anything,
         in plain English.</p>
    </div>
  </div>
  <div class="ki-links">
    <a href="https://kubeintellect.com/" target="_blank" rel="noopener">Website</a>
    <a href="https://github.com/MSKazemi/kubeintellect" target="_blank" rel="noopener">GitHub</a>
    <a href="https://pypi.org/project/kube-q/" target="_blank" rel="noopener">PyPI</a>
    <a href="https://doi.org/10.1007/s10723-026-09837-6" target="_blank" rel="noopener">Paper</a>
  </div>
</div>
"""

# One sentence per role, each matching the four-tier model documented in the server's
# app/api/v1/auth.py. A role this table does not know is described in the words the server
# used and nothing more — inventing permissions for an unrecognised role is how the original
# defect worked.
ROLE_SENTENCES = {
    "readonly": (
        "<strong>Read-only, shared cluster.</strong> This deployment's key holds the "
        "<code>readonly</code> role, so a write is refused by RBAC before it runs — with an "
        "operator key on your own deployment, that same command stops at a "
        "<strong>human approval</strong> prompt instead."
    ),
    "operator": (
        "<strong>Write-capable deployment.</strong> This key holds the <code>operator</code> "
        "role: create, apply, scale and exec are allowed and stop at a "
        "<strong>human approval</strong> prompt; delete, drain, replace and taint are refused "
        "by RBAC."
    ),
    "admin": (
        "<strong>Write-capable deployment.</strong> This key holds the <code>admin</code> "
        "role: high- and medium-risk operations are allowed, every one behind a "
        "<strong>human approval</strong> prompt, and writes to protected infrastructure "
        "namespaces are still blocked."
    ),
    "superadmin": (
        "<strong>Write-capable deployment.</strong> This key holds the <code>superadmin</code> "
        "role: every operation is allowed, each behind a <strong>human approval</strong> "
        "prompt, including writes to protected infrastructure namespaces."
    ),
}

_ROLE_UNAVAILABLE = (
    "<strong>Permissions unconfirmed.</strong> This deployment's backend did not report which "
    "role its key holds, so this page makes no claim about what the agent may do here — treat "
    "it as write-capable."
)


def role_sentence(role: str) -> str:
    """Describe what the key may do, in terms the server itself supplied."""
    if role == _ROLE_UNKNOWN:
        return _ROLE_UNAVAILABLE
    known = ROLE_SENTENCES.get(role)
    if known:
        return known
    return (
        "<strong>Permissions unconfirmed.</strong> This deployment's key holds the "
        f"<code>{html.escape(role)}</code> role, which this page has no description for — "
        "treat it as write-capable."
    )


def footer_panel() -> str:
    """Render the footer, naming the role the key actually holds."""
    return (
        '<div class="ki-footer">'
        f"<p>{role_sentence(key_role())} "
        "Please don't paste secrets: your question goes to the demo backend and on to an "
        "LLM provider.</p>"
        "<p>Run it on <strong>your own</strong> cluster: <code>pip install kube-q</code> · "
        '<a href="https://github.com/MSKazemi/kubeintellect#quick-start" target="_blank" '
        'rel="noopener">quick start</a> · AGPL-3.0 · built by '
        '<a href="https://github.com/MSKazemi" target="_blank" rel="noopener">'
        "Mohsen Seyedkazemi Ardebili</a></p>"
        "</div>"
    )

EXAMPLES = [
    ("🔎 Any unhealthy pods?", "Are any pods unhealthy, pending or restarting right now?"),
    ("📦 What's deployed?", "What is deployed in the kubeintellect namespace?"),
    ("⚠️ Recent warnings", "Show me the most recent warning events in the cluster."),
    ("🔒 Try a write →  blocked", "Scale the nginx deployment in the default namespace to 3 replicas"),
]

CSS = """
.gradio-container { max-width: 900px !important; margin: 0 auto !important; }
footer { display: none !important; }

.ki-header { text-align: center; padding: 4px 0 2px; }
.ki-brand { display: flex; align-items: center; justify-content: center; gap: 14px; }
.ki-brand h1 { font-size: 1.9rem; line-height: 1.1; margin: 0; letter-spacing: -0.02em; }
.ki-brand p { margin: 3px 0 0; font-size: 0.93rem; opacity: 0.85; text-align: left; }
.ki-links { margin-top: 10px; display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
.ki-links a {
  font-size: 0.82rem; text-decoration: none; padding: 3px 11px; border-radius: 999px;
  border: 1px solid rgba(128,128,128,0.32); opacity: 0.92;
}
.ki-links a:hover { border-color: #4f46e5; opacity: 1; }

.ki-cluster {
  border: 1px solid rgba(128,128,128,0.28); border-radius: 10px;
  padding: 10px 13px; margin: 14px 0 4px; font-size: 0.85rem;
}
.ki-cluster--down { border-color: rgba(220,38,38,0.5); }
.ki-cluster-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.ki-muted { opacity: 0.62; font-size: 0.8rem; }
.ki-dot {
  width: 8px; height: 8px; border-radius: 50%; background: #10b981;
  box-shadow: 0 0 0 3px rgba(16,185,129,0.18); flex: none;
}
.ki-dot--down { background: #dc2626; box-shadow: 0 0 0 3px rgba(220,38,38,0.18); }
.ki-ns-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
.ki-ns {
  font-size: 0.76rem; padding: 2px 8px; border-radius: 6px;
  background: rgba(79,70,229,0.12); border: 1px solid rgba(79,70,229,0.25);
}

.ki-footer { margin-top: 14px; font-size: 0.79rem; opacity: 0.72; line-height: 1.5; }
.ki-footer p { margin: 0 0 6px; }

#ki-examples button {
  font-size: 0.82rem !important; font-weight: 500 !important;
  min-height: 34px !important; padding: 5px 10px !important;
}
"""

with gr.Blocks(
    title="KubeIntellect — AI SRE for Kubernetes",
    fill_height=False,
) as demo:
    gr.HTML(HEADER)
    cluster = gr.HTML(cluster_panel)

    chatbot = gr.Chatbot(
        height=380,
        show_label=False,
        placeholder=(
            "<div style='text-align:center;opacity:0.6'>"
            "<p style='font-size:0.95rem;margin:0'>Ask about the live demo cluster.</p>"
            "<p style='font-size:0.82rem;margin:6px 0 0'>"
            "Expand the grey blocks in any answer to see the real <code>kubectl</code> "
            "calls behind it.</p></div>"
        ),
    )

    msg = gr.Textbox(
        placeholder="e.g. why is my api-server pod crashlooping?",
        show_label=False,
        lines=1,
        max_lines=4,
        autofocus=True,
        submit_btn=True,
    )

    with gr.Row(elem_id="ki-examples"):
        for label, prompt in EXAMPLES:
            gr.Button(label, size="sm", variant="secondary").click(
                lambda p=prompt: p, None, msg, queue=False
            ).then(submit_user, [msg, chatbot], [msg, chatbot], queue=False).then(
                stream_bot, chatbot, chatbot
            )

    footer = gr.HTML(footer_panel)

    msg.submit(submit_user, [msg, chatbot], [msg, chatbot], queue=False).then(
        stream_bot, chatbot, chatbot
    )
    chatbot.clear(reset, None, None)

    # Refresh the namespace panel on every page load, not just at boot.
    # `show_progress="hidden"` keeps Gradio's elapsed-time counter from flashing
    # over the panel while the (sub-second) namespace fetch runs.
    demo.load(cluster_panel, None, cluster, show_progress="hidden")
    # The footer states the key's role, so it is re-probed on load like the panel above:
    # a key rotated on the running Space must not leave a stale claim on the page.
    demo.load(footer_panel, None, footer, show_progress="hidden")


if __name__ == "__main__":
    # Gradio 6 moved `theme` and `css` from the Blocks constructor to launch().
    demo.queue(default_concurrency_limit=6).launch(
        theme=gr.themes.Soft(primary_hue="indigo", secondary_hue="cyan"),
        css=CSS,
    )
