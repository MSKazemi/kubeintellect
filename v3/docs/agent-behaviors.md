---
description: >-
  How the KubeIntellect v3 DeepAgents coordinator investigates failures — error
  interpretation, snapshot pre-seed + on-demand refresh, parallel discipline,
  on-demand playbooks, and visible plans.
---

# Agent Behaviors

The KubeIntellect v3 coordinator implements five behaviors that shape how it
investigates Kubernetes issues. Unlike v2 — where each behavior is an independent
environment toggle — v3 delegates control flow to the **DeepAgents** framework, so
only the kubectl error interpreter is env-configurable; the other four are **always
on**, encoded in the coordinator's system prompt
(`app/agent/prompts.py::MAIN_INSTRUCTIONS`) or realized as agent tools rather than
read from the environment. See [Configuration → Agent behavior flags](configuration.md#agent-behavior-flags).

These behaviors live in the **coordinator** of the v3 DeepAgents topology — the same
agent that dispatches the five specialist subagents (pod, metrics, logs, events,
`deep_investigator`) and synthesises their findings. For how the coordinator,
subagents, snapshot, and streaming fit together, see [Architecture](architecture.md).

| Behavior | Configurable? |
|----------|---------------|
| [kubectl error interpreter](#kubectl-error-interpreter) | `KUBECTL_ERROR_HINTS_ENABLED` (default on) |
| [Snapshot pre-seed & refresh](#snapshot-pre-seed-and-refresh) | always on (prompt + `refresh_snapshot` tool) |
| [Gather-then-conclude discipline](#gather-then-conclude-discipline) | always on (prompt) |
| [On-demand playbooks](#on-demand-playbooks) | always on (`lookup_playbook` tool) |
| [Visible investigation plan](#visible-investigation-plan) | always on (prompt) |

---

## kubectl error interpreter

When `kubectl` exits non-zero, the tool layer scans `stderr` for known patterns
(NotFound, Forbidden, connection refused, missing CRD, immutable field, …) and
appends a single-line hint after the original error. The LLM sees both — the raw
error is never replaced.

**Why.** Stops the agent from looping on errors it could have skipped. Example:

```
Error from server (NotFound): pods "payments-1" not found
→ Pod may have been rescheduled — re-run `kubectl get pods -n <ns>` to find the new name.
```

**Disable:** `KUBECTL_ERROR_HINTS_ENABLED=false` (this is the one real agent-behavior
env var in `app/core/config.py`).

---

## Snapshot pre-seed and refresh

v3 has **no `context_fetcher` graph node** (that was v2). Instead, `app/agent/runner.py`
**pre-seeds** a cluster snapshot into the DeepAgents virtual filesystem as `/snapshot.md`
(pod list + Warning events) at the start of each turn, and the coordinator reads it
directly. When the snapshot is missing, older than ~30 s, or the coordinator just
mutated the cluster, the coordinator calls the **`refresh_snapshot`** tool
(`app/tools/snapshot_tool.py`) to rebuild it.

A prompt directive biases the coordinator toward answering list-shaped, read-only
questions from `/snapshot.md` when the cluster is healthy, and **always** fetches fresh
data when:

- The question targets a specific named pod / deployment / service.
- The user asks about logs, metrics, history, "yesterday", "last N hours".
- The coordinator just performed a mutation (verifies via a fresh read).
- The query contains `now` / `right now` / `currently`.
- The snapshot is stale.

**Why a soft bias, not a hard gate?** Pod state changes fast; a hard gate could return
stale answers. This bias is part of the coordinator prompt and is not environment-tunable
in v3 (the v2 vars `SNAPSHOT_SUFFICIENCY_MODE` / `SNAPSHOT_FRESHNESS_SECONDS` have no
effect here).

---

## Gather-then-conclude discipline

A prompt-only directive: when tools are needed, follow PLAN → FETCH → SYNTHESIZE. Emit
all independent tool calls in a single response (parallel), then synthesize once. Never
interleave partial answers with more tool calls.

**Exception:** sequential dependencies (e.g. find a pod's name → describe that pod) are
allowed; even then, gather everything else in parallel at each step.

This is always on — part of the coordinator's core system prompt.

---

## On-demand playbooks

For the top recurring Kubernetes failure modes, v3 ships a YAML playbook with a
deterministic investigation sequence. Unlike v2 (which prompt-injects every matched
playbook), the v3 coordinator fetches a playbook **on demand** by calling the
**`lookup_playbook(symptom)`** tool (`app/tools/playbook_tool.py`) when it recognises a
textbook pattern — keeping prompts small on simple turns.

**Playbooks shipped (10, in `app/agent/playbooks/`):**

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

`lookup_playbook` matches by exact name, then substring, then trigger regex. The
coordinator still has agency — it can deviate when the situation warrants — but the
playbook gives it a strong default.

---

## Visible investigation plan

For queries requiring three or more tool calls, the coordinator writes its plan as the
first line of the response:

```
INVESTIGATION_PLAN:
- Check pod status in default namespace
- Describe the crashing pod
- Query Loki for errors in the last 30m
- Propose a fix
```

The plan block is parsed out of the message body and emitted as a structured `PlanEvent`
on the SSE stream (`app/streaming/emitter.py`). UI clients (kube-q, browsers) can render
it as a checklist; Langfuse traces show it for post-mortem review.

**Why.** Makes multi-step investigations transparent and gives the agent an anchor to
stay on-track. Trivial single-call queries skip the plan (threshold: ≥ 3 steps). Always
on — part of the coordinator prompt.

---

## How they compose

A typical investigation of a CrashLoopBackOff pod:

1. **Snapshot pre-seed** — `runner.py` writes `/snapshot.md`; the coordinator reads it and
   sees the unhealthy pod.
2. **On-demand playbook** — recognising the pattern, the coordinator calls
   `lookup_playbook("CrashLoopBackOff")` to get the describe → previous-logs → events
   sequence.
3. **Investigation plan** — for a 3+ step query, the coordinator emits `INVESTIGATION_PLAN: …`
   first; the UI shows the checklist.
4. **Parallel discipline** — the coordinator emits all independent tool calls in one response;
   for deep RCA it delegates to the specialist subagents via `task()`.
5. **Error interpreter** — if any kubectl call returns a known error pattern, the hint is
   appended before the LLM sees it, avoiding retry loops.
6. Final answer references each plan step and proposes a fix from the playbook's
   `recommended_fix_template`.
