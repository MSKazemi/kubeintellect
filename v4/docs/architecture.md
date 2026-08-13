---
description: >-
  System-level architecture reference for KubeIntellect: how the FastAPI app,
  LangGraph graph, tools, HITL gate, and streaming layer fit together, plus the
  feature-flagged V4 MAPE-K autonomic loop.
---
# Architecture

System-level reference for KubeIntellect: how the FastAPI app, LangGraph graph, tools, HITL gate, and streaming layer fit together. Read this first; then drill into per-subsystem deep dives.

**Related docs**

- [Agent behaviors](agent-behaviors.md) — five additive coordinator behaviors (kubectl error hints, snapshot bias, gather-then-conclude, playbook library, visible plan)
- [Reflexion subsystem](reflexion.md) — cluster-scoped outcome learning, cooldown/decay/verification gates
- [Configuration](configuration.md) — env vars, feature flags, deployment values
- [Security](security.md) — auth, RBAC, namespace/resource blocks

---

## Overview

KubeIntellect is a FastAPI + LangGraph multi-agent system for Kubernetes operations. It accepts natural-language queries via HTTP/SSE, runs LLM-orchestrated investigations against a live cluster, and streams typed events (status, tool calls, tokens) back to the client. Write operations are gated behind human-in-the-loop approval.

---


## V4 architecture (feature-flagged preview)

The V4 platform layers form a **MAPE-K autonomic loop** (Monitor → Analyze →
Plan → Execute, around a central Knowledge base) over the live cluster:

![KubeIntellect V4 as a MAPE-K autonomic loop: Monitor (Sensorium, kubectl --watch) → Analyze (Detectors, compiled, zero tokens) → Plan (Cortex + autonomy ladder, tiered models) → Execute (guarded tools, HITL gate), all reading from and writing to a central Knowledge base (memory hierarchy + flight recorder); the cluster is observed and acted upon to close the loop.](figures/mape-k-loop.png)

*Purple = the MAPE phases; orange = the shared Knowledge base every phase reads
and writes; blue = the live cluster, observed by Monitor and acted on by Execute.
The TikZ source is at `docs/figures/mape-k-loop.tex`.*

They ship alongside the V2 graph and activate via feature flags
(see [Configuration](configuration.md)):

