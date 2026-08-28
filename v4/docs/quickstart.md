---
description: >-
  Choose your KubeIntellect install path — demo server, local Kind cluster, Docker Compose, or Helm on AKS/EKS/GKE.
---

# Quickstart

## Fastest path — a first answer in ~2 minutes

No cluster, no Docker, no LLM key of your own. Install the thin `kq` client and
point it at the hosted demo server (read-only):

```bash
# 1. Get a personal key: open https://kubeintellect.com/demo, enter your email —
#    the key appears on the page (looks like ki-ro-…). Keys last 30 days.

# 2. Install the CLI (Python 3.12+)
pip install kube-q

# 3. Save your key and start the REPL — kq already defaults to the hosted server
mkdir -p ~/.kube-q
echo "KUBE_Q_API_KEY=<your-ki-ro-key>" >> ~/.kube-q/.env
kq
```

Now ask, in plain English:

```text
You ❯ what pods are broken right now?
You ❯ why is crash-loop crashing and how do I fix it?
```

KubeIntellect investigates the shared demo cluster with real tools and answers
with a root cause and a proposed fix. Writes are disabled on the demo — to run
approval-gated fixes on your own cluster, pick a full path below.

→ Full walkthrough: **[No cluster, no Docker](install/no-cluster.md)** ·
what else you can ask: **[Capabilities](capabilities.md)**.

---

## Choose your full path

Choose based on your situation:

| I want to… | Use |
|---|---|
| Try it instantly — open a browser, no install | [Browser demo](install/no-cluster.md#try-it-in-your-browser-zero-install) at kubeintellect.com/demo — slower, read-only |
| Try it fast — no Docker, no cluster | [C1 — kube-q CLI](install/no-cluster.md#option-a-kube-q-cli) — read-only, one `pip install` |
| Try it fast — no cluster, install Docker | [C2 — install Docker + Kind](install/no-cluster.md#option-b-local-cluster) (~5 min, all features) |
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
| `kubeintellect backup-manifest` | Record what the database holds, so a restore can be proved complete |
| `kubeintellect verify-restore` | Check a restored database against a backup manifest (exits 1 on any gap) |
| `kubeintellect kind-setup` | Create a Kind cluster with DNS config (standalone, without running `init`) |
| `kubeintellect service <action>` | Manage the systemd background service (install / uninstall / start / stop / status / logs) |

---

## All config options

See [configuration.md](configuration.md) for the full variable reference and a
[ready-to-copy `~/.kubeintellect/.env` template](configuration.md#pip-install-template)
you can fill in without running the wizard.

---

## Next steps

Once your server is running:

- **[What you can ask →](capabilities.md)** — example queries and the full capability catalog.
- **[CLI Reference →](cli-reference.md)** — every `kubeintellect` and `kq` command.
- **[Troubleshooting →](troubleshooting.md)** — when something doesn't work.
