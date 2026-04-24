---
description: >-
  Install KubeIntellect with pip and SQLite in minutes — no Kubernetes cluster needed. Explore the AI assistant and kube-q CLI locally.
---

# Install: pip — No Cluster (Quick Try)

Run KubeIntellect locally with SQLite and no Kubernetes cluster. Explore the API and CLI in minutes.

**Requirements:** Python 3.12+, an LLM API key.

---

## 1. Install

```bash
pip install kubeintellect
```

> **Ubuntu 22.04** ships Python 3.10. You need 3.12+:
> ```bash
> sudo add-apt-repository ppa:deadsnakes/ppa -y && sudo apt-get install -y python3.12
> python3.12 -m venv .venv && source .venv/bin/activate
> pip install kubeintellect
> ```
>
> If `kubeintellect` or `kq` are not found after install (pipx path):
> ```bash
> echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
> ```

---

## 2. Set up — one command

```bash
kubeintellect init
```

The wizard walks you through:

| Prompt | What to enter (no-cluster path) |
|--------|--------------------------------|
| LLM provider | `1` OpenAI or `2` Azure OpenAI |
| API key (and endpoint for Azure) | Your key |
| Create a local Kind cluster? | **N** — skip for now |
| Access level | `1` admin (recommended for local testing) |
| Database (if Docker available) | Press Enter — SQLite is the default |
| Install as background service? | **Y** — server starts automatically on login |

When `init` finishes it:
- Writes `~/.kubeintellect/.env`
- Configures `kube-q` automatically (`~/.kube-q/.env`)
- Installs a systemd service (Linux) so the server starts on every login
- Hands off to `kq` immediately

---

## 3. Connect

Open a new terminal (or wait for `kq` to launch automatically):

```bash
kq
```

No API key to copy — `init` configured it automatically.

---

## 4. Verify

```bash
kubeintellect status
```

Expected output (no cluster):
```
  Config:    ✓  ~/.kubeintellect/.env
  LLM:       ✓  openai / gpt-4o
  DB:        ✓  sqlite  ~/.kubeintellect/kubeintellect.db
  kubectl:   ✗  not found  → run: kubeintellect kind-setup
  Kube:      ✗  ~/.kube/config  file not found — set KUBECONFIG_PATH in ~/.kubeintellect/.env
  Auth:      ✓  enabled
    admin     ki-admin-xxxxxxxxxxxxxxxxxxxx
  Prometheus:-  not configured
  Loki:      -  not configured
  Grafana:   -  not configured
  Langfuse:  -  disabled
  kube-q:    ✓  found
```

Without `kubectl` and a cluster only informational questions work. That's fine for exploring the API.

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
kubeintellect set PROMETHEUS_URL=http://localhost:9090
```

Changes take effect immediately — if the service is running it restarts automatically.

---

## Next step

Once you have a cluster, add kubectl and connect:
- [Connect to an existing cluster →](install-pip-existing-cluster.md)
- [Create a local Kind cluster →](install-pip-kind.md)
