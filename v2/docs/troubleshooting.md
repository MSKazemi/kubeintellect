---
description: >-
  Fixes for common KubeIntellect problems — install, server startup, LLM keys,
  database, authentication, cluster access, observability, and agent behavior —
  in symptom → cause → fix form.
---

# Troubleshooting

Start here when something doesn't work. Most issues surface in one command:

```bash
kubeintellect status
```

It prints `✓` / `✗` / `-` for every component (config, LLM, database, kubectl,
kubeconfig, auth, Prometheus, Loki, Grafana, Langfuse, `kq`) with a fix hint for
each `✗`. Work down the list.

---

## Install

### `kq: command not found` after `pip install kube-q`

**Cause:** `pip` installed the script to a directory that isn't on your `PATH`
(commonly `~/.local/bin`).

**Fix:**

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

Or install with `pipx`, which manages the PATH for you: `pipx install kube-q`.

### `kubeintellect: command not found`

Same cause as above for the server package. Re-open your shell after
`pip install kubeintellect`, or add `~/.local/bin` to `PATH`. Verify with
`which kubeintellect`.

### `kubectl not found`

`kubeintellect init` installs `kubectl` automatically. To do it standalone, run
`kubeintellect kind-setup` (installs `kubectl`, `kind`, `helm`) or install
`kubectl` manually:

```bash
curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl && sudo mv kubectl /usr/local/bin/kubectl
# macOS: brew install kubectl
```

---

## Server won't start or behaves oddly

### "Configuration issues" on `serve`

The server validates config at startup and prints warnings, but **still starts**.
If the agent then misbehaves (no answers, auth errors, empty results), resolve the
reported issues: run `kubeintellect status`, then `kubeintellect init` or
`kubeintellect set KEY=VALUE` to fix them.

### `uvicorn not found`

The server package isn't fully installed in the active environment:

```bash
pip install --upgrade kubeintellect
```

### Port 8000 already in use

Another process (or a previous server) holds the port. Use a different one:

```bash
kubeintellect serve --port 9000
# then point the client at it:
kq --url http://localhost:9000
```

---

## LLM provider

### "LLM_PROVIDER=openai but OPENAI_API_KEY is not set"

Set the key for the provider you chose:

```bash
kubeintellect set OPENAI_API_KEY=sk-proj-...
# or for Azure:
kubeintellect set AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
```

### Azure: 404 / "deployment does not exist"

The **deployment name** in `AZURE_COORDINATOR_DEPLOYMENT` /
`AZURE_SUBAGENT_DEPLOYMENT` must match the name you gave the model in Azure AI
Foundry — not the model id. Check Azure AI Foundry → Deployments.

### Azure: 400 "context length" errors

KubeIntellect already trims session history (last 20 messages) and tool output
(2,000 chars/message) to prevent this. If you still see it, you're likely on a
small-context deployment — use a `gpt-4o`-class deployment for the coordinator.

### `AZURE_OPENAI_ENDPOINT must start with https://`

Use the full resource URL, including the scheme and trailing slash:

```bash
kubeintellect set AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com/
```

---

## Database

KubeIntellect auto-detects the database: PostgreSQL if reachable, otherwise it
falls back to **SQLite** with no setup. Most database errors only matter if you
explicitly want PostgreSQL (required for cross-session memory and reflexion).

### "password authentication failed"

`POSTGRES_PASSWORD` doesn't match the database user. Fix it and re-init:

```bash
kubeintellect set POSTGRES_PASSWORD=<correct-password>
kubeintellect db-init
```

### "connection refused" / "could not connect"

Postgres isn't running or isn't reachable at `POSTGRES_HOST:POSTGRES_PORT`. Start
one, or switch to SQLite:

```bash
# Option A — run postgres in Docker:
docker run -d --name ki-pg -e POSTGRES_USER=kubeintellect \
  -e POSTGRES_PASSWORD=<pw> -e POSTGRES_DB=kubeintellect -p 5432:5432 postgres:16

# Option B — use SQLite (no setup):
kubeintellect set USE_SQLITE=true
```

### "database/role does not exist"

Create the database or role, then `kubeintellect db-init`:

```bash
createdb -h localhost -U kubeintellect kubeintellect
# or
createuser -h localhost -s kubeintellect
```

### SSL connection errors to a managed database

