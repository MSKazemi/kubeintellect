![1777130270937](image/quickstart/1777130270937.png)---
description: >-
  Choose your KubeIntellect install path — demo server, local Kind cluster, Docker Compose, or Helm on AKS/EKS/GKE.
---

# Quickstart — Pick Your Path

Choose based on your situation:

| I want to… | Use |
|---|---|
| Try it instantly — open a browser, no install | [Browser demo](install/no-cluster.md#try-it-in-your-browser-zero-install) at kubeintellect.com/demo — slower, read-only |
| Try it fast — no Docker, no cluster | [C1 — kube-q CLI](install/no-cluster.md#option-a--kube-q-cli) — read-only, one `pip install` |
| Try it fast — no cluster, install Docker | [C2 — install Docker + Kind](install/no-cluster.md#option-b--local-cluster) (~5 min, all features) |
| Try it fast — I have a cluster and Docker | [A — Docker Compose](deploy/docker-compose.md) |
| Try it fast — I have a cluster, no Docker | [B — pip install + existing cluster](install/existing-cluster.md) |
| Have Docker, want a local Kind cluster | [D — pip install + Kind](install/kind.md) |
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

## All config options

See [configuration.md](configuration.md) for the full variable reference and a
[ready-to-copy `~/.kubeintellect/.env` template](configuration.md#pip-install-template)
you can fill in without running the wizard.
