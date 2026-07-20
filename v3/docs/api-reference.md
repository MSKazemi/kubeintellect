---
description: >-
  KubeIntellect HTTP API — the OpenAI-compatible streaming chat endpoint, the
  SSE wire format and event types, the human-in-the-loop approval flow, auth, and
  the supporting endpoints.
---

# API Reference

KubeIntellect exposes a small HTTP API served by FastAPI (`app/main.py`,
`app/api/v1/`). The main entry point — `POST /v1/chat/completions` — is **OpenAI
chat-completions compatible**, so any OpenAI-style client can drive it, while a
side channel carries Kubernetes-specific events (tool calls, plans, approval
prompts).

- **Base URL:** the server address (e.g. `http://localhost:8000`).
- **API version prefix:** `/v1`.
- **Content type for streaming:** `text/event-stream` (SSE).

The v3 server registers exactly four route groups (`app/api/v1/router.py`):
`chat/completions`, `namespaces`, `events/replay`, and `healthz`.

---

## Authentication

Send your API key as a Bearer token:

```http
Authorization: Bearer ki-admin-xxxxxxxxxxxxxxxx
```

The key determines the caller's role and therefore what the agent may do
(`superadmin` / `admin` / `operator` / `readonly`). Roles are resolved once per
request in `app/api/v1/auth.py`. If no keys are configured server-side, auth is
disabled and every caller is treated as `admin` (intended for localhost only).
See [Security](security.md) for the full role model.

---

## `POST /v1/chat/completions`

Send a message, stream back the agent's investigation and answer.

### Request

| Header | Required | Purpose |
|---|---|---|
| `Authorization: Bearer <key>` | when auth is enabled | Identifies the caller's role. |
| `X-Session-ID: <id>` | recommended | Ties the request to a LangGraph thread. Reuse the same value across turns to keep history **and to approve/deny pending actions**. Generated if omitted. |
| `Content-Type: application/json` | yes | |

Body (`application/json`):

```json
{
  "model": "kubeintellect",
  "messages": [
    {"role": "user", "content": "why is the checkout pod crashing?"}
  ],
  "stream": true,
  "user": "default",
  "auto_approve": false
}
```

| Field | Type | Default | Notes |
|---|---|---|---|
| `model` | string | `"kubeintellect"` | Accepted for OpenAI compatibility; the server always uses its configured models. |
| `messages` | array | — | Chat messages. The **last `user` message** is the query. At least one `user` message is required (`422` otherwise). |
| `stream` | bool | `true` | Must be `true` — `stream=false` returns `422`. |
| `user` | string | `"default"` | Opaque caller id, recorded in the audit log and used as the [memory](memory.md) key. |
| `auto_approve` | bool | `false` | Skip HITL gates for this request (trusted automation / evaluation). |

### Response — SSE stream

The response is a stream of `data:` frames built in
`app/api/v1/endpoints/chat_completions.py`. Most are standard OpenAI
`chat.completion.chunk` objects; KubeIntellect adds a handshake frame, a
`ki_event` side channel, and HITL fields.

**1. Handshake (always first):**

```text
data: {"protocol_version":"1.0","object":"stream.start","session_id":"<sid>"}
```

**2. Answer tokens** — standard OpenAI chunks; concatenate
`choices[0].delta.content`. Only the **coordinator's** tokens are streamed —
subagent model output is internal and suppressed:

```text
data: {"id":"chatcmpl-…","object":"chat.completion.chunk","model":"kubeintellect","choices":[{"index":0,"delta":{"content":"The ","role":"assistant"},"finish_reason":null}]}
```

**3. `ki_event` side channel** — progress events. These carry `ki_event` at the
top level and an **empty `choices` array**, so OpenAI-only clients ignore them
safely:

```text
data: {"object":"chat.completion.chunk","model":"kubeintellect","ki_event":{"type":"tool_call","tool":"run_kubectl","message":"Running: kubectl get pods -n prod"},"choices":[]}
```