Append the SSL mode to the DSN:

```bash
kubeintellect set DATABASE_URL=postgresql://user:pass@host:5432/db?sslmode=require
```

---

## Authentication & connecting `kq`

### `kq` can't authenticate / 401 / 403

The client key must match a server-side key. List the server's keys and copy one:

```bash
kubeintellect status          # prints each key under "Auth"
export KUBE_Q_API_KEY=<a-listed-key>
kq
```

`kubeintellect init` writes the URL and key into `~/.kube-q/.env` automatically.

### "open access (no API keys set)"

No keys are configured, so the server treats everyone as `admin`. That's fine for
localhost, but **set keys before exposing the server**:

```bash
kubeintellect set KUBEINTELLECT_READONLY_KEYS=ki-ro-$(openssl rand -hex 20)
```

### A command was refused as "insufficient role"

Your key's role can't run that verb. `readonly` can only read; `operator` can't
`delete`/`drain`. Use an `admin` key for full access — see [Security](security.md).

---

## Cluster access

### "kubeconfig not found"

Point `KUBECONFIG_PATH` at your config, or create a local cluster:

```bash
kubeintellect set KUBECONFIG_PATH=~/.kube/config
# or, if you have no cluster:
kubeintellect kind-setup
```

In Kubernetes (Helm) deployments, leave `KUBECONFIG_PATH` empty — the pod uses its
mounted ServiceAccount token automatically.

### "Forbidden" errors from kubectl

The ServiceAccount (in-cluster) or your kubeconfig user lacks RBAC for that
resource. For Helm deployments, widen `rbac.*` in `values.yaml`
(`createClusterReadOnly`, `createClusterOps`); see [Container Image → Kubernetes
access](deploy/image.md#kubernetes-helm-serviceaccount-token).

### The agent says it can't read a Secret

That's intentional. Secrets and ServiceAccounts are blocked at the tool layer for
**every** role and cannot be unblocked through normal config — see
[Security](security.md).

---

## Observability (metrics & logs)

### "Prometheus not configured" / "Loki not configured"

These features are opt-in. Without them, every `kubectl`-based answer still works.
To enable:

```bash
kubeintellect set PROMETHEUS_URL=http://localhost:9090 LOKI_URL=http://localhost:3100
```

In Kubernetes, set `config.prometheusUrl` / `config.lokiUrl` in
`deploy/helm/kubeintellect/values.yaml` — the in-cluster services live in the
`monitoring` namespace
(`kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090`,
`loki.monitoring.svc.cluster.local:3100`).

### Prometheus/Loki show as "unreachable" in `status`

The URL is set but the endpoint didn't respond. Check it's running and reachable
from the server host (not just your laptop), and that the port is correct.

---

## Agent behavior

### It answered from a stale snapshot

KubeIntellect biases toward its pre-fetched cluster snapshot for healthy,
list-shaped questions, but always re-fetches for named resources, time-ranged
questions, or after a mutation. Add "right now" to force fresh data, or set
`SNAPSHOT_SUFFICIENCY_MODE=off` to disable the bias entirely. See
[Agent Behaviors → Snapshot sufficiency gate](agent-behaviors.md#snapshot-sufficiency-gate).

### A write operation keeps asking for approval

That's the human-in-the-loop gate working as designed. Approve with `yes` /
`/approve`. For trusted automation, set `auto_approve: true` in the API request —
but cascading-blast actions (`delete namespace`, `delete pv/crd`,
`set image/resources`, `drain`) always require explicit confirmation.

### Reflexion isn't recording or recalling patterns

Reflexion (cross-session learning) needs **PostgreSQL** — it is silently disabled
in SQLite mode. Confirm `kubeintellect status` shows `DB: ✓ postgres`, that
`REFLEXION_ENABLED` is not turned off, and note that patterns are only promoted
after a verified, high-confidence fix. See [Reflexion Subsystem](reflexion.md).

---

## Still stuck?

- Re-run `kubeintellect status` and resolve every `✗`.
- Check server logs: `kubeintellect service logs` (systemd) or
  `~/.kubeintellect/server.log`.
- Set `LOG_LEVEL=DEBUG` and `LOG_FORMAT=json` for verbose, structured logs.
- Open an issue: <https://github.com/MSKazemi/kubeintellect/issues>.
