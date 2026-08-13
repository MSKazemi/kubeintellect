---
description: >-
  What you can ask KubeIntellect — a catalog of capabilities, 30+ real example
  queries grouped by task, what a root-cause answer looks like, and the limits of
  what the agent will do.
---

# What You Can Ask

KubeIntellect is a **Human-Governed AI SRE for Kubernetes**. You ask questions in
plain English; it investigates your cluster with real tools, explains what it
found, and — with your approval — fixes it.

You don't need to know `kubectl` syntax, PromQL, or LogQL. Ask the way you'd ask a
colleague: *"why is the payments pod crashing?"*

---

## What it can reach

Every answer is grounded in live data from up to four sources:

| Source | Tool | What it gives the agent |
|---|---|---|
| **Cluster API** | `run_kubectl` | Pods, deployments, services, events, nodes, endpoints, logs, `describe`, YAML specs — read freely; writes are gated. |
| **Helm** | `run_helm` (read-only) | Release list, values, status, history — to reason about what was deployed. |
| **Metrics** | `query_prometheus` | CPU/memory/throughput/error-rate time series (when `PROMETHEUS_URL` is set). |
| **Logs** | `query_loki` | Application and system logs over a time window (when `LOKI_URL` is set). |

Metrics and logs are optional — without them, KubeIntellect still answers every
`kubectl`-shaped question. See [Configuration](configuration.md) to wire them up.

---

## Autonomous capabilities (V4)

V4 also works *between* your questions:

