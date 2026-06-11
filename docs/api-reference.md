---
description: >-
  KubeIntellect HTTP API — the OpenAI-compatible streaming chat endpoint, the
  SSE wire format and event types, the human-in-the-loop approval flow, auth, and
  the supporting endpoints.
---

# API Reference

KubeIntellect exposes a small HTTP API served by FastAPI. The main entry point —
`POST /v1/chat/completions` — is **OpenAI chat-completions compatible**, so any
OpenAI-style client can drive it, while a side channel carries Kubernetes-specific
events (tool calls, plans, approval prompts).

- **Base URL:** the server address (e.g. `http://localhost:8000`).
- **API version prefix:** `/v1`.
- **Content type for streaming:** `text/event-stream` (SSE).

---

## Authentication

Send your API key as a Bearer token:

```http
Authorization: Bearer ki-admin-xxxxxxxxxxxxxxxx
```

The key's prefix determines the role and therefore what the agent may do
(`ki-admin-*`, `ki-op-*`, `ki-ro-*`). If no keys are configured server-side, auth
is disabled and every caller is treated as `admin` (intended for localhost only).
See [Security](security.md) for the full role model.

---

## `POST /v1/chat/completions`

Send a message, stream back the agent's investigation and answer.

### Request

| Header | Required | Purpose |
|---|---|---|
| `Authorization: Bearer <key>` | when auth is enabled | Identifies the caller's role. |
| `X-Session-ID: <id>` | recommended | Ties the request to a conversation thread. Reuse the same value across turns to keep history **and to approve/deny pending actions**. Generated if omitted. |
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
| `user` | string | `"default"` | Opaque caller id, recorded in the audit log. |
| `auto_approve` | bool | `false` | Skip approval gates for this request (trusted automation / evaluation). The [always-confirm gate](agent-behaviors.md#always-confirm-gate-overrides-auto_approve) still fires for cascading-blast actions. |

### Response — SSE stream

The response is a stream of `data:` frames. Most are standard OpenAI
`chat.completion.chunk` objects; KubeIntellect adds a handshake frame, a
`ki_event` side channel, and HITL fields.

**1. Handshake (always first):**

```text
data: {"protocol_version":"1.0","object":"stream.start","session_id":"<sid>"}
```

**2. Answer tokens** — standard OpenAI chunks; concatenate `choices[0].delta.content`:

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
| `status` | `phase`, `message` | Phase transition (e.g. fetching context, synthesizing). |
| `tool_call` | `tool`, `message` | The agent is about to run a tool/command. |
| `tool_result` | `tool`, `output` | Result of a tool call (first ~500 chars). |
| `plan` | `steps` | The visible investigation plan (a list of steps). |

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

The stream **pauses** here. To continue, send a **new request with the same
`X-Session-ID`** whose user message is an approval (`yes`, `approve`, `/approve`,
`proceed`) or denial (`no`, `deny`, `/deny`, `cancel`). Saying `approve all` /
`auto-approve` approves writes for the rest of the session. See
[Agent Behaviors → HITL](agent-behaviors.md#always-confirm-gate-overrides-auto_approve).

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

Replay the stored event history for a session as an SSE stream — useful for UIs
that reconnect or render a past investigation. Returns the same `ki_event` /
content frames the live stream produced.

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

## `POST /v1/auth/demo-keys`

Mint a short-lived, **read-only** HMAC demo key (admin-gated). Used to hand out
time-boxed access — e.g. for the public browser demo — without provisioning
static keys. Requires `AUTH_BACKEND=hmac` and `DEMO_KEY_HMAC_SECRET` to be
configured (see [Configuration](configuration.md)). Keys are validated
statelessly and expire after their TTL.

---

## `GET /healthz`

Liveness/readiness probe. Available with and without the `/v1` prefix
(`GET /healthz` and `GET /v1/healthz`). Returns the status and server version; it
is excluded from request logging.

```bash
curl http://localhost:8000/healthz
# → {"status":"ok","version":"2.0.2"}
```

---

## Status codes

| Code | When |
|---|---|
| `200` | Stream opened successfully (errors during streaming are reported in-band as an error event, then `[DONE]`). |
| `401` / `403` | Missing or insufficient credentials (when auth is enabled). |
| `422` | No `user` message, or `stream` is not `true`. |

---

## Related

- [CLI Reference](cli-reference.md) — the `kq` client that wraps this API.
- [Agent Behaviors](agent-behaviors.md) — what happens between request and answer.
- [Security](security.md) — roles, key formats, and what each role may do.
