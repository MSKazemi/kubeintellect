---
description: >-
  What you can ask KubeIntellect v3 — the tools it can reach, the failure
  patterns it recognizes, example queries, what a root-cause answer looks like,
  and what it will not do.
---

# What You Can Ask

KubeIntellect is an AI SRE you drive in plain English. A single **coordinator**
agent (built on DeepAgents, `app/agent/main_agent.py`) answers directly for
simple questions and, for root-cause investigations, fans out to **five
specialist subagents** and synthesises their findings into one answer.

---

## What it can reach

The coordinator has seven direct tools plus five subagents (`app/tools/`,
`app/agent/subagents.py`):

| Source | Tool | What it gives the agent |
|---|---|---|
| Cluster API | `run_kubectl` | Any `kubectl` verb — `get`, `describe`, `logs`, `top`, `events`, plus write verbs (`apply`, `scale`, `patch`, `delete`, …) behind an approval gate. |
| Metrics | `query_prometheus` | Instant and range PromQL against the cluster Prometheus (CPU, memory, saturation, SLOs). |
| Logs | `query_loki` | LogQL — cross-pod / cross-namespace aggregation, historical logs from crashed containers, error-rate metrics. |
| Cluster snapshot | `refresh_snapshot` | A fresh `/snapshot.md` (all-namespace pod list + Warning events) in the agent's virtual filesystem. |
| Playbooks | `lookup_playbook` | The canonical investigation steps for a known failure pattern (10 shipped playbooks). |
| Memory | `read_memory` / `write_memory` | Cross-session recall and persistence of findings (PostgreSQL — see [Memory](memory.md)). |

