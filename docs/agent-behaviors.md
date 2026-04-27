---
description: >-
  How the KubeIntellect coordinator investigates failures — error interpretation,
  snapshot bias, parallel discipline, playbooks, and visible plans.
---

# Agent Behaviors

The KubeIntellect coordinator implements five additive behaviors that shape how
it investigates Kubernetes issues. Each is feature-flagged in
[Configuration → Agent behavior flags](configuration.md#agent-behavior-flags).

| Behavior | Default |
|----------|---------|
| [kubectl error interpreter](#kubectl-error-interpreter) | on |
| [Snapshot sufficiency gate](#snapshot-sufficiency-gate) | `lenient` |
| [Gather-then-conclude discipline](#gather-then-conclude-discipline) | always on |
| [Playbook library](#playbook-library) | on |
| [Visible investigation plan](#visible-investigation-plan) | on |

---

## kubectl error interpreter

When `kubectl` exits non-zero, the tool layer scans `stderr` for known patterns
(NotFound, Forbidden, connection refused, missing CRD, immutable field, …) and
appends a single-line hint after the original error. The LLM sees both — the
raw error is never replaced.

**Why.** Stops the agent from looping on errors it could have skipped. Example:

```
Error from server (NotFound): pods "payments-1" not found
→ Pod may have been rescheduled — re-run `kubectl get pods -n <ns>` to find the new name.
```

**Disable:** `KUBECTL_ERROR_HINTS_ENABLED=false`.

---

## Snapshot sufficiency gate

`context_fetcher` runs at the start of every turn and pre-fetches a cluster
snapshot (pod list + Warning events). C2 adds a soft prompt bias: when the
snapshot is healthy and the user asks a list-shaped, read-only question, the
coordinator is encouraged to answer from the snapshot without an extra
`kubectl get pods`.

**Always falls back to fresh data when:**
- The question targets a specific named pod / deployment / service.
- The user asks about logs, metrics, history, "yesterday", "last N hours".
- The coordinator just performed a mutation (verifies via fresh `get`).
- The query contains `now` / `right now` / `currently`.
- The snapshot is older than `SNAPSHOT_FRESHNESS_SECONDS` (default 30s).

**Why a soft bias, not a hard gate?** Pod state changes fast in Kubernetes; a
hard gate could return stale answers. The soft bias only fires for clean
snapshots and list-shaped questions, with explicit always-fetch escape hatches.

**Modes:** `off` (no bias — pre-C2 behavior), `lenient` (default — bias only
when truly applicable), `strict` (aggressive bias — opt in for trusted
deployments).

**Set:** `SNAPSHOT_SUFFICIENCY_MODE=off|lenient|strict`,
`SNAPSHOT_FRESHNESS_SECONDS=30`.

---

## Gather-then-conclude discipline

A prompt-only directive: when tools are needed, follow PLAN → FETCH → SYNTHESIZE.
Emit all independent tool calls in a single response (parallel), then synthesize
once. Never interleave partial answers with more tool calls.

**Exception:** sequential dependencies (e.g. find a pod's name → describe that
pod) are allowed; even then, gather everything else in parallel at each step.

This is always on — it is part of the coordinator's core system prompt.

---

## Playbook library

For the top recurring Kubernetes failure modes, KubeIntellect ships a YAML
playbook with a deterministic investigation sequence. When `context_fetcher`
detects a matching pattern in the snapshot, the coordinator's system prompt
includes the playbook(s) inline — guiding it to follow proven steps before
improvising.

**Playbooks shipped (10):**

- `CrashLoopBackOff`
- `OOMKilled`
- `ImagePullBackOff` / `ErrImagePull`
- `PendingInsufficientResources`
- `PendingSchedulingConstraints` (taints / affinity / nodeSelector)
- `CreateContainerConfigError`
- `ContainerCreatingStuck` (volume / CSI)
- `TerminatingStuck` (finalizers)
- `ReadinessProbeFailing` (also covers liveness)
- `ServiceUnreachable`

**Schema** (drop a YAML file into `app/agent/playbooks/`):

```yaml
name: <unique pattern name>
triggers:
  - pod_status_regex: "<regex on STATUS column>"
  - event_reason_regex: "<regex on Warning event REASON>"
  - event_message_regex: "<regex on Warning event MESSAGE>"
investigation_steps:
  - "<imperative step 1>"
  - "<imperative step 2>"
expected_evidence:
  - "<what to look for>"
recommended_fix_template: |
  <multi-line fix template; placeholders welcome>
```

A playbook matches if any of its triggers matches. The coordinator still has
agency — it can deviate when the situation warrants — but the playbook gives it
a strong default.

**Disable:** `PLAYBOOKS_ENABLED=false`.

---

## Visible investigation plan

For queries requiring three or more tool calls, the coordinator writes its plan
as the first line of the response:

```
INVESTIGATION_PLAN:
- Check pod status in default namespace
- Describe the crashing pod
- Query Loki for errors in the last 30m
- Propose a fix
```

The plan block is parsed out of the message body and emitted as a structured
`PlanEvent` on the SSE stream. UI clients (kube-q, browsers) can render it as a
checklist; Langfuse traces show it for post-mortem review.

**Why.** Makes multi-step investigations transparent and gives the agent an
anchor to stay on-track. Trivial single-call queries skip the plan (threshold:
≥ 3 steps).

**Disable:** `INVESTIGATION_PLAN_ENABLED=false`.

---

## How they compose

A typical investigation of a CrashLoopBackOff pod, with all behaviors on:

1. **Snapshot gate** — `context_fetcher` builds the snapshot, sees the unhealthy pod, sets
   `snapshot_has_issues=true` and matches the `CrashLoopBackOff` playbook.
2. The coordinator system prompt now includes the snapshot, the
   playbook details (describe → previous logs → events), and the snapshot
   sufficiency block (which won't fire because issues are present — we always
   fetch when unhealthy).
3. **Investigation plan** — for a 3+ step query, the coordinator emits
   `INVESTIGATION_PLAN: …` first; UI shows the checklist.
4. **Parallel discipline** — coordinator emits all independent tool calls in one response.
5. **Error interpreter** — if any kubectl call returns a known error pattern, the hint is
   appended before the LLM sees it, avoiding retry loops.
6. Final answer references each plan step and proposes a fix from the
   playbook's `recommended_fix_template`.

Each phase can be flipped independently if you need to roll one back.
