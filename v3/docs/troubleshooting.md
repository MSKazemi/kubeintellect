---
description: >-
  Fixes for the common KubeIntellect v3 problems — install/PATH, server startup,
  LLM provider, database, authentication, cluster access, observability, and
  agent behavior.
---

# Troubleshooting

Most problems surface in one command:

```bash
kubeintellect status
```

It prints `✓` / `✗` / `-` for every component — config file, LLM key, database,
`kubectl`, kubeconfig, auth keys, Prometheus / Loki / Grafana / Langfuse, and the
`kq` client — with a fix hint for anything wrong. Run it first. The sections
below expand on the specific errors.

---

## Install

### "kq: command not found" after `pip install kube-q`

pip installed the entry point to a directory that isn't on your `PATH` (commonly
`~/.local/bin`).

```bash
export PATH="$HOME/.local/bin:$PATH"   # add to ~/.bashrc / ~/.zshrc
# or install in an isolated environment:
pipx install kube-q
```

### "kubeintellect: command not found"

Same `PATH` issue as above, or the package isn't installed.

```bash
export PATH="$HOME/.local/bin:$PATH"
pip install --upgrade kubeintellect
```

### "kubectl not found"

KubeIntellect shells out to `kubectl`. Let it install one, or install manually:

```bash
kubeintellect kind-setup     # installs kubectl (and kind/helm) automatically
# or, Linux:
curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl && sudo mv kubectl /usr/local/bin/kubectl
# macOS:
brew install kubectl
```

---

## Server won't start or behaves oddly

### "Configuration issues" on `serve`

`kubeintellect serve` validates the config and prints issues, but **still starts**
(warnings never block startup). Errors mean the server may not function until
fixed:

```bash
kubeintellect status      # see exactly what's wrong
kubeintellect init        # re-run the wizard to fix interactively
```

### "uvicorn not found"

The server dependency is missing.

```bash
pip install --upgrade kubeintellect
```

### "Port 8000 already in use"

Another process holds the port. Use a different one, or free it:

```bash
kubeintellect serve --port 9000
# or find and stop the process:
lsof -i :8000
```

---

## LLM provider

v3 supports exactly two providers: `openai` and `azure`
(`app/core/config.py`). Any other value raises a configuration error.

### "LLM_PROVIDER=openai but OPENAI_API_KEY is not set"

```bash
kubeintellect set OPENAI_API_KEY=sk-proj-...
# for Azure:
kubeintellect set LLM_PROVIDER=azure
kubeintellect set AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
```

### "Azure: 404 / deployment does not exist"

The **deployment name** in your config must match the name in Azure AI Foundry →
Deployments — not the underlying model ID.

```bash
kubeintellect set AZURE_COORDINATOR_DEPLOYMENT=<your-gpt-4o-deployment-name>
kubeintellect set AZURE_SUBAGENT_DEPLOYMENT=<your-gpt-4o-mini-deployment-name>
```

### "AZURE_OPENAI_ENDPOINT must start with https://"

Use the full resource URL, including scheme and trailing slash:

```bash
kubeintellect set AZURE_OPENAI_ENDPOINT=https://my-resource.openai.azure.com/
```

### "authentication failed" / "invalid api key" at startup

The key is wrong or expired. The server prints an actionable hint
(`app/main.py::_startup_hint`) — regenerate the key and update it:

```bash
# OpenAI: platform.openai.com/api-keys
kubeintellect set OPENAI_API_KEY=sk-proj-...
# Azure: Portal → your OpenAI resource → Keys and Endpoint → regenerate KEY 1
kubeintellect set AZURE_OPENAI_API_KEY=...
```

### "rate limit" / 429

The LLM API quota was hit. Wait a moment and retry; check your quota on the
provider's console.

---

## Database

`kubeintellect serve` auto-detects the database: if PostgreSQL is reachable it
uses it; otherwise it falls back to **SQLite** (`~/.kubeintellect/kubeintellect.db`).
Errors below apply to PostgreSQL mode.

### "password authentication failed"

`POSTGRES_PASSWORD` doesn't match your postgres user.

```bash
kubeintellect set POSTGRES_PASSWORD=<correct-password>
kubeintellect db-init
```

### "connection refused" / "could not connect"

Postgres isn't running or is unreachable. Start it, or switch to SQLite:

```bash
docker run -d --name ki-pg \
  -e POSTGRES_USER=kubeintellect -e POSTGRES_PASSWORD=<pass> \
  -e POSTGRES_DB=kubeintellect -p 5432:5432 postgres:16
kubeintellect db-init
# or, no database setup at all:
kubeintellect set USE_SQLITE=true
```

