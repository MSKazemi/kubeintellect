---
title: AI-Powered Kubernetes Debugging & Management – How KubeIntellect Works
description: >-
  KubeIntellect is an LLM-orchestrated agent framework for end-to-end Kubernetes
  management.
---

# AI-Powered Kubernetes Debugging & Management

Kubernetes failures are hard to debug manually — not because the data is unavailable, but because it lives in too many places at once. Logs, events, resource configs, metrics, and RBAC bindings all tell part of the story. KubeIntellect correlates them automatically, surfaces the root cause, and proposes a fix — with a dry-run diff and your approval before anything changes.

KubeIntellect is a peer-reviewed, open-source system. It covers the full Kubernetes API surface: read, write, delete, exec, access control, lifecycle, and advanced verbs.

---

## The problem with traditional Kubernetes debugging

A typical debugging session looks like this:

1. `kubectl get pods` → spot the `CrashLoopBackOff`
2. `kubectl logs pod/api-xyz` → scan for errors
3. `kubectl describe pod/api-xyz` → check events
4. `kubectl get events -n prod` → look for recent warnings
5. `kubectl top pod` → check resource usage
6. Google the error message
7. Repeat for each related service

This takes 10–30 minutes per incident, assumes you already know where to look, and still misses correlated signals across services.

---

## How KubeIntellect debugs differently

KubeIntellect replaces the manual loop with a **multi-agent AI system** that:

1. Accepts a plain-English description of the problem
2. Dispatches specialized agents **in parallel** to collect all relevant signals
3. Correlates findings across logs, metrics, events, and config
4. Returns a structured root cause — not a list of things to check
5. Proposes a fix with a server-side dry-run diff
6. Waits for your explicit approval before applying anything

### Example: diagnosing a CrashLoopBackOff

```text
> Why is my payment-api pod crashing in the prod namespace?

Analyzing logs, events, metrics, and resource config in parallel...

Root cause: OOMKilled — container hit the 256Mi memory limit.
  • Last 3 events: BackOff restarts (5m ago, 3m ago, 1m ago)
  • Peak RSS before last crash: 248Mi (96% of limit)
  • No application-level error in logs — clean exit code 137 (SIGKILL)

Recommendation: increase memory limit to 512Mi.

Dry-run diff:
  resources:
    limits:
-     memory: 256Mi
+     memory: 512Mi

Apply this change? [approve / deny]
```

---

## Agents involved in a debugging session

KubeIntellect routes your query through a **Supervisor LLM** that dispatches the right specialized agents:

| Agent | What it fetches |
|-------|----------------|
| **Logs** | Pod logs with structured error extraction |
| **Metrics** | CPU/memory usage and trends |
| **DiagnosticsOrchestrator** | Logs + Metrics + Events in parallel via LangGraph Send API |
| **Lifecycle** | Pod restarts, conditions, resource quotas |
| **RBAC** | Role bindings, service account permissions |
| **Security** | Network policies, PSA violations, privileged containers |
| **Infrastructure** | Node conditions, taints, resource pressure |
| **ConfigMapsSecrets** | ConfigMap/Secret presence (key names only — values never logged) |

For a `CrashLoopBackOff`, the Supervisor routes to DiagnosticsOrchestrator, which fans out three agents in parallel and returns a correlated summary in a single LLM call.

---

## Human-in-the-loop for every write operation

KubeIntellect never applies changes silently. Every write operation — scaling, patching, deleting, applying YAML — follows this workflow:

```
observe → diagnose → propose (with dry-run diff) → human approve → execute → verify
```

The system pauses at the **approve** step and waits. If you deny, nothing changes. This is enforced at the framework level (LangGraph `interrupt_before`), not just by prompt instruction.

---

## Common failure patterns KubeIntellect handles

KubeIntellect ships with 30 pre-seeded failure patterns injected as hints before each query. Examples:

- `OOMKilled` — memory limit too low, kernel terminates the container
- `CrashLoopBackOff` — repeated restarts due to application error, missing config, or resource exhaustion
- `Pending` pod — insufficient cluster resources, taint/toleration mismatch, or PVC not bound
- `ImagePullBackOff` — registry credentials missing or image tag doesn't exist
- `Evicted` pod — node disk pressure or memory pressure eviction
- `CreateContainerConfigError` — ConfigMap or Secret referenced in pod spec doesn't exist
- RBAC `Forbidden` — service account lacks required ClusterRole/Role binding

---

## Dynamic tool generation for missing capabilities

If KubeIntellect doesn't have a built-in tool for your query, the **CodeGenerator** agent writes one:

```text
> Show me pods sorted by restart count across all namespaces

No matching tool found in registry. Generating...

[HITL] Review generated code before registration? [approve / deny]
[approve]

Tool 'list_pods_by_restart_count' registered and running:
  prod/api-6d4f9b         14 restarts
  staging/worker-2         3 restarts
  default/scheduler-1      0 restarts
```

Generated tools are:
- Sandboxed (AST validation + exec timeout)
- SHA-256 checksummed
- Registered for reuse across sessions
- Optionally promoted to the static codebase via GitHub PR

---

## Getting started

```bash
git clone https://github.com/MSKazemi/kubeintellect
cd kubeintellect
cp .env.example .env       # add your LLM credentials
make kind-kubeintellect-clean-deploy
make port-forward-librechat  # → http://localhost:3080
```

See the [Installation guide](installation.md) for full prerequisites, Azure AKS deployment, and Helm configuration options.
