---
description: >-
  Choose your KubeIntellect install path — pip with SQLite, Docker Compose, Kind cluster, or Helm on AKS/EKS/GKE.
---

# Quickstart — Pick Your Path

Choose based on your situation:

| I want to… | Use |
|---|---|
| Try it fast — I have a cluster and Docker | [A — Docker Compose](deploy/docker-compose.md) |
| Try it fast — I have a cluster, no Docker | [B — pip install + existing cluster](install/existing-cluster.md) |
| Try it with no cluster at all | [C — pip install + SQLite](install/no-cluster.md) |
| Create a local K8s cluster on my machine/VM | [D — pip install + Kind](install/kind.md) |
| Full local dev environment (monitoring + Langfuse) | [E — Kind from repo](deploy/kind.md) |
| Deploy to production / AKS / EKS / GKE | [F — Helm cloud](deploy/cloud.md) |

---

## CLI commands

All pip-install paths use the `kubeintellect` CLI:

| Command | Purpose |
|---------|---------|
| `kubeintellect init` | Interactive setup — installs kubectl, creates cluster (optional), configures kube-q, installs systemd service |
| `kubeintellect serve` | Start the API server manually (default: `0.0.0.0:8000`) |
| `kubeintellect status` | Show config, connectivity, and all component health |
| `kubeintellect set KEY=VALUE` | Update a config value in `~/.kubeintellect/.env` |
| `kubeintellect db-init` | Apply schema to PostgreSQL (skip for SQLite — auto-created) |
| `kubeintellect kind-setup` | Create a Kind cluster with DNS config (standalone, without running `init`) |
| `kubeintellect service <action>` | Manage the systemd background service (install / uninstall / start / stop / status / logs) |

---

## `kubeintellect init` vs `make` — what's the difference?

| | `kubeintellect init` (pip) | repo `make` path |
|---|---|---|
| Requires repo clone | No | Yes |
| Cluster setup | 1-node Kind + DNS, sample workloads, RCA scenarios | 2-node Kind, hot-reload mounts, tuned ingress |
| Observability | NodePort (host-accessible from init) | `make monitoring-install` |
| Langfuse | Not included in init | `make langfuse-install` |
| Background service | systemd user service (auto-start on login) | `make run-bg` |
| kube-q auto-config | Yes (`~/.kube-q/.env` written by init) | Manual (`KUBE_Q_URL=...`) |
| Cluster DNS auto-config | Yes — `svc.cluster.local` works from host | No |
| Who it's for | End users, ops teams | Developers working on KubeIntellect itself |

---

## All config options

See [configuration.md](configuration.md) for the full variable reference.