### "database / role does not exist"

Create the missing database or role, then initialize the schema:

```bash
createdb  -h localhost -U kubeintellect kubeintellect
createuser -h localhost -s kubeintellect
kubeintellect db-init
```

### "SSL connection errors to a managed database"

Many managed Postgres services require TLS. Append `?sslmode=require`:

```bash
kubeintellect set DATABASE_URL=postgresql://user:pass@host:5432/db?sslmode=require
```

---

## Authentication & connecting kq

### "kq can't authenticate / 401"

The client key doesn't match any server key. List the server's keys and copy one:

```bash
kubeintellect status                         # prints each key per role
# then, on the client side, point kq at a matching key:
echo "KUBE_Q_API_KEY=<key>" >> ~/.kube-q/.env
# or per-invocation:
KUBE_Q_API_KEY=<key> kq
```

### "open access (no API keys set)"

No keys are configured, so every caller is treated as `admin`. Safe on localhost
only — set at least a readonly key before exposing the server:

```bash
kubeintellect set KUBEINTELLECT_READONLY_KEYS=ki-ro-$(openssl rand -hex 20)
```

### "A command was refused as insufficient role"

Your key's role lacks that permission: `readonly` can't run any write verb;
`operator` can't run high-risk verbs (delete, drain, replace, taint). Use a key
with the right role. See [Security → Role capabilities](security.md#2-role-capabilities).

---

## Cluster access

### "kubeconfig not found"

Set the path, or create a local cluster:

```bash
kubeintellect set KUBECONFIG_PATH=~/.kube/config
kubeintellect kind-setup          # if you don't have a cluster yet
```

In-cluster (Helm) deployments use the mounted ServiceAccount — `KUBECONFIG_PATH`
is ignored there.

### "Forbidden" errors from kubectl

In-cluster, the app's ServiceAccount is missing the RBAC for that operation.
Widen the `rbac.*` settings in your Helm values (see [Security](security.md)).

### "The agent says it can't read a Secret"

Intentional. `secret` and `serviceaccount` are in `KUBECTL_BLOCKED_RESOURCES`
and are refused for every verb, every namespace, and every role — this protects
cluster credentials and cannot be unblocked in-app. See
[Security → Secret protection](security.md#5-secret-protection-why-users-cant-steal-the-api-key).

---

## Observability (metrics & logs)

### "Prometheus not configured" / "Loki not configured"

Metrics and logs are opt-in. Set the endpoints:

```bash
kubeintellect set PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090
kubeintellect set LOKI_URL=http://loki.monitoring.svc.cluster.local:3100
```

kubectl-based diagnostics work without these — only PromQL/LogQL queries are
unavailable.

### "Prometheus / Loki show as unreachable in status"

The URL is set but the endpoint isn't responding. Verify it is running,
reachable from the server host, and on the right port. From a Kind setup the
`init` wizard writes NodePort URLs (`:30090` / `:30100`).

---

## Agent behavior

### "It answered from a stale snapshot"

The coordinator biases toward the pre-seeded `/snapshot.md` for healthy,
list-shaped questions. Force fresh data by phrasing the question with `now` /
`right now` / `currently`, or by asking about a specific named resource — those
always re-fetch. The snapshot is also auto-refreshed once it is older than ~30 s.

### "A write operation keeps asking for approval"

That's the HITL gate working as designed — every destructive verb pauses for your
`yes`. To skip gates for trusted automation, set `auto_approve: true` in the API
request (or say `approve all` in the session).

### "Memory isn't recording or recalling anything"

Cross-session memory requires **PostgreSQL** — in SQLite mode `read_memory` /
`write_memory` no-op silently (`app/tools/memory_tool.py`). Confirm your mode:

```bash
kubeintellect status          # look for: DB: ✓ postgres
```

See [Memory](memory.md) for how it works.

---

## Still stuck?

```bash
kubeintellect status                       # re-check every component
kubeintellect set LOG_LEVEL=DEBUG          # verbose logs
kubeintellect service logs                 # tail live logs (systemd)
tail -f ~/.kubeintellect/server.log        # tail live logs (background launch)
```

If the problem persists, open an issue at
[github.com/MSKazemi/kubeintellect](https://github.com/MSKazemi/kubeintellect)
with the output of `kubeintellect status` and the relevant log lines.

---

## Related

- [CLI Reference](cli-reference.md) — every command and flag.
- [Configuration](configuration.md) — every environment variable.
- [Security](security.md) — roles, HITL, and secret protection.
- [Operations](operations.md) — running KubeIntellect as a service.