- **Zero-token known-failure detection** — the playbook library covers the 23 most common failures, and compiled detectors recognize 20 of them straight off the live cluster stream without spending a single LLM token. → [Agent Behaviors → V4 additions](agent-behaviors.md#v4-additions)
- **Self-opened investigations** — a firing detector opens its own investigation and publishes the report; how far it may go is set per namespace (A0–A3). → [Autonomous Operations](autonomy.md)
- **Morning digest** — `kq digest` summarizes findings, autonomous investigations, and rollback points from the last N hours. → [Autonomous Operations → digest](autonomy.md#the-morning-digest)
- **Tamper-evident audit replay** — every session is hash-chained in an append-only log; `kq replay <session-id>` reproduces it and verifies integrity. → [Flight Recorder & Replay](flight-recorder.md)
- **Anticipatory (predictive) detection** *(opt-in)* — trend predicates project a metric toward its limit and warn *before* a slow-burn failure (e.g. OOM) manifests, still at zero token cost; predictions only investigate, never auto-fix. → [CLI: `kq findings`](cli-reference.md#kq-findings-limit-n)
- **Grounded incident postmortems** — `kq postmortem <session-id>` turns the hash-chained log into a human postmortem where every line cites its recorded event and the audit chain is verified. → [CLI: `kq postmortem`](cli-reference.md#kq-postmortem-session-id)
- **Teach a new failure in plain English** *(opt-in)* — `kq detector new "..."` compiles a description into a detector that runs in shadow until you promote it. → [CLI: `kq detector`](cli-reference.md#kq-detector-teach-a-new-failure-in-plain-english)

---

## Failure patterns it recognizes

KubeIntellect ships **23 built-in playbooks** — deterministic investigation
recipes for the most common Kubernetes failures. When the cluster snapshot
matches a pattern, the matching playbook guides the investigation automatically.
You don't invoke these by name; they fire on their own.

| Area | Patterns |
|---|---|
| **Container lifecycle** | CrashLoopBackOff · OOMKilled · ImagePullBackOff / ErrImagePull · CreateContainerConfigError · ContainerCreating stuck · Init-container failing · Readiness/liveness probe failing · hardcoded-command failure |
| **Node & scheduling** | Pending (insufficient CPU/memory) · Pending (taints / affinity / nodeSelector) · ResourceQuota exceeded · Node NotReady · Evicted (node pressure) |
| **Lifecycle / jobs** | Pod stuck Terminating (finalizers) · Job backoffLimit exceeded |
| **Networking** | Service has no endpoints (selector/label drift) · Service unreachable |
| **Admission** | Webhook admission rejected |

See [Agent Behaviors → Playbook library](agent-behaviors.md#playbook-library) for
how they work and how to add your own.

---

## Example queries

These all work as written. Phrasing is flexible — these are illustrative, not a
fixed command set.

### Diagnose a failure

```text
why is the checkout pod crashing?
what's wrong in the payments namespace?
the api-server service has no endpoints — why?
why is my deployment stuck rolling out?
this pod has been Pending for 10 minutes, what's blocking it?
why did the nightly job fail?
diagnose the CrashLoopBackOff in namespace prod
my pod is OOMKilled — what limit should I set?
why can't this pod pull its image?
what's causing the readiness probe to fail on the web deployment?
```

### Survey cluster health

```text
what pods are broken right now?
show me everything that isn't Running across all namespaces
are there any Warning events in the last hour?
which namespaces have unhealthy workloads?
list pods in the demo namespace
is anything pending or crash-looping?
```

### Investigate with metrics and logs

```text
what was the memory usage of the api pod over the last 6 hours?
show me error logs for the worker deployment in the last 30 minutes
which pods had the highest CPU yesterday?
were there any OOM events last night?
graph request latency for the gateway service this morning
find log lines containing "connection refused" in namespace prod
```

### Understand a deployment

```text
what image is the frontend running?
show me the rollout history of the api deployment
what helm releases are installed and at what versions?
what are the resource requests and limits on the worker pods?
which config map does the checkout pod mount?
```

### Make a change (approval-gated)

```text
scale the web deployment to 5 replicas
restart the payments deployment
increase the memory limit on the oom-killer pod to 256Mi
fix the crash-looping pod and verify it recovers
delete the failed job
apply the corrected manifest
```

Any write operation pauses for your approval before it runs — see
[Safe changes](#safe-changes-human-in-the-loop) below.

### Capacity, security & maintenance

```text
which nodes are under memory pressure?
are any pods running as root or with privileged security contexts?
which deployments have no resource limits set?
what's consuming the most CPU in the cluster?
are there pods without liveness probes?
which namespaces are close to their resource quota?
```

!!! tip "Say what time range you mean"
    "Right now / current / today" → KubeIntellect reads the **live** cluster
    state. "Last night / yesterday / last 6 hours" → it queries **Prometheus /
    Loki** for history. Being explicit about the window gets you the right tool
    and avoids surfacing already-resolved problems as if they were current.

---

## What a root-cause answer looks like

For a simple question you get a direct, plain-English answer. For a genuine
outage, KubeIntellect runs a **root-cause analysis (RCA)**: it fans out four
specialist agents (pod, metrics, logs, events) in parallel and synthesizes their
findings into a single structured conclusion with these parts:

| Part | What it tells you |
|---|---|
| **Root cause** | The one underlying reason, stated plainly. |
| **Confidence** | How sure the agent is, given the evidence it gathered. |
| **Supporting evidence** | The specific tool outputs that back the conclusion. |
| **Conflicting evidence** | Anything that points the other way (surfaced, not hidden). |
| **Reasoning** | How the evidence leads to the conclusion. |
| **Recommended fix** | A concrete, ready-to-apply remedy. |

For multi-step investigations the agent also emits a visible
**investigation plan** up front (rendered as a checklist in `kq`), so you can see
what it intends to check before it checks it. See
[Agent Behaviors](agent-behaviors.md) for the full investigation flow.

---

## Safe changes (human-in-the-loop)

KubeIntellect can fix problems, not just describe them — but **every write
operation stops for approval first**. When the agent wants to run a mutating
command (`scale`, `patch`, `apply`, `delete`, `rollout`, …), it shows you the
exact command and waits:

```text
🟡 Approval Required — risk level: MEDIUM

Command:
  kubectl scale deployment/web --replicas=5 -n prod

Type `yes` or `/approve` to proceed, or `no` / `/deny` to cancel.
```

After a change is applied, the agent re-checks the resource and reports the actual
post-fix state ("Pod is now Running — verified"). A small set of cascading-blast
actions (`delete namespace`, `delete pv`, `delete crd`, `set image/resources`,
`drain`) **always** require confirmation, even in auto-approve mode. Full rules:
[Agent Behaviors → safety rules](agent-behaviors.md#always-on-safety-rules-in-the-coordinator-prompt)
and [Security](security.md).

---

## What it will not do

- **It won't read Secrets or ServiceAccounts.** These are blocked at the tool
  layer for every role — the agent cannot exfiltrate credentials.
- **It won't touch protected namespaces.** `kube-system`, `monitoring`,
  `kubeintellect`, and others are off-limits for writes by default.
- **It won't act beyond your role.** A `readonly` key can only read; an
  `operator` key can't delete or drain. See [Security](security.md).
- **It won't silently guess on destructive actions.** Ambiguous high-risk
  operations stop and ask rather than picking a default.

---

## Try it on purpose-built broken pods

`kubeintellect init` can deploy five intentionally-broken workloads into the
`demo-rca` namespace so you can practise:

| Workload | Failure | Try asking |
|---|---|---|
| `crash-loop` | CrashLoopBackOff | *"why is crash-loop crashing and how do I fix it?"* |
| `oom-killer` | OOMKilled | *"why does oom-killer keep restarting?"* |
| `bad-image` | ImagePullBackOff | *"why can't bad-image pull its image?"* |
| `resource-hog` | Pending | *"why is resource-hog pending?"* |
| `api-server` | No endpoints | *"why does the api-server service have no endpoints?"* |

---

## Related

- [Quickstart](quickstart.md) — get a server running.
- [CLI Reference](cli-reference.md) — the `kq` query client.
- [Agent Behaviors](agent-behaviors.md) — how investigations work under the hood.
- [API Reference](api-reference.md) — drive KubeIntellect from your own code.