Metric and log tools require `PROMETHEUS_URL` / `LOKI_URL` to be set
([Configuration](configuration.md#observability-optional)); when they are empty,
kubectl-based diagnostics still work.

### The five specialists

For a root-cause question the coordinator delegates via `task()` to specialists
that run on the cheaper subagent model and write their findings to
`/findings/<name>.md`:

| Subagent | Tool(s) | Focus |
|---|---|---|
| `pod_specialist` | `run_kubectl` | Pod-level failures — CrashLoopBackOff, OOMKilled, ImagePullBackOff, probe failures, restarts. |
| `metrics_specialist` | `query_prometheus` | CPU / memory / saturation / SLO questions and trends. |
| `logs_specialist` | `query_loki` | Log-volume spikes, error-message hunts, cross-pod log correlation. |
| `events_specialist` | `run_kubectl` | Cluster events — scheduling failures, FailedMount, eviction, node-level symptoms. |
| `deep_investigator` | `run_kubectl`, `query_prometheus`, `query_loki` | Catch-all for cross-domain probes — compare namespaces, validate a NetworkPolicy, correlate a metric + log spike, RBAC audit. |

The first four fan out **in parallel** for a standard RCA; `deep_investigator`
is used once for unusual, cross-domain probes.

---

## Failure patterns it recognizes

KubeIntellect ships **10 playbooks** (`app/agent/playbooks/`) with a
deterministic investigation sequence for the most common Kubernetes failures.
The coordinator calls `lookup_playbook(symptom)` when a symptom matches a
textbook pattern:

| Area | Patterns |
|---|---|
| Container lifecycle | CrashLoopBackOff · OOMKilled (exit 137) · CreateContainerConfigError |
| Image / registry | ImagePullBackOff · ErrImagePull |
| Scheduling | Pending — insufficient resources · Pending — scheduling constraints (taints / affinity / nodeSelector) |
| Volumes / startup | ContainerCreating stuck (CSI / volume) · Terminating stuck (finalizers) |
| Health / networking | Readiness/liveness probe failing · Service unreachable (no endpoints) |

Each playbook returns investigation steps, the evidence to expect, and a
recommended-fix template.

---

## Example queries

### Diagnose a failure

- "Why is the payments pod crashing?"
- "What's wrong in the `demo-rca` namespace?"
- "The checkout deployment won't roll out — investigate."
- "Why is `resource-hog` stuck in Pending?"

### Survey cluster health

- "What pods are broken across all namespaces?"
- "List the pods in `demo` and their status."
- "Any Warning events in the last few minutes?"

### Investigate with metrics and logs

- "Is the `api` deployment hitting its memory limit?"
- "Show CPU saturation for `web` over the last hour."
- "Find `OutOfMemoryError` in the payments logs from the last 30 minutes."

### Understand a deployment

- "Describe the `web` deployment and its current replicas."
- "Why does the `api-server` service have no endpoints?"
- "What image is `bad-image` trying to pull?"

### Make a change (approval-gated)

- "Scale `web` to 5 replicas." → pauses for approval
- "Patch the `payments` memory limit to 512Mi." → pauses for approval
- "Delete the crashlooping pod so it reschedules." → pauses for approval

### Capacity, security & maintenance

- "Which nodes are tainted, and what tolerations do pending pods need?"
- "Is there a PodDisruptionBudget blocking the rollout?"
- "Compare resource requests between `staging` and `prod`."

---

## What a root-cause answer looks like

After a fan-out investigation, the coordinator synthesises the findings files
into a single evidence-cited answer. A good answer contains:

| Part | What it tells you |
|---|---|
| Root cause | The single most likely explanation, stated plainly. |
| Supporting evidence | Concrete lines — e.g. "memory limit 64Mi < working set 98Mi (exit 137)", not "memory was too low". |
| Per-domain findings | What pod / metrics / logs / events each contributed (or that a domain found nothing). |
| Recommended fix | A specific action, often from the matching playbook's fix template. |
| Approval prompt | If the fix is a write operation, the exact command it will run — gated on your `yes`. |

The coordinator quotes specific evidence lines rather than paraphrasing, and
after a confident conclusion it may call `write_memory` so future sessions
benefit (see [Memory](memory.md)).

---

## Safe changes (human-in-the-loop)

KubeIntellect can *make* changes, but every destructive or write operation pauses
for your explicit approval before `kubectl` runs:

```text
🟡 Approval Required — risk level: MEDIUM

Command:
  kubectl scale deployment/web --replicas=5 -n prod

Type `yes` or `/approve` to proceed, or `no` / `/deny` to cancel.
```

Approve with `yes` / `/approve`, deny with `no` / `/deny`, or say `approve all`
to skip gates for the rest of the session. Your role also bounds what is even
offered: `readonly` keys can't request writes at all, and `operator` keys can't
request high-risk verbs (delete, drain, replace, taint). See
[Security](security.md) for the full model.

---

## What it will not do

- **Read Secrets or ServiceAccount tokens.** `secret` and `serviceaccount` are
  blocked for every verb, in every namespace, for every role — the agent cannot
  exfiltrate cluster credentials ([Security](security.md#5-secret-protection-why-users-cant-steal-the-api-key)).
- **Write to infrastructure namespaces** (`kubeintellect`, `monitoring`,
  `kube-system`, …) — reads are allowed so it can diagnose itself, but writes are
  refused (superadmin excepted).
- **Run arbitrary shell.** Only `kubectl` runs, with `shell=False`; shell
  metacharacters are rejected and only `grep` is emulated after a pipe.
- **Execute a destructive command without your approval** — even for admins.

---

## Try it on purpose-built broken pods

`kubeintellect init` (or `kubeintellect kind-setup`) can deploy five intentionally
broken workloads to the `demo-rca` namespace so you have something to investigate:

| Workload | Failure | Try asking |
|---|---|---|
| `crash-loop` | CrashLoopBackOff | "Why is crash-loop crashing and how do I fix it?" |
| `oom-killer` | OOMKilled | "Why does oom-killer keep restarting?" |
| `bad-image` | ImagePullBackOff | "Why can't bad-image pull its image?" |
| `resource-hog` | Pending | "Why is resource-hog stuck in Pending?" |
| `api-server` | No endpoints | "Why does the api-server service have no endpoints?" |

---

## Related

- [Quickstart](quickstart.md) — get a cluster and a client running.
- [Agent Behaviors](agent-behaviors.md) — how it investigates.
- [CLI Reference](cli-reference.md) — driving it from `kq`.
- [Security](security.md) — roles and safety gates.