| Layer | What it does | Docs | Flag (default) |
|---|---|---|---|
| Sensorium + detectors | `kubectl --watch` perception; compiled playbook predicates detect known failures with **zero LLM tokens** | [Agent Behaviors](agent-behaviors.md) | `SENSORIUM_ENABLED` (on) |
| Anticipatory detection | Trend predicates project a range-PromQL metric toward its threshold (least-squares ETA, zero tokens) and fire a `predicted` finding **before** a slow-burn failure; capped at autonomy A1 (never auto-fix) — ADR-010 | [Capabilities](capabilities.md) | `PREDICTIVE_DETECTION_ENABLED` (off) |
| Memory hierarchy | Episodic recall + temporal knowledge graph injected into answers | [Memory](memory.md) | `MEMORY_HIERARCHY_ENABLED` (on) |
| Memory V5 upgrade (experimental) | State-of-the-art-grounded, additive slices behind default-off flags: hybrid RRF recall, bi-temporal KG, multi-hop blast-radius (PPR), write reconciliation, episode→rule→detector promotion, importance/surprise ranking + prospective "re-check later" memory, MINJA-hardened write path with hash-chain tamper-evidence, and a RAPTOR-style theme summary tree | [Memory](memory.md) | 9 flags, **all off** (see [Configuration](configuration.md)) |
| Flight recorder | Hash-chained, tamper-evident audit log with deterministic replay | [Flight Recorder](flight-recorder.md) | `FLIGHT_RECORDER_ENABLED` (on) |
| Incident postmortems | Read-only grounded narrative over the flight recorder: seq-cited timeline + `chain_valid` verdict; optional constrained LLM narrative — ADR-011 | [CLI](cli-reference.md#kq-postmortem-session-id) | `POSTMORTEM_ENABLED` (on) |
| Autonomy ladder + watchtower | Detector firings open autonomous investigations; morning digest | [Autonomy](autonomy.md) | `WATCHTOWER_ENABLED` (on, level A1) |
| NL detector authoring | Compile a plain-English failure into a detect block, run it in **shadow** (observes only, never reaches the watchtower) until a human promotes it — ADR-012 | [CLI](cli-reference.md#kq-detector-teach-a-new-failure-in-plain-english) | `NL_DETECTOR_AUTHORING_ENABLED` (off) |
| Cortex V4 | Explicit-node reasoning graph: structured triage plan, live step transitions, true token streaming, tiered models | [Agent Behaviors](agent-behaviors.md) | `CORTEX_V4_ENABLED` (off) |

The V2 graph below remains the default reasoning path until the V4 cortex
meets resolution parity on the evaluation suite.

## System Components

```
Browser / CLI (kq)
      │  HTTP POST /v1/chat/completions
      │  SSE GET  /v1/chat/stream/{session_id}
      ▼
FastAPI app  (packages/kubeintellect-server/app/main.py)
      │
      ├── Auth middleware (API key → role)
      ├── Chat completions endpoint
      ├── SSE streaming endpoint
      └── asyncio.create_task → run_session()
                                     │
                              LangGraph graph
                              (workflow.py)
```

---

## LangGraph Graph Topology

```
START
  │
  ▼
memory_loader          — load pinned cluster context from DB (or Redis)
  │
  ▼
context_fetcher        — parallel kubectl pods + warning events pre-fetch
  │
  ▼
coordinator            — LLM decision + tool calls (ReAct loop)
  │
  ├─── TARGETED_sentinel ──► targeted_investigator ──► coordinator
  │                          (3 parallel reads: describe, events, deploys)
  │
  ├─── RCA_REQUIRED ──────► subagent_executor x4 (parallel fan-out via Send)
  │                          pod | metrics | logs | events
  │                                  │
  │                          coordinator (synthesis)
  │
  └─── direct answer ──────► END
```

### Routing logic (`route_coordinator`)

| State condition | Route |
|---|---|
| `targeted_investigation` is set | `targeted_investigator` |
| `rca_required = True` | `list[Send]` → 4× `subagent_executor` |
| `rca_result` is not None | `END` (synthesis complete) |
| `findings` present, no `rca_result` | `coordinator` (synthesis mode) |
| otherwise | `END` (direct answer) |

---

## Node Reference

### `memory_loader`

- Loads pinned cluster context from Postgres/SQLite (key-value store keyed by `user_id`).
- Resets `findings` to `[]` via `_findings_reducer(None)` — prevents stale RCA findings bleeding into the next turn.
- Emits `StatusEvent(phase="loading")`.

### `context_fetcher`

- Runs two `kubectl` commands in parallel via `asyncio.to_thread`:
  - `kubectl get pods --all-namespaces`
  - `kubectl get events --all-namespaces --sort-by=.lastTimestamp --field-selector=type=Warning`
- Caps each output at `_SNAPSHOT_MAX_CHARS = 8_000` chars.
- Builds `cluster_snapshot` string injected into the coordinator's system prompt.
- **Why this matters**: Kubernetes events contain the exact error message (e.g., `"couldn't find key DB_URL in Secret"`). With the snapshot pre-loaded, the coordinator can diagnose without additional tool calls.
- Adds ~150ms to every query (2 parallel subprocess calls against local kubeconfig).

### `coordinator`

Two modes, same node:

**Decision mode** (no findings yet):
- Builds system prompt from: `_COORDINATOR_SYSTEM` + optional `memory_context` + `cluster_snapshot`.
- Trims session history to last `_MAX_SESSION_MESSAGES = 20` messages (prevents Azure 400 errors from context overflow).
- Calls `create_react_agent` (LangGraph prebuilt) with 4 tools: `run_kubectl`, `run_helm`, `query_prometheus`, `query_loki`.
- Instructs LLM to emit parallel tool calls in one response for independent operations.
- Parses response for sentinels: `RCA_REQUIRED` or `TARGETED: namespace=X, pod=Y, issue=Z`.
- Applies `_trim_tool_messages()` to all new `ToolMessage` objects before writing to state (caps at 2,000 chars per message).

**Synthesis mode** (findings present):
- Formats all 4 subagent findings as XML.
- Single LLM call (no ReAct loop) to synthesize into structured `RCAResult` JSON.
- Returns `AIMessage` with human-readable RCA summary.

### `targeted_investigator`

- Triggered when coordinator emits `TARGETED: namespace=X, pod=Y, issue=Z`.
- Runs 3 parallel reads via `asyncio.to_thread`:
  - `kubectl describe pod <pod> -n <ns>`
  - `kubectl get events -n <ns> --sort-by=.lastTimestamp`
  - `kubectl get deployments -n <ns>`
- Appends findings to `cluster_snapshot` and clears `targeted_investigation`.
- Returns to coordinator with enriched snapshot for final answer.
- **Design rationale**: Avoids a full 4-subagent RCA fan-out for single-resource issues. Adds 1 extra LLM round-trip but uses 3 parallel reads instead of 4× subagent overhead.
- **Note**: In practice the LLM often makes parallel tool calls directly in the ReAct loop for single-pod issues (more efficient — no extra round-trip). The TARGETED path handles cases where the LLM decides pre-enrichment is needed before answering.

### `subagent_executor`

- LangGraph node wrapping `run_subagent()`.
- Receives a `SubagentInput` payload (domain, session_id, messages, evidence_bundle).
- Emits `StatusEvent(phase="investigating")`.
- Returns `{"findings": [AgentFinding]}` — accumulated by `_findings_reducer`.

### `run_subagent` (in `nodes/subagent.py`)

- Each of 4 domains (pod, metrics, logs, events) has a domain-specific system prompt.
- Receives `evidence_bundle` (the cluster snapshot) in system prompt — avoids duplicate `kubectl get pods` calls.
- Uses `gpt-4o-mini` (Azure) or `OPENAI_SUBAGENT_MODEL` for cost efficiency.
- Returns structured `AgentFinding` (domain, signals, hypothesis, confidence 0-1, evidence, tool_calls_made).

---

## State Schema (`AgentState`)

| Field | Type | Description |
|---|---|---|
| `messages` | `Annotated[list[BaseMessage], add_messages]` | Full conversation history. `add_messages` reducer appends (never overwrites). |
| `memory_context` | `str` | Pinned cluster context loaded by `memory_loader`. |
| `cluster_snapshot` | `str` | Pre-fetched by `context_fetcher`. Injected into coordinator system prompt. Reset each turn. |
| `findings` | `Annotated[list[AgentFinding], _findings_reducer]` | Subagent findings. Custom reducer: `None` resets, list appends. |
| `rca_required` | `bool` | Set by coordinator when LLM emits `RCA_REQUIRED`. |
| `rca_result` | `dict \| None` | Final synthesized RCA (stored as `dict` to avoid LangGraph msgpack warning). |
| `targeted_investigation` | `dict[str, str] \| None` | `{namespace, pod, issue}` set by coordinator when LLM emits TARGETED sentinel. Cleared by `targeted_investigator`. |
| `pending_hitl` | `dict[str, Any] \| None` | HITL state: `{action_id, command, risk_level, human_summary}`. |
| `session_id` | `str` | Per-conversation ID (maps to LangGraph thread_id). |
| `user_id` | `str` | API key-derived user identifier. |
| `user_role` | `str` | `superadmin \| admin \| operator \| readonly` |

---

## Tool Architecture

All tools live in `app/tools/`. Registered in `app/tools/registry.py` as `ALL_TOOLS`.

### `run_kubectl`

- Runs `kubectl` as a subprocess with configurable timeout.
- **Role-based access control**:
  - `readonly`: read-only verbs only (get, describe, logs, top, explain, diff, version, api-resources, api-versions)
  - `operator`: read + medium-risk writes (create, apply, scale, set, rollout, exec, port-forward, cp, attach, label, annotate, taint, cordon, uncordon, drain)
  - `admin`: read + medium + high-risk (patch, delete, replace, edit, run, expose, autoscale, config, certificate, cluster-info, proxy)
  - `superadmin`: all verbs, bypasses namespace block (but still blocks secrets/serviceaccounts)
- **Namespace block**: `KUBECTL_BLOCKED_NAMESPACES` env var (default: kubeintellect, monitoring, kube-system, etc.)
- **Resource block**: `KUBECTL_BLOCKED_RESOURCES` env var (default: secret, serviceaccount)
- **HITL gate**: destructive commands trigger `interrupt()` → `HITLRequired` → pauses graph → resumes via `Command(resume=bool)`

### `run_helm`

- Read-only Helm inspection: `list`, `get`, `status`, `history`, `env`, `version`, `show`, `search`.
- Write verbs (`install`, `upgrade`, `rollback`, `uninstall`, `repo`, …) are blocked — KubeIntellect never mutates releases through Helm.
- Output is capped at 6,000 characters.

### `query_prometheus`

- PromQL queries against `PROMETHEUS_URL`.
- `range_minutes=0` → instant query (current snapshot).
- `range_minutes>0` → range query.
- Returns empty/error string when `PROMETHEUS_URL` is not configured.

### `query_loki`

- LogQL queries against `LOKI_URL`.
- `since` parameter accepts duration strings (`1h`, `2d`).
- Returns empty/error string when `LOKI_URL` is not configured.

---

## HITL (Human-in-the-Loop) Flow

```
LLM decides to run a destructive kubectl command
          │
          ▼
run_kubectl checks risk level via _classify_risk()
          │
          ▼ (medium/high risk)
HITLRequired exception raised
          │
          ▼
LangGraph interrupt() — pauses graph at checkpoint
          │
          ▼
SSE stream emits HitlRequestEvent {risk_level, command, stdin_yaml}
          │
          ▼
User sees approval prompt in UI/CLI
          │
  ┌───── yes ──────┐
  │                │
  ▼                ▼
Command(resume=True)  Command(resume=False)
  │                │
  ▼                ▼
kubectl runs    Denied — no-op
```

The graph is checkpointed at the interrupt point. The next user message is checked for an active interrupt (`graph.aget_state()`) and routed to `Command(resume=...)` instead of `_fresh_turn_state()`.

---

## Streaming Architecture

Every query runs as `asyncio.create_task(run_session(...))`. The task writes to a per-session `asyncio.Queue` via `emit()`. The SSE endpoint reads from this queue and yields Server-Sent Events.

### Typed events emitted

| Event | When |
|---|---|
| `StatusEvent` | Node entry (memory_loader, context_fetcher, coordinator, subagent_executor) |
| `ToolCallEvent` | `on_tool_start` from LangGraph astream_events |
| `ToolResultEvent` | `on_tool_end` from LangGraph astream_events (first 500 chars) |
| `TokenEvent` | `on_chat_model_stream` — each LLM token chunk |
| `PlanEvent` | Coordinator emits `INVESTIGATION_PLAN:` block; parsed out and re-emitted as structured steps |
| `HitlRequestEvent` | HITL interrupt detected after stream ends |
| `ErrorEvent` | Any unhandled exception in `run_session` |

**Important**: `ToolResultEvent` uses the raw tool output from `on_tool_end` (fires BEFORE `_trim_tool_messages`). Users see full tool output; only the LLM re-ingestion is trimmed.

---

## Structured Logging and Observability

All log output is JSON (`LOG_FORMAT=json`). Every line carries `time`, `level`, `logger`, `request_id`, and any `extra={}` fields the caller passes. The JSON formatter emits all extra fields — there is no fixed allowlist.

Three key production events are logged at `INFO` level and carry a `session_id` field, making them queryable by session in Loki:

| Logger event | Key fields | Source |
|---|---|---|
| `context_fetcher: snapshot_complete` | `session_id`, `cluster_id`, `snapshot_has_issues`, `snapshot_has_warnings`, `snapshot_pod_count`, `matched_playbooks` | `context_fetcher.py` |
| `coordinator: routing_decision` | `session_id`, `routing_decision` (`direct`\|`TARGETED`\|`RCA`), `elapsed_ms` | `coordinator.py` |
| `run_kubectl: hitl_classification` | `session_id`, `hitl_verb`, `hitl_risk_level`, `hitl_cmd` | `kubectl_tool.py` |

**Querying by session in Loki:**
```logql
{app="kubeintellect"} |= "eval-1746295200-10-incident-rca" | json | level="INFO"
```

The `session_id` is the same value set in the SSE stream and stored in Langfuse traces, so all three observability signals (logs, traces, metrics) are correlatable by a single ID.

---

## Context Management

### Session trimmer (`_trim_session_messages`)

Caps session history at `_MAX_SESSION_MESSAGES = 20` before passing to LLM. Always advances to the first `HumanMessage` in the window — prevents Azure 400 errors from orphaned `ToolMessage` without matching `AIMessage(tool_calls)`.

### Tool output trimmer (`_trim_tool_messages`)

Applied to all new `ToolMessage` objects before writing to state (`AgentState.messages`).

Strategy:
- kubectl table output (has `NAME` column): header + first 30 rows + any rows matching `error|warning|failed|pending|oomkilled|crashloop|backoff|imagepull|containercreating`
- logs / describe / prometheus / loki: first 60 lines
- Hard cap: 2,000 chars per message

---

## Auth & RBAC

Four-tier role model, enforced at API layer:

| Role | Capabilities | HITL |
|---|---|---|
| `superadmin` | All operations, bypass namespace block | Write ops still gated |
| `admin` | High + medium risk ops, infra namespace writes blocked | Required for medium/high risk |
| `operator` | Medium risk only (create, apply, scale, exec…) | Required for medium risk |
| `readonly` | Read-only verbs only | N/A (writes rejected before LLM) |

API key → role mapping via `KUBEINTELLECT_*_KEYS` env vars (comma-separated). Optional HMAC backend for demo keys (signed, expiry-checked, no list needed).

---

## Checkpointing & Storage

LangGraph checkpointer persists full `AgentState` between turns:

- **Production**: `AsyncPostgresSaver` (Postgres via `DATABASE_URL` or `POSTGRES_*` vars)
- **Local/dev**: `AsyncSqliteSaver` (`USE_SQLITE=true`, path: `~/.kubeintellect/kubeintellect.db`)

Every turn begins with `_fresh_turn_state()` which resets transient fields (`cluster_snapshot`, `rca_required`, `rca_result`, `targeted_investigation`, `pending_hitl`) while preserving the `messages` accumulation via `add_messages` reducer.

**Memory context** (`memory_loader`) is loaded from a separate `cluster_memory` table, keyed by `user_id`. This is pinned, human-edited context (e.g., "default namespace is production") injected into every coordinator system prompt.

---

## LLM Configuration

| Setting | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `azure` | `azure` or `openai` |
| `AZURE_COORDINATOR_DEPLOYMENT` | `gpt-4o` | Coordinator model (Azure) |
| `AZURE_SUBAGENT_DEPLOYMENT` | `gpt-4o-mini` | Subagent model (Azure) |
| `AZURE_OPENAI_API_VERSION` | `2024-10-01-preview` | Enables Azure prefix caching |
| `OPENAI_COORDINATOR_MODEL` | `gpt-4o` | Coordinator model (direct OpenAI) |
| `OPENAI_SUBAGENT_MODEL` | `gpt-4o-mini` | Subagent model (direct OpenAI) |

**Azure prefix caching**: With `2024-10-01-preview`, the static `_COORDINATOR_SYSTEM` prefix (~800 tokens) is cached after the first call. Each subsequent call in the same deployment saves ~800 tokens of input cost.

---

## Observability

The observability stack is **shared across all KubeIntellect versions**: a single Kind cluster runs one Prometheus/Grafana/Loki and one Langfuse instance that serve every version (v1, v2, v4, …). It is deployed and managed from the **repo-root Makefile**, not per-version — so individual versions never stand up their own monitoring.

- **Prometheus** (metrics): App exposes `/metrics` (Starlette Prometheus middleware). Scrape target for the shared stack. Also **queried by the agent** for cluster diagnostics.
- **Loki** (logs): App logs are collected by Promtail from in-cluster pods and stored in the shared Loki. Also **queried by the agent** for cluster diagnostics.
- **Grafana** (dashboards): Dashboards over Prometheus + Loki.
- **Langfuse** (LLM tracing): Optional per-LLM-call tracing of token usage, cost, and latency for the agent's own calls. Set `LANGFUSE_ENABLED=true` + keys. Every LLM call, tool call, and subagent span is traced. Configure `LANGFUSE_HOST` (e.g., `langfuse-web.monitoring.svc.cluster.local:3000`).

All observability services install to the `monitoring` namespace. URLs wired via Helm `values.yaml` → ConfigMap → env vars.

**One shared Langfuse project, per-version tagging.** All versions send traces to a single Langfuse project; each version stamps its traces with a `version:vN` tag (via the `KI_VERSION` setting) so per-version cost and token usage can be filtered out of the shared project. Separate per-version Langfuse projects would require Langfuse Enterprise and are not used.

---

## Deployment

### Helm (Kubernetes)

```
deploy/helm/kubeintellect/
├── Chart.yaml
├── values.yaml          ← single source of truth for all config
├── templates/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml
│   ├── configmap.yaml
│   └── ...
```

Key values:
- `config.prometheusUrl`, `config.lokiUrl` — injected as env vars via ConfigMap
- `config.blockedNamespaces`, `config.blockedResources` — RBAC guards
- `hotReload.enabled` — mounts `packages/kubeintellect-server/app/` as a hostPath for dev (watchfiles restarts on change)
- `ingress.hosts` — list of hostnames served

### Health probes

```yaml
readinessProbe:
  path: /healthz
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 10   # raised from 3 — event loop can be briefly loaded under parallel kubectl calls
  failureThreshold: 3
livenessProbe:
  path: /healthz
  initialDelaySeconds: 15
  periodSeconds: 30
  timeoutSeconds: 10
  failureThreshold: 3
```

---

## Key Design Decisions

### Why `create_react_agent` instead of a custom ReAct loop?

LangGraph's prebuilt `create_react_agent` natively supports parallel tool calls when the LLM returns multiple `tool_calls` in one `AIMessage`. The LLM handles execution ordering; we just instruct it to emit independent calls together. No custom loop needed.

### Why pre-fetch cluster snapshot before LLM runs?

Kubernetes puts the exact error message in events: `"couldn't find key DB_URL in Secret scenario-test/app-secrets"`. By pre-fetching events before any LLM call, the coordinator can diagnose without additional tool calls. On a healthy cluster, warning events are near-empty — the cost is minimal (~150ms for 2 parallel subprocess calls).

### Why trim tool output before storing in state?

`add_messages` accumulates every `ToolMessage` in the session. Without trimming, a single `kubectl get pods --all-namespaces` call can add 4,000+ chars to every future turn's prompt. The trimmer caps at 2,000 chars; the SSE stream still shows users the full output (fired by `on_tool_end`, before trimming).

### Why store `rca_result` as `dict` instead of `RCAResult`?

LangGraph's msgpack checkpointer can't serialize unregistered Pydantic models — it logs `Deserializing unregistered type` warnings. Storing `rca.model_dump()` (plain dict) avoids this entirely.

### Why separate `coordinator` and `subagent_executor` nodes?

`coordinator` runs as a standard node (full `AgentState`). Subagents run as `Send` payloads (`SubagentInput`) with isolated state — no session history, just the current query + evidence bundle. This prevents the 6,700-token session history from being passed to subagents, which caused them to respond in prose instead of JSON.

---

## Phase A+B regression-fix highlights

Snapshot of measurable improvements after the Phase A+B refactors landed (2026-04-26). These are point-in-time before/after deltas for the regressions those phases targeted, **not** steady-state performance numbers — current numbers will differ as the agent and tooling continue to evolve.

| Metric | Before | After |
|---|---|---|
| Subagent confidence (RCA) | 0.00 (empty JSON) | 0.80–0.90 |
| Duplicate `kubectl get pods` per RCA | 4 | 0 |
| CreateContainerConfigError diagnosis | Fails (tries to read blocked secret) | Correct (reads events snapshot) |
| Multi-tool query (parallel) | 3 sequential loops | 1 AIMessage with 3 tool_calls |
| Session history growth per turn | +4,442 tokens (ISS-01) | ≤500 tokens |
| context_fetcher latency | N/A | ~150ms |

---

## File Map

Since V4 the repository is a uv workspace; the server's Python module is still
named `app`, housed in `packages/kubeintellect-server/`:

```
packages/
├── ki-protocol/                  — shared SSE wire protocol (server + client views)
├── kube-q/                       — the kq terminal client (kube_q/ package)
└── kubeintellect-server/app/
├── agent/
│   ├── nodes/
│   │   ├── context_fetcher.py    — pre-fetch pods + events, cluster_id, playbook trigger match
│   │   ├── coordinator.py        — LLM decision + synthesis, tool trimmer, reflexion writes
│   │   ├── memory_loader.py      — load pinned cluster context + failure-pattern hints
│   │   └── subagent.py           — 4 specialist subagent runners
│   ├── playbooks/                — YAML playbook library + loader (23 playbooks)
│   ├── state.py                  — AgentState, SubagentInput, AgentFinding, RCAResult
│   ├── workflow.py               — LangGraph graph, targeted_investigator, route_coordinator
│   └── hitl.py                   — HITL approval/denial detection
├── api/
│   ├── middleware.py             — auth, logging
│   └── v1/endpoints/
│       ├── chat_completions.py   — POST /v1/chat/completions
│       ├── stream.py             — GET /v1/chat/stream/{session_id}
│       ├── health.py             — GET /healthz
│       ├── postmortem.py         — GET /v1/episodes/{id}/postmortem (ADR-011)
│       ├── detectors.py          — POST/GET /v1/detectors + promote/demote (ADR-012)
│       └── memory.py             — CRUD for pinned context
├── core/
│   ├── config.py                 — Settings (pydantic-settings, ~/.kubeintellect/.env)
│   └── llm.py                    — LLM factory (Azure/OpenAI), Langfuse callbacks
├── streaming/
│   └── emitter.py                — per-session queue, typed events, SSE serialization
├── tools/
│   ├── kubectl_tool.py           — run_kubectl with RBAC + HITL
│   ├── kubectl_errors.py         — error-pattern → one-line hint mapper (15 patterns)
│   ├── prometheus_tool.py        — query_prometheus + query_prometheus_range_raw (trend feed)
│   ├── loki_tool.py              — query_loki
│   └── registry.py               — ALL_TOOLS list
├── detectors/                    — zero-token detection engine
│   ├── models.py                — DetectBlock, WatchPredicate, TrendPredicate (ADR-010), Finding
│   ├── engine.py                — match/debounce, project_eta + evaluate_trends, shadow firing
│   ├── authoring.py             — NL → detect block compile + validate + stage (ADR-012)
│   ├── review.py                — shadow→active promote / demote
│   └── service.py               — sensorium wiring (reactive + trend + db-detector loops)
├── digest/
│   ├── builder.py               — morning digest view over the recorder
│   └── postmortem.py            — grounded incident postmortem (ADR-011)
├── utils/
│   └── redact.py                 — secret/PII scrubber for stored outcomes
├── cluster_id.py                 — stable cluster fingerprint (kube-apiserver + namespace count hash)
└── db/
    ├── audit.py                  — audit log (writes to Postgres)
    ├── memory_store.py           — rca_outcomes / failure_patterns reads + writes (reflexion)
    └── memory.py                 — pinned context CRUD
```
