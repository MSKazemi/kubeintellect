---
description: >-
  Complete reference for the two KubeIntellect command-line tools — the
  `kubeintellect` server CLI and the `kq` query client — with every subcommand,
  flag, and a worked example.
---

# CLI Reference

KubeIntellect ships **two** command-line tools with distinct jobs:

| Tool | Package | Entry point | Job |
|---|---|---|---|
| **Server CLI** | `kubeintellect` | `kubeintellect` | Set up, configure, and run the API server on a host. |
| **Query client** | `kube-q` | `kq` | Talk to a running server in plain English (REPL or one-shot). |

A typical first run is `kubeintellect init` (which configures and launches the
server, then hands off to `kq`). After that, you just open a terminal and run
`kq`.

---

## `kubeintellect` — server CLI

Installed by `pip install kubeintellect`. Manages a single configuration file at
**`~/.kubeintellect/.env`** and the server process.

```text
kubeintellect <command> [options]

Commands:
  init                 Interactive setup wizard
  serve                Start the API server
  status               Show configuration + connectivity
  set KEY=VALUE …      Update one or more config values
  db-init              Initialize the PostgreSQL schema
  kind-setup           Create a local Kind cluster
  service <action>     Manage the systemd background service
```

Run `kubeintellect --help` or `kubeintellect <command> --help` for inline help.

### `kubeintellect init`

Interactive first-time setup. Safe to re-run — it detects existing values and
offers to reuse each one.

What it does, in order:

1. Installs `kubectl` automatically if it is missing.
2. Asks for your **LLM provider** (OpenAI or Azure OpenAI) and key.
3. If no cluster is found (`~/.kube/config` missing), offers to:
    - create a local **Kind** cluster with sample workloads,
    - install the **observability stack** (Prometheus + Grafana + Loki),
    - deploy **RCA demo scenarios** (intentionally broken pods to practise on).
4. Picks an **access level** and generates an API key:
    - `admin` (`ki-admin-…`) — full access; auto-selected for local/Kind clusters,
    - `operator` (`ki-op-…`) — create/scale/apply, no deletes or drains,
    - `readonly` (`ki-ro-…`) — queries only (recommended for production).
5. Writes a fully-commented `~/.kubeintellect/.env` and configures `kq`
   (`~/.kube-q/.env` with `KUBE_Q_URL` + `KUBE_Q_API_KEY`).
6. Resolves the database mode (PostgreSQL if reachable, otherwise SQLite).
7. Optionally installs a **systemd user service** so the server starts on login,
   then launches `kq`.

```bash
kubeintellect init
```

!!! tip "Database auto-detection"
    `init` (and `serve`) probe for PostgreSQL on `POSTGRES_HOST:POSTGRES_PORT`.
    If it is unreachable and Docker is available, you can start a managed
    `postgres:16` container; otherwise KubeIntellect falls back to **SQLite**
    (`~/.kubeintellect/kubeintellect.db`) with zero setup. In SQLite mode the
    cross-session memory/reflexion features are disabled, but all diagnostics work.

### `kubeintellect serve`

Start the FastAPI server with uvicorn. Loads `~/.kubeintellect/.env`, validates
it (warnings never block startup), resolves the database mode, then serves.

```bash
kubeintellect serve                              # 0.0.0.0:8000
kubeintellect serve --port 9000                  # custom port
kubeintellect serve --host 127.0.0.1 --reload    # localhost, auto-reload (dev)
```

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `0.0.0.0` | Bind address. |
| `--port` | `8000` | Bind port. |
| `--reload` | off | Auto-reload on code changes (development only). |

### `kubeintellect status`

Health dashboard for every component. Prints `✓` / `✗` / `-` per item with a fix
hint, and **lists your API keys** so you can copy one into `KUBE_Q_API_KEY`.

```bash
kubeintellect status
```

Checks: config file, LLM provider + key, database (SQLite/PostgreSQL
reachability), `kubectl` binary, kubeconfig + current context, auth keys per
role, Prometheus / Loki / Grafana / Langfuse reachability, and the `kq` client.

### `kubeintellect set KEY=VALUE …`

Update one or more values in `~/.kubeintellect/.env` without the wizard. If the
background service is running, it is restarted so changes take effect immediately.