| `ki_event.type` | Fields | Meaning |
|---|---|---|
| `status` | `phase`, `message` | Phase transition (e.g. loading, snapshot seeded, dispatching to a subagent, findings saved). |
| `tool_call` | `tool`, `message` | The agent is about to run a tool/command. Covers `run_kubectl`, `query_prometheus`, `query_loki`, `refresh_snapshot`, `lookup_playbook`, `read_memory`, `write_memory`. |
| `tool_result` | `tool`, `output` | Result of a tool call (first ~500 chars). |
| `plan` | `steps` | The visible investigation plan (a list of `{description, status}` steps), emitted from the coordinator's `write_todos`. |

**4. Approval request (HITL)** — when the agent wants to run a write operation,
it emits a normal content chunk **plus** approval fields on the `choices` entry:

```json
{
  "choices": [{
    "index": 0,
    "delta": {"content": "…approval prompt as markdown…", "role": "assistant"},
    "hitl_required": true,
    "action_id": "…",
    "risk_level": "medium",
    "human_summary": "kubectl scale deployment/web --replicas=5 -n prod"
  }]
}
```

`risk_level` is `high` (delete, drain, replace, taint) or `medium` (patch, apply,
scale, exec, cordon, uncordon, create, run) — see `app/tools/kubectl_tool.py`.
The stream **pauses** here. To continue, send a **new request with the same
`X-Session-ID`** whose user message is an approval (`yes`, `approve`, `/approve`,
`proceed`, `go ahead`, …) or denial (`no`, `deny`, `cancel`, `abort`, …). Saying
`approve all` / `auto-approve` approves writes for the rest of the session. The
exact phrases live in `app/agent/hitl.py`; see
[Security → HITL](security.md#4-hitl-human-in-the-loop-gate).

**5. Completion (always last):**

```text
data: {"…","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

**Keepalive:** if the stream is silent for 15 seconds, the server sends an SSE
comment line `: heartbeat` to keep the connection open. Ignore these.

### Example

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KUBE_Q_API_KEY" \
  -H "X-Session-ID: demo-1" \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role":"user","content":"what pods are broken in demo-rca?"}],
    "stream": true
  }'
```

Approve a pending action by re-posting to the **same session**:

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KUBE_Q_API_KEY" \
  -H "X-Session-ID: demo-1" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"yes"}],"stream":true}'
```

!!! tip "Use the `kq` client"
    [`kq`](cli-reference.md#kq-query-client) already speaks this protocol —
    streaming, `ki_event` rendering, and approvals included. Implement the wire
    format directly only when embedding KubeIntellect in your own product.

---

## `GET /v1/events/replay/{session_id}`

Replay the stored event history for a session as an SSE stream — useful for
post-mortem debugging or UIs that reconnect. Returns the same `ki_event` /
content frames the live stream produced (from the in-memory history in
`app/streaming/emitter.py`), then `[DONE]`.

```bash
curl -N http://localhost:8000/v1/events/replay/demo-1 \
  -H "Authorization: Bearer $KUBE_Q_API_KEY"
```

---

## `GET /v1/namespaces`

List the namespaces the server can see (runs `kubectl get namespaces`). Handy for
populating a namespace picker.

```bash
curl http://localhost:8000/v1/namespaces -H "Authorization: Bearer $KUBE_Q_API_KEY"
# → {"namespaces": ["default","demo","demo-rca","prod", …]}
```

---

## `GET /healthz`

Liveness/readiness probe. Available with and without the `/v1` prefix
(`GET /healthz` and `GET /v1/healthz` — the health router is registered in both
places). Returns the status and server version; it is excluded from request
logging.

```bash
curl http://localhost:8000/healthz
# → {"status":"ok","version":"2.0.0"}
```

---

## Status codes

| Code | When |
|---|---|
| `200` | Stream opened successfully (errors during streaming are reported in-band as an error event, then `[DONE]`). |
| `401` | Missing or invalid credentials (when auth is enabled). |
| `422` | No `user` message, or `stream` is not `true`. |

---

## Related

- [CLI Reference](cli-reference.md) — the `kq` client that wraps this API.
- [Agent Behaviors](agent-behaviors.md) — what happens between request and answer.
- [Security](security.md) — roles, key formats, and what each role may do.
- [Memory](memory.md) — how `user` and session state persist across turns.
