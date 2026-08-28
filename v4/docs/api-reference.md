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

The role — and therefore what the agent may do — comes from **which configured list the
key appears in**, not from its prefix. `ki-admin-*`, `ki-op-*` and `ki-ro-*` are a naming
convention we recommend, and nothing enforces them: a key named `ki-op-…` that sits in
`KUBEINTELLECT_READONLY_KEYS` is `readonly`, and an unrecognised key is rejected. If no keys
are configured server-side, auth is disabled and every caller is treated as `admin` (intended
for localhost only). A client that needs to *state* what it may do must ask
[`GET /v1/auth/whoami`](#get-v1authwhoami) rather than read the prefix. See
[Security](security.md) for the full role model.

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
| `error` | `message`, `fatal` | Something failed. `fatal: true` means **the turn ended here**; without it the agent recovered and the answer that follows is still valid. |
| `usage` | `prompt_tokens`, `completion_tokens`, `total_tokens`, `llm_calls` | What the turn cost. Emitted **once**, immediately before the `finish_reason: "stop"` chunk, summed over every model call the turn made — triage, the coordinator loop, the subagent fan-out, verification. |

> **Reading `usage`.** `llm_calls` is the field that makes a zero interpretable. `llm_calls: 0`
> means no model was called; `llm_calls: 40, total_tokens: 0` means the provider reported no
> counts, which is an instrumentation gap and **not** a free turn. A client that records cost
> should also distinguish "the frame said zero" from "no frame arrived" — an older server emits
> none at all, and treating that as `0` is how a whole evaluation campaign came to report
> `tokens == 0` for every one of its 72 predictions and write off its efficiency axis. Usage is
> reported whether or not Langfuse tracing is enabled.


> **Detecting a failed turn.** A crashed stream terminates with the *same* frames a successful
> one does — a `finish_reason: "stop"` chunk, then `[DONE]` — and the reason arrives as ordinary
> `content` (`[Error: …]`) so that clients ignoring this side channel still show something. That
> text is not a signal: it is indistinguishable from an answer to anything that is not reading
> English. **Check for a `ki_event` of type `error` with `fatal: true`, and discard any partial
> answer when you see one** — the agent may have streamed half a diagnosis before it died, and a
> truncated diagnosis presented as a complete one is worse than no answer. `kq` does this and
> exits non-zero; it previously exited `0`.

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
`auto-approve` approves writes for the rest of **that turn**; it is not persisted, so the next
request is gated again unless it sets `"auto_approve": true`. See
[Agent Behaviors → HITL](agent-behaviors.md#always-confirm-gate-overrides-auto_approve).

**5. Completion (always last):**

```text
data: {"…","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

**Keepalive:** if the stream is silent for 15 seconds, the server sends an SSE
comment line `: heartbeat` to keep the connection open. Ignore these.

**Frames you cannot parse:** a client must decide what an undecodable frame means, and the
answer is not always *"skip it"*. Frames carrying prose (`delta.content`) can be dropped and the
answer stays readable; frames carrying a **verdict** cannot — the first frame of
`GET /v1/episodes/{episode_id}/replay` is
a meta record holding `chain_valid`, so treating it as absent silently converts *"the audit chain
is broken"* into *"the audit chain was never mentioned"*. A stream that ends without a `[DONE]`
sentinel is likewise truncated, not complete.

`kube-q` handles this by counting rather than raising. `kube_q.core.transport.iter_sse` and its
async twin `kube_q.core.client._aiter_sse` accept an optional `SseStats`:

```python
from kube_q.core.transport import SseStats, iter_sse

stats = SseStats()
for frame in iter_sse(response, stats):
    ...
if not stats.lossless:                     # dropped_frames, first_error, truncated_tail
    raise RuntimeError(f"stream lost {stats.dropped_frames} frame(s)")
```

Passing no `SseStats` yields exactly the same frames as before. Interactive `kq` chat passes one
and prints a warning that the answer may be incomplete; commands whose output is a verdict should
fail closed on `stats.lossless` being false.

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
content frames the live stream produced, behind a meta frame:

```text
data: {"type":"replay_meta","session_id":"demo-1","records":42,"durable":false}
```

```bash
curl -N http://localhost:8000/v1/events/replay/demo-1 \
  -H "Authorization: Bearer $KUBE_Q_API_KEY"
```

`durable: false` is the important field. This history lives in **this process's memory**: it
does not survive a restart and is not shared between replicas. So a `404` here means only that
*this pod* has nothing for that session — never that the session did not run or produced
nothing — and the response body says so. A session that genuinely emitted nothing answers `200`
with `records: 0`, which is a different answer on purpose.

| Status | Meaning |
|---|---|
| `200` | This process has a history for the session. `records` may be `0`. |
| `404` | This process has no history for it. Not a statement about the session. |

For an answer that survives restarts and replicas, read
[`GET /v1/episodes/{episode_id}/replay`](#get-v1episodesepisode_idreplay) instead — episode IDs
equal session IDs.

---

## `GET /v1/episodes/{episode_id}/replay`

Replay a recorded episode from the **flight recorder** — the durable,
hash-chained decision log (episode IDs equal session IDs). Unlike
`GET /v1/events/replay/{session_id}` (in-memory, lost on restart), this reads
persisted records and verifies chain integrity before streaming.

The response is an SSE stream. The **first frame is a meta record**, followed
by each recorded event payload in sequence order, then `[DONE]`:

```text
data: {"type":"replay_meta","episode_id":"demo-1","records":42,"chain_valid":true,"chain_verified":true}
data: {…recorded event payload…}
…
data: [DONE]
```

| `replay_meta` field | Meaning |
|---|---|
| `type` | Always `"replay_meta"`. |
| `episode_id` | The requested episode/session ID. |
| `records` | Number of recorded events that follow. |
| `chain_valid` | `true` if nothing contradicted the stored records; `false` means they may have been tampered with. |
| `chain_verified` | `false` when the check could not be performed — the `decision_log_head` anchor could not be read or parsed. `chain_valid` is then *"nothing contradicted these records"*, which is not evidence, because nothing could have. Absent on servers older than 2026-08-24; read it as `true`. |

**Read both.** Until 2026-08-24 a failed anchor read returned the same `chain_valid: true` an
agreeing anchor returns, so `kq replay` printed `✓ chain intact` and exited `0` for an episode
the server had just logged as *"truncation … NOT currently detectable"*. `kq replay` now exits
`4` on `chain_verified: false`, the code it already owned for an unverified chain. A **missing**
anchor is not that state: the read succeeded and there was nothing to contradict the records
(an episode written before the anchor existed), so it stays `chain_verified: true`.

`chain_valid` covers both an alteration and a **truncation**: the hash links are recomputed
and the surviving chain is compared with the episode's `decision_log_head` anchor, so deleting
the newest records is caught rather than leaving a shorter valid chain. An episode whose
records are all gone but whose anchor survives returns **`409`** — `404` would report a total
truncation as an episode that never existed. An episode with no records whose anchor could not
be read returns **`503`**: absence and total truncation are indistinguishable from there, and
both `404` and `409` would state one of them as fact.

`chain_valid: true` means **nothing stored was altered**. It does not mean the
episode is complete: the recorder is fire-and-forget, so an outage loses events
that were never stored. Losses appear in the stream as `recorder_gap` payloads
carrying `dropped` and `reason` — see
[flight recorder](flight-recorder.md#tamper-evidence).

Returns `404` when no episode with that ID has been recorded, and `503` when the
decision log could not be read at all — the recorder is not running, or its
database is unreachable. The two are kept apart deliberately: `404` is a positive
claim that the episode does not exist, and answering it for an outage tells an
operator mid-incident that the trail they are looking for was never recorded.
The `503` body says so explicitly:

```json
{"detail": "the flight recorder is not running — no decision log to read — this is not the same as episode 'demo-1' having no records"}
```

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
  "sensorium_reason": "",
  "detectors": 20,
  "predictive": "active",
  "predictive_detectors": 3,
  "predictive_error": null,
  "streams": [
    {"name": "get pods -A", "connected": true, "stopped": false,
     "consecutive_failures": 0, "last_error": null}
  ],
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

`sensorium` reports **perception, not object lifetime** — it is `active` only
while at least one `kubectl --watch` stream is connected:

| Value | Meaning |
|---|---|
| `active` | at least one watch stream is connected — an empty `findings` list means the cluster is quiet |
| `disabled` | there is no detector engine on this replica; **read `sensorium_reason`** — four unrelated situations land here and only one of them is a setting |
| `starting` | no watch stream has started yet |
| `reconnecting` | every stream is down and retrying (backoff caps at 60s) |
| `stopped` | every stream gave up permanently and will not reconnect — e.g. kubectl is missing on the server |

`sensorium_reason` is empty while perceiving and, when `sensorium` is `disabled`, says which
of four things happened. Until 2026-08-24 all four produced the single sentence *"the sensorium
is not running (SENSORIUM_ENABLED=false, or no compiled detectors loaded)"*, which for the last
two below was simply untrue:

| Situation | What it means |
|---|---|
| switched off | `SENSORIUM_ENABLED=false`. A configuration choice. |
| no compiled detectors | nothing was loaded to watch with, so the engine never started. |
| **failed to start** | it tried and raised (the message is included). This is an **outage** — the server logs one `WARNING` at startup and continues serving, so this field is how you find it afterwards. |
| **leader-election standby** | this replica lost the singleton lock and watches nothing **by design**; another replica is perceiving. Read its findings, not this replica's silence. See `leader` on [`GET /healthz`](#get-healthz). |

The same answer is on [`GET /healthz`](#get-healthz) as the `sensorium` block, which is where
to look first — this endpoint tells you about perception once you are already asking about
findings, and the health probe tells you before you think to.

`streams` carries one entry per watch stream with `connected`, `stopped`,
`consecutive_failures` and `last_error` (kubectl's own stderr, e.g. an RBAC
denial).

`predictive` is the same claim for the **anticipatory** detectors
([ADR-010 trend predicates](autonomy.md)), which see through Prometheus rather than through a
watch stream. The two are independent: a connected watch stream says nothing about whether
Prometheus is answering.

| Value | Meaning |
|---|---|
| `active` | the last trend sweep reached Prometheus — nothing is projected to fail |
| `blind` | Prometheus could not be queried; `predictive_error` carries the reason, and no prediction *could* have fired |
| `off` | `PREDICTIVE_DETECTION_ENABLED=false`, or no loaded detector has a `trend_predicates` block |

`predictive_detectors` counts the detectors that carry trend predicates.

!!! danger "An empty `findings` list is only an all-clear when `sensorium` is `active`"
    In every other state nothing is being watched, so no finding *could* have
    fired. Before 2026-08-20 the field reported `active` whenever a detector
    engine object existed — measured with kubectl absent, both watch tasks had
    permanently exited and the endpoint still answered
    `{"sensorium": "active", "detectors": 20, "findings": []}`, which `kq findings`
    renders as *"No findings · 20 detectors watching"*. Nothing was watching.

!!! danger "…and only when `predictive` is not `blind`"
    The same trap one layer over. A Prometheus outage used to produce no predicted findings **and
    no signal at all**: the query error came back as the discarded half of a tuple, so the engine
    saw an empty series, and the documented `trend_query_error` log line was in an `except` block
    that a Prometheus outage can never reach — `_query_raw` returns its errors, it does not raise.
    A layer whose entire job is to warn *before* a failure had stopped warning, silently.

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
  "set_but_unwired_flags": [],
  "degraded_experimental_flags": [],
  "memory": {"enabled": true, "state": "ready", "reason": "", "observations_dropped": 0},
  "unenforceable_guard_config": [],
  "kill_switch_engaged": false,
  "change_freeze": false,
  "spend_cap_usd": 0.0,
  "autonomy_promotion": {
    "enabled": false, "operating": false, "direction": "revoke-only",
    "action_class": "watchtower-autofix", "samples": 0,
    "authority_revoked": false, "reason": "flag off"
  }
}
```

| Field | Meaning |
|---|---|
| `arm` | Architecture generation (`KI_VERSION`). |
| `version` | Package SemVer — distinguishes v4 / v4.1 / v4.2. |
| `set_but_unwired_flags` | Flags you turned **on** that no code reads — they change nothing. Normally `[]`. A name here means the setting had no effect, so do not treat it as confirmation that a slice is live; see [v5 experimental flags](v5-experimental-flags.md). |
| `degraded_experimental_flags` | Flags you turned **on** that code *does* read, but whose subsystem is **not running** — so they change nothing anyway. Every `MEMORY_*` slice runs inside the memory hierarchy, so all of them appear here while it is down, including when you turned a slice on and left `MEMORY_HIERARCHY_ENABLED` off. They stay listed in `active_flags`: that list is rollout *identity* — how this pod was configured — and must not flap when Postgres blips. Normally `[]`. |
| `memory` | The memory hierarchy's own state, observed recall/write counters, `symptoms` and `healthy`, identical to the field on [`/healthz`](#get-healthz). Read it for **why** the flags above are degraded — and read `healthy`, not just `enabled`, since `enabled` only means the pool is up. |
| `unenforceable_guard_config` | Guard settings you configured that **cannot match anything** — a `KUBECTL_BLOCKED_NAMESPACES` entry that is not a legal namespace name (a glob, a slash), or an `AUTONOMY_NAMESPACE_LEVELS` / `AUTONOMY_A3_ALLOWLIST` entry the parser drops. Also covers `ALLOWED_ORIGINS`, where an unmatchable entry (trailing slash, missing scheme) is silently not allowed and **`*` is reported as a security problem** — CORS runs with credentials enabled, so a wildcard lets any site call this API with an operator's session. Normally `[]`. A name here means the protection you configured is **not in force**; see [security](security.md#how-the-kubectl-blocklist-works). |
| `cortex_v5_enabled` | The master v5 switch. |
| `active_flags` | The `KI_V5_*` experimental toggles currently on (empty ⇒ v4 baseline). |
| `kill_switch_engaged` | `true` ⇒ all autonomous writes are denied (fail-closed). Binds regardless of `KI_V5_BLAST_RADIUS_BUDGET` — see [stopping the agent](autonomy.md#stopping-the-agent-break-glass). |
| `change_freeze` | `true` ⇒ deny-by-default change window. Also independent of `KI_V5_BLAST_RADIUS_BUDGET`. |
| `spend_cap_usd` | Per-scope spend ceiling (`0` = unlimited). |
| `autonomy_promotion` | The fourth brake on the autonomous-write path (ADR-102), behind `KI_V5_STATISTICAL_PROMOTION`. **`direction` is always `revoke-only`**: the recorded record can close the A3 gate, never open it — the samples come from writes the allowlist already permitted, so promoting on them would be circular. Read **`operating`, not `enabled`**: they differ exactly when the flag is on and there is no outcome store to read, which is a brake reported as on that is not in your write path (the flag is then also listed in `degraded_experimental_flags`). `authority_revoked: true` means A3 auto-fix is currently shut, and `reason` says which trigger fired; `samples` is the count inside the ADR-102 rolling window, not the table size. A store that exists and cannot be read reports `operating: false` with the error in `reason` — never a clean record. |

---

## `GET /v1/digest`

A digest of cluster activity over the last N hours, built from the flight
recorder: detector findings, autonomous investigations, user sessions, and
rollback points. Each entry in `rollback_points` carries `restorable` — `false` means the
captured YAML was redacted or truncated and must not be applied, and a missing field means the
capture predates the check. See
[Flight Recorder → Restorable vs recorded](flight-recorder.md#restorable-vs-recorded).

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
  "rollback_points": [{"at": 1765499000.0, "rollback_id": "rb-1a2b3c4d5e6f",
                       "command": "kubectl scale deployment/web --replicas=0 -n prod",
                       "restorable": true, "capture_notes": []}],
  "degraded": false,
  "degraded_reasons": [],
  "summary": "…"
}
```

`degraded` is `true` when a source could not answer — the flight recorder is
disabled, the server is in SQLite mode (no `decision_log` table), the watchtower
is off, the pool is unavailable, or a query failed. `degraded_reasons` names each
one. **An empty digest with `degraded: true` means "nothing was recorded", not
"nothing happened"** — do not treat it as an all-clear. The sections that were
readable are still populated.

Those are all statements about the **record**. A flawless, empty record is also what a
window with nothing *watching* produces, so `degraded_reasons` covers the **perception**
sources too — no `kubectl` watch stream connected, or `predictive: blind` — read from the
same classifier [`GET /v1/findings`](#get-v1findings) uses, so the two endpoints cannot
disagree about whether the same window was covered.

!!! danger "A `Quiet watch` summary is a claim about perception, not just about the log"
    Before 2026-08-20 the digest validated only its recording sources. Measured with a
    healthy recorder, a readable `decision_log` and **nothing watching**, `/v1/findings`
    answered `{"sensorium": "starting", "findings": []}` and `kq findings` refused to call
    it clear — while `kq digest` over the same window said *"Quiet watch: no findings in
    the last 24h."* One absence, two surfaces, opposite answers.

    Note what `degraded_reasons` still cannot say: stream health is the state **now**, not
    a history of the window, and none is kept. A stream that died an hour ago and has since
    reconnected reads as `active`, so the listed gaps are a lower bound on the blindness in
    the window.

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
event's sequence number (`[#seq]`); `chain_valid` and `chain_verified` report the
audit-chain verdict, and `events_lost` / `gaps` report whether the recorder lost
any events for this episode — intact and complete are different claims. Requires
`POSTMORTEM_ENABLED` (default on).

| Query param | Default | Meaning |
|---|---|---|
| `format` | `json` | `json` (structured) or `markdown` (rendered, seq-cited). |

```json
{
  "episode_id": "auto-fnd-abc",
  "chain_valid": true,
  "chain_verified": true,
  "timeline": [{"seq": 0, "at": 1765500000.0, "kind": "finding", "summary": "…"}],
  "what_fired": [], "investigated": [], "tried": [], "worked": [],
  "root_cause": "…", "follow_ups": [], "narrative": null,
  "events_lost": 0,
  "gaps": [],
  "summary": "… audit chain intact."
}
```

**`chain_valid` alone cannot tell "the hashes disagree" from "there was nothing to
hash".** `chain_verified` says whether the chain was checked at all, and it is `false`
in exactly two cases: the recorder could not be read (`recorder_available: false`), and
the episode has no recorded events. In both the markdown render carries an **AUDIT CHAIN
NOT VERIFIED** banner instead of the tamper warning — a postmortem over records nobody
read is neither a statement that they are intact nor that they were altered. This is the
same distinction [`kq replay`](cli-reference.md#kq-replay-session-id) draws with its exit
code `4`. `chain_valid` keeps its existing meaning and type: `true` only when the chain
was checked and verified.

`events_lost` is the total number of events the recorder could not write for
this episode, and `gaps` lists each one as `{"seq", "dropped", "reason"}`. When
`events_lost` is non-zero the markdown render carries a **RECORD INCOMPLETE**
banner beside the chain verdict, and `kq export` exits `5`.

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
| `POST /v1/detectors/{name}/promote` | Promote shadow → active (it now reaches the watchtower). **409** if its predicates can never match — see below. |
| `POST /v1/detectors/{name}/demote` | Demote/reject — stop it firing. |
| `GET /v1/detectors/{name}/shadow-findings` | A shadow detector's firings (review before promoting). |

`GET /v1/preferences` and `GET /v1/detectors` answer **503** when their store cannot be read (not configured, or the query failed) rather than an empty `200`. An empty `detectors` list therefore means exactly one thing: the store was read and holds nothing — never that the question could not be answered.

Promotion is refused with **409** for a detector whose predicates provably cannot match — a
`kind` outside `Pod`/`Event`/`Node`, a `Pod`/`Node` predicate with no `status_regex`, a
`reason_regex`/`status_regex` satisfiable only by something no cluster emits, or a
`trend_predicates` entry pinned to an unfilled template
(`{deployment="your-deployment-name"}`), asking for an `min_r2` above 1.0, or carrying a
non-positive ETA bound, lookback window, or an unrecognised `direction`. Shadow detectors
are promoted on their precision record, and one that cannot fire records zero firings, which
looks identical to a condition that never occurred. The same check runs when detectors are
loaded from the store, so such a row is skipped with a warning rather than counted as coverage.
A store read failure is not treated as evidence that a detector is dead, a missing detector is
still a **404**, and demotion is never blocked.

The store lookup behind that check reads `cluster_id IN (<this cluster>, 'global')`. Authoring
writes under `global` — the write path's word for *everywhere* — so an equality lookup found no
row on any deployment that sets `CLUSTER_ID`, returned "no reason to refuse", and promoted
without checking. The load path reads the same way, for the same reason: a detector authored
through this API is evaluated by the cluster running it, not only by a deployment that happens
to leave `CLUSTER_ID` unset.

```json
{"staged": true, "status": "shadow", "name": "nl:OOMKilled",
 "compiled": {"watch_predicates": [{"kind": "Pod", "status_regex": "^OOMKilled$"}]},
 "errors": []}
```

`GET /v1/detectors/{name}/shadow-findings` answers with the firings **and** two fields about
whether the number means anything:

```json
{"name": "nl:soak-cpu-saturated", "findings": [], "watching": false,
 "watching_reason": "nl:soak-cpu-saturated is loaded but has only trend predicates, and PREDICTIVE_DETECTION_ENABLED is false — nothing evaluates them. Its zero firings are not evidence about the predicate."}
```

`watching` means the engine **evaluates** this detector, not merely that it loaded it. The two
are different for a detector whose only predicates are `trend_predicates`: those are evaluated on
the predictive interval, which does not run when `PREDICTIVE_DETECTION_ENABLED` is false. Such a
detector is in the shadow set, lists as `shadow`, and is evaluated by nothing — so `"findings":
[]` is a fact about the flag, not about the cluster. `watching_reason` names the case in words,
including which flag to change. **A precision or recall figure computed over shadow detectors
must take its denominator from `watching`, never from the row count in the store.**

`watching_reason` also carries the opposite problem, because `watching: true` on its own can be
true and misleading in the same breath. A detector whose predicate matches a **healthy** object
fires on every object of its kind on the cluster, and a reviewer reads a firing count as a fault
count:

```json
{"name": "nl:soak-cpu-saturated", "watching": true,
 "watching_reason": "loaded, with watch predicates evaluated on every observation. WARNING: this detector fires on HEALTHY objects, so its findings are not evidence of a fault — status_regex '^Running$' matches Running, which is what the observer emits for a healthy Pod; this predicate fires on every pod on the cluster, not on a fault. A Pod predicate has no namespace or label scope, so there is no way to narrow it."}
```

`POST /v1/detectors` refuses such a predicate outright, and so does promotion out of `shadow`.
The **engine loads it anyway** — deleting a false-positive source at load would improve a
measured false-positive rate by removing the evidence against it — so a detector authored before
the gate existed keeps firing, keeps counting, and now explains itself.

The [`kq detector`](cli-reference.md#kq-detector-teach-a-new-failure-in-plain-english)
subcommands wrap these.

---

## `GET /v1/namespaces`

List the namespaces the server can see (runs `kubectl get namespaces`). Handy for
populating a namespace picker. Protected namespaces
([`KUBECTL_BLOCKED_NAMESPACES`](security.md)) are removed from the list.

```bash
curl http://localhost:8000/v1/namespaces -H "Authorization: Bearer $KUBE_Q_API_KEY"
# → {"namespaces": ["default","demo","demo-rca","prod", …], "withheldByPolicy": ""}
```

**`withheldByPolicy` says whether the list is the whole list.** It is empty when nothing was
removed, and otherwise carries the same sentence a filtered `kubectl get ns -o json` puts in its
own `withheldByPolicy` field — a count, never the withheld names:

```json
{"namespaces": ["default", "shop"],
 "withheldByPolicy": "[Protected] 2 namespace(s) withheld — they belong to a namespace in KUBECTL_BLOCKED_NAMESPACES. This listing is NOT the complete set."}
```

Until 2026-08-24 the filtering happened in silence, so this endpoint and `run_kubectl` gave
different answers to the same question — `kubectl get ns monitoring` was refused out loud, and
`GET /v1/namespaces` simply omitted it. `kq` read the omission as proof and told operators
*"Namespace 'monitoring' not found in the cluster"*. **A namespace missing from this list is not
necessarily absent: check `withheldByPolicy` first.** When it is non-empty, absence from
`namespaces` is not evidence of anything.

**`503` when the cluster cannot be listed** — an unreachable API server, expired credentials, an
RBAC denial, a missing `kubectl`, or a timeout. The `detail` carries the first line of kubectl's
own error:

```json
{"detail": "Cannot list namespaces: The connection to the server localhost:8080 was refused"}
```

This matters for anything consuming the endpoint: a `200` with an empty list means the cluster
genuinely has no namespaces you may see, and nothing else. Until 2026-08-20 the return code was
not checked, so every one of those failures was reported as `200 {"namespaces": []}` — and `kq`
believed it, answering *"Namespace 'prod' not found in the cluster"* to an operator whose
credentials had just expired. **Treat an empty list as data and a `503` as unknown; do not
collapse them.**

---

## `POST /v1/auth/demo-keys`

Mint a short-lived, **read-only** HMAC demo key (admin-gated). Used to hand out
time-boxed access — e.g. for the public browser demo — without provisioning
static keys. Requires `AUTH_BACKEND=hmac` and `DEMO_KEY_HMAC_SECRET` to be
configured (see [Configuration](configuration.md)). Keys are validated
statelessly and expire after their TTL.

---

## `GET /v1/auth/whoami`

Return the role the **caller's own** key holds — never another caller's. Authenticated like
every other `/v1` route, so a request with no key, or an unrecognised one, is rejected before
the handler runs.

```bash
curl http://localhost:8000/v1/auth/whoami -H "Authorization: Bearer $KUBE_Q_API_KEY"
# → {"role": "readonly"}
```

`role` is one of `superadmin`, `admin`, `operator`, `readonly` — see
[Security](security.md) for what each may do.

This exists because a client cannot infer its own privileges, and one that guesses will
eventually say something untrue. The Hugging Face Space used to print *"this demo key holds
the `readonly` role"* as fixed text next to an agent that could act; its backend URL and key
are both environment variables, so the sentence was true only for the deployment it was
written for. The footer now renders what this endpoint returns, and says it could not confirm
when the probe fails rather than assuming the safe-sounding answer.

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

**Liveness** probe. Available with and without the `/v1` prefix (`GET /healthz` and
`GET /v1/healthz`). Returns the status and server version; it is excluded from request logging.

It checks **nothing** — that is deliberate. Liveness answers "is this process wedged, restart
it?", so a liveness probe that touches Postgres would turn one database blip into a
cluster-wide restart loop. Use `/readyz` for routing decisions.

```bash
curl http://localhost:8000/healthz
# → {"status":"ok","arm":"v4","version":"2.0.2","experimental_flags":[],
#    "set_but_unwired_flags":[],"degraded_experimental_flags":[],
#    "leader":{...},"sensorium":{"enabled":true,"state":"running","watching":true,
#    "reason":""},"audit":{"enabled":true,"state":"ready",
#    "reason":"","dropped":0},"memory":{"enabled":true,"state":"ready","reason":"",
#    "observations_dropped":0},"recorder":{"enabled":true,"state":"ready","reason":"",
#    "lost_while_down":0},"db_schema":{"state":"current","expected_version":1,
#    "applied_version":1,"matches":true,"reason":""}}
```

`db_schema` answers whether the **database** is the shape this build writes to — `current`,
`stale` (run `kubeintellect db-init`), `ahead` (the deployment was rolled back and the database
was not), `unrecorded`, or `unknown` (the check could not run — not a verdict). It is read once
when the memory pool opens and cached; this endpoint never queries Postgres. A non-`current`
schema deliberately does **not** move the top-level `status`: every memory, recorder and audit
write is fire-and-forget, so the failure is silent rather than fatal, and failing liveness would
turn a fixable database into a restart loop. See
[CLI reference → `db-init`](cli-reference.md#kubeintellect-db-init).

`experimental_flags` lists the default-off toggles actually in force. `set_but_unwired_flags`
lists toggles you set that **no code reads** — it is normally empty, and a non-empty value means
that setting did nothing (see [v5 experimental flags](v5-experimental-flags.md)).

`degraded_experimental_flags` answers the case one step out: the flag *is* read by code, but the
subsystem it lives in is not running, so it changes nothing anyway. Every `MEMORY_*` slice runs
inside the memory hierarchy, so all of them appear here whenever `memory.state` is not `ready` —
including the case where you turned a slice on and left `MEMORY_HIERARCHY_ENABLED` off, where
there is no hierarchy for it to run inside. **These flags stay listed in `experimental_flags`.**
That list is rollout identity — which arm this pod was configured as — and a Postgres blip must
not make it flap; `degraded_experimental_flags` is the liveness answer, and `memory.reason` says
why. `state: "starting"` counts as degraded and clears itself once startup finishes.

`audit` reports whether API requests are reaching `request_log`, and `dropped` counts the
requests that went unrecorded since this process started. `enabled: false` with
`state: "unavailable"` means this replica is auditing **nothing** — see
[Operations → Audit log](operations.md#audit-log). `state: "sqlite"` is configuration, not a
fault. Note this is reported state, not a probe: `/healthz` still checks nothing on the request
path, and stays `200` while the audit log is down — an unaudited pod is degraded, not wedged.

`memory` reports whether the memory hierarchy (L1 episodes, L2 knowledge graph, consolidation)
is running, and `observations_dropped` counts the sensorium observations discarded since start.
`enabled: false` with `state: "unavailable"` means this replica is recording **nothing** — see
[Memory → When the hierarchy is not running](memory.md#when-the-hierarchy-is-not-running).
`state: "flag"` and `state: "sqlite"` are configuration, not faults. Like `audit`, this is
reported state rather than a probe, and `/healthz` stays `200` while it is down.

Read `memory.healthy`, not only `memory.enabled`. `enabled` says the **pool is up**, which is a
narrower claim than "memory works": an evaluation lane once ran nine hours with `state: "ready"`
while nothing was ever written or recalled, and nothing in the response was false. Alongside it the
block reports what memory actually did — `recall_attempts`, `recall_hits`, `recall_failures`,
`episodes_written` — with `symptoms` naming anything observably wrong and `healthy` folding it into
one boolean. `enabled: true` with `healthy: false` is the connected-but-doing-nothing case. A cold
store is not a fault: symptoms stay silent for the first 10 recall attempts, and memory switched off
by flag reports `healthy: true`. `healthy` does not move the top-level `status`, because restarting
a pod whose memory is merely cold turns a degraded subsystem into a crash loop — alert on
`.memory.healthy`, restart on `.status`. Benchmarks and evaluation harnesses should gate on
`.memory.healthy` before grading.

`sensorium` reports whether anything is actually **watching** the cluster, and it is the field
to alert on — the three blocks around it describe what gets *written down*, while this one
describes whether there is anything to write down at all. Until 2026-08-24 it was absent: a
sensorium that raised on the way up produced a `/healthz` of `status: "ok"` with no field
mentioning perception, and the reason was reachable only from `/v1/findings` and the digest,
which you consult once you already suspect a problem.

Read `watching`, not only `enabled`. They are separate on purpose: the detector engine exists
whether or not any `kubectl --watch` stream is connected, and the watch loop returns permanently
when `kubectl` is missing — so `enabled: true, watching: false` is a real and durable state, and
its `reason` says so. `state` carries the precise cause when nothing is watching:
`disabled_by_flag` and `standby` are configuration (on a leader-election standby a **peer** holds
the singleton lock and is watching, so the cluster is covered); `no_detectors`, `start_failed`
and `stopped` are not. Like the blocks above, this is reported state rather than a probe, and
`/healthz` stays `200` while perception is down — a blind pod is degraded, not wedged.

`recorder` reports whether the tamper-evident decision log is being written, and
`lost_while_down` counts events that were never persisted because it was not running — those
same events are written into the chain as `recorder_gap` records once recording resumes. See
[Flight recorder → When the recorder is not running](flight-recorder.md#when-the-recorder-is-not-running).

```bash
```

---

## `GET /readyz`

**Readiness** probe — a different question from liveness: *"should this replica receive traffic
right now?"* Returns `200 {"status":"ready"}` while serving, and `503 {"status":"draining"}`
once shutdown has begun.

> **Do not build a drain on that 503 — you cannot observe it.** This page used to claim the
> 503 was what stopped traffic during a rolling update. Probing a real server through a real
> `SIGTERM` disproved it: uvicorn closes its listening socket first and runs the application's
> shutdown hook last, so from outside the transition is `200` → connection refused. What
> drains a rolling update is the chart's **`preStop` sleep** (`drainSeconds`, default 5),
> which Kubernetes runs *before* `SIGTERM` and which keeps the socket open while the
> Endpoints removal propagates. Readiness governs a *running* pod; a terminating one leaves
> the EndpointSlice by deletion, whatever its probe says.

If you raise `drainSeconds`, raise `terminationGracePeriodSeconds` (default 45) with it — the
sleep is spent inside that budget, and a `SIGKILL` at the deadline lands mid-drain.

`/readyz` reports **local state only and never probes the database.** A readiness probe that
pings a shared dependency looks more thorough and is more dangerous: when that dependency blips,
every replica goes unready simultaneously and the Service is left with no endpoints at all —
turning a degraded system into a total outage. Dependency health belongs in alerting.

```bash
curl -i http://localhost:8000/readyz
# → HTTP/1.1 200 OK   {"status":"ready"}
# during shutdown:
# → HTTP/1.1 503 Service Unavailable   {"status":"draining"}
```

---

## Status codes

| Code | When |
|---|---|
| `200` | Stream opened successfully (errors during streaming are reported in-band as an error event, then `[DONE]`). |
| `401` / `403` | Missing or insufficient credentials (when auth is enabled). |
| `422` | No `user` message, or `stream` is not `true`. |
| `429` | Rate limit exceeded for this caller. Carries `Retry-After` (seconds until one request will succeed) and `X-RateLimit-Limit`. Tunable with `RATE_LIMIT_PER_MIN` / `RATE_LIMIT_BURST`; the probe and `/metrics` paths are never limited. |

---

## Related

- [CLI Reference](cli-reference.md) — the `kq` client that wraps this API.
- [Agent Behaviors](agent-behaviors.md) — what happens between request and answer.
- [Security](security.md) — roles, key formats, and what each role may do.