```bash
kubeintellect set OPENAI_API_KEY=sk-proj-...
kubeintellect set USE_SQLITE=true                       # switch to SQLite
kubeintellect set PROMETHEUS_URL=http://localhost:9090 LOKI_URL=http://localhost:3100
```

See the [Configuration Reference](configuration.md) for every key.

### `kubeintellect db-init`

Apply the schema (`app/db/schema.sql`) to PostgreSQL. Uses `DATABASE_URL` if set,
otherwise the `POSTGRES_*` values. In SQLite mode this is a no-op (the schema is
created on first start). Database errors print an actionable fix hint
(authentication, connection refused, missing database/role, SSL).

```bash
kubeintellect db-init
```

### `kubeintellect kind-setup`

Create a local Kind cluster for testing without a real cluster — standalone (you
don't have to run `init`). Installs `kind`, `kubectl`, and `helm` if missing,
adds the nginx ingress controller, and configures host DNS so
`*.svc.cluster.local` resolves.

```bash
kubeintellect kind-setup
kubeintellect kind-setup --cluster-name dev --skip-ingress
```

| Flag | Default | Meaning |
|---|---|---|
| `--cluster-name` | `kubeintellect` | Kind cluster name. |
| `--skip-ingress` | off | Skip installing the nginx ingress controller. |

### `kubeintellect service <action>`

Manage a **systemd user service** that runs `kubeintellect serve` automatically
on login (Linux only).

```bash
kubeintellect service install      # enable + start now
kubeintellect service status       # current state
kubeintellect service logs         # tail live logs (journalctl -f)
kubeintellect service stop         # stop without uninstalling
kubeintellect service start        # start again
kubeintellect service uninstall    # remove the service entirely
```

| Action | Effect |
|---|---|
| `install` | Write the unit, `daemon-reload`, enable, and start. |
| `uninstall` | Disable, stop, and remove the unit. |
| `start` / `stop` | Control the running service. |
| `status` | `systemctl --user status kubeintellect`. |
| `logs` | `journalctl --user -u kubeintellect -f`. |

---

## `kq` — query client

Installed separately with `pip install kube-q` (or `pipx install kube-q`). It is
a thin HTTP client that streams answers from a running server — it never talks to
your cluster directly, so you can run it from anywhere that can reach the API.

```bash
kq                                   # interactive REPL
kq --query "why is the payments pod crashing?"   # one-shot
kq --url http://localhost:8000       # point at a specific server
```

| Setting | Env var | Default |
|---|---|---|
| Server URL | `KUBE_Q_URL` | `http://localhost:8000` |
| API key (Bearer token) | `KUBE_Q_API_KEY` | — |

`kubeintellect init` writes both into `~/.kube-q/.env` for you. To connect
manually:

```bash
KUBE_Q_URL=http://localhost:8000 KUBE_Q_API_KEY=<your-key> kq
```

Inside the REPL, approve a pending write operation by typing `yes` (or
`/approve`); reject it with `no` (or `/deny`). To approve every write for the
session, say `approve all` / `auto-approve`. See
[Agent Behaviors → HITL](agent-behaviors.md#always-confirm-gate-overrides-auto_approve)
for what always requires confirmation.

!!! note "`kq` is a standalone project"
    The query client lives in its own package (`kube-q`) and is versioned
    independently. This page documents how KubeIntellect uses it; run
    `kq --help` for the client's full, authoritative flag set.

---

## Where things live

| Path | Written by | Purpose |
|---|---|---|
| `~/.kubeintellect/.env` | `kubeintellect init` / `set` | Server configuration. |
| `~/.kubeintellect/kubeintellect.db` | server (SQLite mode) | Local database. |
| `~/.kubeintellect/server.log` | background launch | Server logs (non-systemd). |
| `~/.kube-q/.env` | `kubeintellect init` | `kq` URL + API key. |
| `~/.config/systemd/user/kubeintellect.service` | `service install` | systemd unit. |

---

## Related

- [Quickstart](quickstart.md) — pick an install path.
- [Configuration Reference](configuration.md) — every environment variable.
- [What you can ask](capabilities.md) — example queries and capabilities.
- [Troubleshooting](troubleshooting.md) — when something doesn't work.
