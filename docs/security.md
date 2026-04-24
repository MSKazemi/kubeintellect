---
description: >-
  KubeIntellect security model: HITL approval gates, three-tier RBAC, shell injection blocking, kubectl resource blocklists, and REPL sandbox.
---

# KubeIntellect V2 — Security Model

---

## Table of Contents

1. [API authentication](#1-api-authentication)
2. [Role capabilities](#2-role-capabilities)
3. [Kubernetes RBAC tiers](#3-kubernetes-rbac-tiers)
4. [HITL — Human-in-the-Loop gate](#4-hitl-human-in-the-loop-gate)
5. [Secret protection — why users can't steal the API key](#5-secret-protection-why-users-cant-steal-the-api-key)
6. [Shell injection prevention](#6-shell-injection-prevention)
7. [Secret hygiene checklist](#7-secret-hygiene-checklist)

---

## 1. API authentication

Auth is controlled by three env vars. Leave all three empty to disable auth (useful for local dev or trusted networks).

```bash
KUBEINTELLECT_ADMIN_KEYS=ki-admin-abc123,ki-admin-def456
KUBEINTELLECT_OPERATOR_KEYS=ki-op-xyz789
KUBEINTELLECT_READONLY_KEYS=ki-ro-qwerty
```

- Comma-separated — multiple keys per role are supported (useful for rotating keys without downtime).
- Keys are passed as HTTP Bearer tokens: `Authorization: Bearer ki-admin-abc123`
- The role is resolved once per request in `app/api/v1/auth.py` and injected into the LangGraph config as `user_role`.

Generate a key: `openssl rand -hex 20`

---

## 2. Role capabilities

| Operation | admin | operator | readonly |
|---|---|---|---|
| `kubectl get`, `describe`, `logs`, `top`, `events` | ✅ | ✅ | ✅ |
| `kubectl apply`, `scale`, `patch`, `create`, `run`, `exec` | ✅ HITL | ✅ HITL | ❌ Blocked |
| `kubectl delete`, `drain`, `replace`, `taint` | ✅ HITL | ❌ Blocked | ❌ Blocked |
| Prometheus / Loki queries | ✅ | ✅ | ✅ |
| Receive HITL approval prompts | ✅ | ✅ (medium risk only) | ❌ |

**HITL = Human-in-the-Loop.** Even admin users cannot execute destructive commands without explicitly typing `yes` or `/approve` in the same session.

---

## 3. Kubernetes RBAC tiers

The app's ServiceAccount (`kubeintellect-sa`) has exactly the permissions listed below — nothing more. Users interact via the API; they never have direct cluster access.

### cluster-ro (always enabled)

Read-only cluster-wide: `get`, `list`, `watch` on pods, nodes, services, configmaps, events, deployments, statefulsets, daemonsets, ingresses, RBAC resources, batch jobs, metrics.

**Deliberately excluded:** `secrets` — users cannot run `kubectl get secrets` through KubeIntellect to read API keys or credentials.

### cluster-ops (enabled via `rbac.createClusterOps: true`)

Write operations, all HITL-gated: pod delete, configmap CRUD, deployment/statefulset/daemonset patch/update/delete, scale, service/PVC CRUD, job/cronjob create/delete, HPA CRUD, ingress CRUD.

### cluster-exec (enabled via `rbac.allowExec: true`, default **false**)

`pods/exec` — kubectl exec into pods. **Off by default in all non-dev environments.**

Why it's separate: a user with exec access could `kubectl exec -it <app-pod> -- env` and read the Azure OpenAI API key from the container's environment. Keeping this off in production is the single most important secret-protection control.

```yaml
# values-local.yaml  (dev — exec allowed, no real secrets at risk)
rbac:
  allowExec: true

# values-vm.yaml / values-aks.yaml  (prod — exec blocked)
rbac:
  allowExec: false
```

### namespace-manager (enabled via `rbac.enableNamespaceManagement: true`)

Namespace + quota + RoleBinding CRUD. Off by default. Enable only for eval/test clusters.

---

## 4. HITL — Human-in-the-Loop gate

Every destructive or write operation hits two checks before executing:

```
1. Role check (in run_kubectl)
   ├─ readonly  → "Permission Denied" returned, no HITL shown
   ├─ operator + high-risk verb → "Permission Denied" returned, no HITL shown
   └─ admin / operator + allowed verb → continue

2. Risk classification + interrupt()
   ├─ high-risk (delete, drain, replace, taint):
   │    interrupt() called → graph pauses → user sees approval prompt
   └─ medium-risk (patch, apply, scale, exec, create, run, …):
        interrupt() called → graph pauses → user sees approval prompt

User response in same session (X-Session-ID header):
  "yes" / "approve" / "/approve"  → Command(resume=True)  → command executes
  "no"  / "deny"    / "/deny"     → Command(resume=False) → "Action cancelled by user."
  anything else                   → treated as denial
```

The graph is frozen in the checkpoint store (PostgreSQL or SQLite) during the wait. No timeout — the approval can come hours later.

---

## 5. Secret protection — why users can't steal the API key

The Azure OpenAI API key lives in a Kubernetes Secret (`kubeintellect-secrets`) and is mounted into the pod as environment variables via `envFrom: secretRef`.

### Attack surface analysis

| Attack vector | Protection layer | Status |
|---|---|---|
| User asks: `get secrets in kubeintellect namespace` | **kubectl_tool blocked resources** — `secrets` is in `KUBECTL_BLOCKED_RESOURCES`; tool returns `[Protected]` before calling kubectl | ✅ Blocked in-app |
| User asks: `list all resources in kubeintellect namespace` | **kubectl_tool blocked namespaces** — `kubeintellect` is in `KUBECTL_BLOCKED_NAMESPACES`; tool rejects `-n kubeintellect` | ✅ Blocked in-app |
| User asks: `get secrets in monitoring namespace` | **kubectl_tool blocked namespaces** — `monitoring` is blocked (contains Langfuse keys) | ✅ Blocked in-app |
| User asks: `kubectl get serviceaccounts` | **kubectl_tool blocked resources** — SA tokens could impersonate the app | ✅ Blocked in-app |
| `kubectl exec` into pod → `env` | **`rbac.allowExec: false`** in prod → Kubernetes API server rejects the exec call | ✅ Blocked by RBAC |
| SSH into VM → read `.env` | `.env` owned by deploy user, `chmod 600` | ✅ Protected by OS |
| Shell history on VM leaking keys | `make vm-deploy` sources `.env` internally — key never appears in the shell command string | ✅ Not in history |

### How the kubectl blocklist works

`app/tools/kubectl_tool.py` runs two checks **before** calling kubectl and before showing any HITL prompt:

```
User query → coordinator → run_kubectl("kubectl get secrets -n kubeintellect")
                                │
                                ▼ _check_protected_access()
                           resource = "secrets"  → in KUBECTL_BLOCKED_RESOURCES?
                                │                   YES → return "[Protected]..." immediately
                                │                   kubectl never called
                                ▼
                           namespace = "kubeintellect" → in KUBECTL_BLOCKED_NAMESPACES?
                                │                        YES → return "[Protected]..."
                                │
                                ▼  (only reaches here if both checks pass)
                           role check → HITL → subprocess kubectl
```

The blocklists are configured in `app/core/config.py` and can be overridden per-deployment via env vars:

```bash
# Default — protects all infrastructure namespaces
KUBECTL_BLOCKED_NAMESPACES=kubeintellect,monitoring,kube-system,kube-public,kube-node-lease,ingress-nginx,cert-manager

# Default — protects secrets and SA tokens
KUBECTL_BLOCKED_RESOURCES=secret,secrets,serviceaccount,serviceaccounts
```

**Why this is the right layer:** RBAC controls what the ServiceAccount *can* do at the Kubernetes API level. The in-app blocklist controls what the *AI agent* is allowed to ask for — catching it before the API call, before HITL, and returning a clear refusal to the user's session.

### Further hardening (Azure Key Vault — Phase 2)

The above controls are sufficient for a demo / early production system. For stricter production security, move to **Azure Key Vault + Secrets Store CSI Driver**:

- Secrets are fetched at runtime by the pod using Workload Identity (federated OIDC)
- Injected as files at `/mnt/secrets/`, not as env vars
- `env` inside the pod shows no secrets
- The Kubernetes Secret object is never created

This is logged as a future roadmap item. The current model is pragmatic and secure against the realistic threat (public API users).

---

## 6. Shell injection prevention

`run_kubectl` (`app/tools/kubectl_tool.py`) has multiple layers preventing command injection:

```
Layer 1 — metacharacter guard
  Reject any command containing: ; & ` $ > < \
  Pipe (|) is allowed and handled in Python (not the shell).

Layer 2 — shlex.split (shell=False)
  subprocess is called with a list of args, never a shell string.
  The shell is never invoked. No interpolation possible.

Layer 3 — pipe emulation
  Only `grep` is supported after `|`. Any other command is rejected.
  grep is reimplemented in Python using re — no subprocess involved.

Layer 4 — YAML pre-validation
  stdin YAML is parsed with yaml.safe_load_all before being passed to kubectl.
  Malformed YAML that might confuse kubectl's parser is caught early.

Layer 5 — output cap
  Output is truncated at 8,000 characters regardless of what kubectl returns.
  Prevents memory exhaustion from pathological outputs.
```

---

## 7. Secret hygiene checklist

### Local dev

- [ ] `.env` is in `.gitignore` — never committed
- [ ] Use weak dev passwords (`changeme`) — no real keys in local `.env`
- [ ] `rbac.allowExec: true` is fine — no real secrets in the Kind cluster

### Azure VM (production)

- [ ] `chmod 600 .env` — only the deploy user can read it
- [ ] `values-vm.yaml` is in `.gitignore` — never committed
- [ ] `rbac.allowExec: false` in `values-vm.yaml` — verify before deploy
- [ ] Admin API key shared only with the cluster owner; operator/readonly keys shared with users
- [ ] Prefix `make vm-deploy` with a space to keep it out of shell history, or use `HISTCONTROL=ignorespace`
- [ ] Rotate keys: update `.env` and redeploy — old keys stop working immediately
- [ ] Langfuse admin password is a strong generated password (`openssl rand -base64 16`)

### Key rotation (zero-downtime)

```bash
# 1. Add new key to the comma-separated list in .env
KUBEINTELLECT_ADMIN_KEYS=ki-admin-old,ki-admin-new

# 2. Redeploy
make vm-deploy

# 3. Distribute new key to users, revoke old key

# 4. Remove old key from .env, redeploy again
KUBEINTELLECT_ADMIN_KEYS=ki-admin-new
make vm-deploy
```
