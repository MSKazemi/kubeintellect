---
description: >-
  Run KubeIntellect against a local Kubernetes cluster using Kind — no cloud account required. Full setup with kubeintellect init.
---

# Install: pip — Local Kind Cluster

Create a local Kubernetes cluster on your machine and connect KubeIntellect to it — no repo clone needed.

**Requirements:** Python 3.12+, Docker, an LLM API key.

---

## 1. Install

```bash
pip install kubeintellect
```

> If `kubeintellect` or `kq` are not found after install:
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
> ```

---

## 2. Set up — one command

```bash
kubeintellect init
```

Because `~/.kube/config` doesn't exist yet, the wizard offers to create a cluster:

| Prompt | Recommended answer |
|--------|-------------------|
| LLM provider | `1` OpenAI or `2` Azure OpenAI |
| API key (and endpoint for Azure) | Your key |
| Create a local Kind cluster with sample workloads? | **Y** |
| *(kind, kubectl, helm installed automatically if missing)* | |
| Install observability stack (Prometheus, Grafana, Loki)? | **Y** (optional but useful) |
| Create RCA demo scenarios? | **Y** (5 broken pods to practice root-cause analysis) |
| Database (if Docker available) | Press Enter — SQLite is the default |
| Install as background service? | **Y** — server starts on every login |

When `init` finishes it:
- Creates a 1-node Kind cluster named `kubeintellect`
- Deploys sample workloads (`demo` namespace: nginx ×2, httpbin ×1)
- Installs Prometheus + Grafana (NodePort 30090 / 30080) and Loki (NodePort 30100) — if selected
- Deploys 5 RCA practice scenarios in `demo-rca` namespace — if selected
- Configures cluster DNS so `svc.cluster.local` resolves from your host
- Writes `~/.kubeintellect/.env` with all URLs set automatically
- Configures `kube-q` (`~/.kube-q/.env`) with your API key
- Installs a systemd service (server starts on every login)
- Hands off to `kq` immediately

---

## 3. Connect

```bash
kq
```

No API key to copy — `init` configured it automatically.

---

## 4. Verify

```bash
kubeintellect status
```

Expected output (with observability):
```
  Config:    ✓  ~/.kubeintellect/.env
  LLM:       ✓  azure / gpt-4o
  DB:        ✓  sqlite  ~/.kubeintellect/kubeintellect.db
  kubectl:   ✓  found
  Kube:      ✓  ~/.kube/config  context: kind-kubeintellect
  Auth:      ✓  enabled
    admin     ki-admin-xxxxxxxxxxxxxxxxxxxx
  Prometheus:✓  http://172.18.0.2:30090  reachable
  Loki:      ✓  http://172.18.0.2:30100  reachable
  Grafana:   ✓  http://172.18.0.2:30080  reachable
  Langfuse:  -  disabled
  kube-q:    ✓  found
```

---

## Try the RCA scenarios

```bash
kq
```

Ask questions like:
- *"what pods are broken in the demo-rca namespace?"*
- *"why is crash-loop crashing and how do I fix it?"*
- *"why is resource-hog pending?"*
- *"why does the api-server service have no endpoints?"*

The 5 scenarios cover: `CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`, `Pending` (resource exhaustion), and a service with no endpoints.

---

## Managing the service

```bash
kubeintellect service status     # check if server is running
kubeintellect service logs       # tail live logs
kubeintellect service stop       # stop the server
kubeintellect service start      # start it again
kubeintellect service uninstall  # remove the service entirely
```

---

## Update a config value

```bash
kubeintellect set OPENAI_API_KEY=sk-...
kubeintellect set PROMETHEUS_URL=http://172.18.0.2:30090
```

---

## Difference from `make kind-cluster-create`

| | `kubeintellect init` (pip) | `make kind-cluster-create` (repo) |
|---|---|---|
| Requires repo clone | No | Yes |
| Cluster config | Single-node | 2-node, hot-reload mounts |
| Sample workloads | Yes (`demo` namespace) | No |
| RCA scenarios | Yes (`demo-rca` namespace) | No |
| Observability | NodePort (host-accessible) | ClusterIP (in-cluster DNS) |
| Cluster DNS auto-config | Yes — `svc.cluster.local` works from host | No |
| Who it's for | End users, ops teams | KubeIntellect developers |

For the full developer setup with hot-reload, see [deploy-kind.md](deploy-kind.md).
