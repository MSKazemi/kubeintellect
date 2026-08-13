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

## `GET /v1/episodes/{episode_id}/replay`

Replay a recorded episode from the **flight recorder** — the durable,
hash-chained decision log (episode IDs equal session IDs). Unlike
`GET /v1/events/replay/{session_id}` (in-memory, lost on restart), this reads
persisted records and verifies chain integrity before streaming.

The response is an SSE stream. The **first frame is a meta record**, followed
by each recorded event payload in sequence order, then `[DONE]`:

```text
data: {"type":"replay_meta","episode_id":"demo-1","records":42,"chain_valid":true}
data: {…recorded event payload…}
…
data: [DONE]
```

| `replay_meta` field | Meaning |
|---|---|
| `type` | Always `"replay_meta"`. |
| `episode_id` | The requested episode/session ID. |
| `records` | Number of recorded events that follow. |
| `chain_valid` | `true` if the hash chain verifies end-to-end; `false` means stored records may have been tampered with. |

Returns `404` when no episode with that ID has been recorded.

```bash
curl -N http://localhost:8000/v1/episodes/demo-1/replay \
  -H "Authorization: Bearer $KUBE_Q_API_KEY"
```

The [`kq replay`](cli-reference.md#kq-replay-session-id) subcommand wraps this
endpoint, rendering the events as a table with a chain-integrity verdict.

---

## `GET /v1/findings`

Recent detector firings — produced without any LLM calls by the detector
engine watching the cluster.

| Query param | Default | Meaning |
|---|---|---|
| `limit` | `100` | Maximum findings returned (1–500). |
| `since` | `0` | Only findings with `fired_at` at or after this Unix timestamp. |

```bash
curl "http://localhost:8000/v1/findings?limit=20" \
  -H "Authorization: Bearer $KUBE_Q_API_KEY"
```

Response:

```json
{
  "sensorium": "active",
  "detectors": 20,
  "findings": [
    {
      "type": "finding",
      "id": "fnd-1a2b3c4d5e6f",
      "playbook": "CrashLoopBackOff",
      "cluster_id": "default",
      "namespace": "demo-rca",
      "object": "checkout-7d4b9cf6-xk2lp",
      "evidence": "pod status=CrashLoopBackOff",
      "first_seen": 1765500000.0,
      "fired_at": 1765500030.0,
      "source": "watch"
    }
  ]
}
```

When the detector engine is disabled server-side, the response is
`{"sensorium": "disabled", "findings": []}` (the `detectors` count is omitted).

---

## `GET /v1/v5/status`

The v5 trust-plane state — which experimental slices are active and whether the
fail-closed write brakes are engaged. Read-only, no LLM calls. Surfaced by
[`kq v5-status`](cli-reference.md#kq-v5-status).

```bash
curl "http://localhost:8000/v1/v5/status" \
  -H "Authorization: Bearer $KUBE_Q_API_KEY"
```

Response:

```json
{
  "arm": "v4",
  "version": "2.1.0",
  "cortex_v5_enabled": false,
  "active_flags": [],
  "kill_switch_engaged": false,
  "change_freeze": false,
  "spend_cap_usd": 0.0
}
```

| Field | Meaning |
|---|---|
| `arm` | Architecture generation (`KI_VERSION`). |
| `version` | Package SemVer — distinguishes v4 / v4.1 / v4.2. |
| `cortex_v5_enabled` | The master v5 switch. |
| `active_flags` | The `KI_V5_*` experimental toggles currently on (empty ⇒ v4 baseline). |
| `kill_switch_engaged` | `true` ⇒ all autonomous writes are denied (fail-closed). |
| `change_freeze` | `true` ⇒ deny-by-default change window. |
| `spend_cap_usd` | Per-scope spend ceiling (`0` = unlimited). |

---

## `GET /v1/digest`

A digest of cluster activity over the last N hours, built from the flight
recorder: detector findings, autonomous investigations, user sessions, and
rollback points.

| Query param | Default | Meaning |
|---|---|---|
| `hours` | `24` | Look-back window in hours (float, greater than 0, max `168`). |
| `format` | `json` | `json` (structured digest) or `markdown` (rendered text). |

With `format=json`, the structured digest:

```json
{
  "window_hours": 24.0,
  "generated_at": 1765500000.0,
  "findings": [],
  "auto_investigations": [],
  "user_sessions": 3,
  "rollback_points": [],
  "summary": "…"
}
```

With `format=markdown`, the rendered text wrapped in one field:

```json
{"markdown": "# KubeIntellect digest — last 24h\n…"}
```

```bash
curl "http://localhost:8000/v1/digest?hours=8&format=markdown" \
  -H "Authorization: Bearer $KUBE_Q_API_KEY"
```

The [`kq digest`](cli-reference.md#kq-digest-hours-n) subcommand wraps this
endpoint with `format=markdown`.

---

## `GET /v1/episodes/{episode_id}/postmortem`

A grounded incident postmortem for one episode, reconstructed from the
hash-chained flight recorder (ADR-011). The deterministic timeline cites every
event's sequence number (`[#seq]`); an `chain_valid` field reports whether the
audit chain verified intact. Requires `POSTMORTEM_ENABLED` (default on).

| Query param | Default | Meaning |
|---|---|---|
| `format` | `json` | `json` (structured) or `markdown` (rendered, seq-cited). |

```json
{
  "episode_id": "auto-fnd-abc",
  "chain_valid": true,
  "timeline": [{"seq": 0, "at": 1765500000.0, "kind": "finding", "summary": "…"}],
  "what_fired": [], "investigated": [], "tried": [], "worked": [],
  "root_cause": "…", "follow_ups": [], "narrative": null,
  "summary": "… audit chain intact."
}
```

The [`kq postmortem`](cli-reference.md#kq-postmortem-session-id) subcommand wraps
this with `format=markdown`. An optional LLM narrative (`POSTMORTEM_LLM_NARRATIVE`)
is constrained to the recorded events.

---

## `POST /v1/detectors` (and review endpoints)

Natural-language detector authoring (ADR-012). Compile a plain-English failure
description into a detect block, validate it, and stage it as a **shadow**
candidate that observes but never reaches the watchtower until promoted. Gated by
`NL_DETECTOR_AUTHORING_ENABLED` (default off); writes require operator/admin.

| Method & path | Purpose |
|---|---|
| `POST /v1/detectors` | Body `{"description": "...", "name"?: "..."}` → compile + validate + stage as shadow. Returns the compiled block + any validation errors. |
| `GET /v1/detectors?status=` | List detectors (`candidate`/`shadow`/`active`/`demoted`). |
| `POST /v1/detectors/{name}/promote` | Promote shadow → active (it now reaches the watchtower). |
| `POST /v1/detectors/{name}/demote` | Demote/reject — stop it firing. |
| `GET /v1/detectors/{name}/shadow-findings` | A shadow detector's firings (review before promoting). |

```json
{"staged": true, "status": "shadow", "name": "nl:OOMKilled",
 "compiled": {"watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$"}]},
 "errors": []}
```

The [`kq detector`](cli-reference.md#kq-detector-teach-a-new-failure-in-plain-english)
subcommands wrap these.

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

## `GET|PUT|DELETE /v1/preferences`

Operator-preference memory (the MemoryAgent surface). Preferences are per-user —
keyed by the OpenAI-compatible `user` field (default `"default"`), the same
identity chat uses. Explicitly set preferences have confidence `1.0` and are
never overwritten by behaviour-inferred ones.

Requires `PREFERENCE_MEMORY_ENABLED` **and** an active memory hierarchy
(PostgreSQL + `MEMORY_HIERARCHY_ENABLED`); otherwise returns `404` (disabled) or
`503` (memory inactive). Reads are open to any role; **writes require the
`operator` role or higher**.

| Method | Path | Role | Purpose |
|---|---|---|---|
| `GET` | `/v1/preferences?user=<u>` | any | List active preferences for a user (top 50). |
| `PUT` | `/v1/preferences` | operator+ | Set an explicit preference. Body: `{"key","value","user"}`. |
| `DELETE` | `/v1/preferences/{key}?user=<u>` | operator+ | Forget a single preference. |

```bash
# set a preference
curl -X PUT http://localhost:8000/v1/preferences \
  -H "Authorization: Bearer $KUBE_Q_API_KEY" -H "Content-Type: application/json" \
  -d '{"key":"default_namespace","value":"payments","user":"alice"}'
# → {"user":"alice","key":"default_namespace","value":"payments","source":"explicit"}

# list them
curl "http://localhost:8000/v1/preferences?user=alice" -H "Authorization: Bearer $KUBE_Q_API_KEY"
# → {"user":"alice","preferences":[ … ]}
```

The `kq preference {set,list,forget}` subcommands wrap these endpoints — see
[CLI Reference](cli-reference.md).

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
