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

!!! note "Working from the source repo? Two Makefiles, not one"
    The CLIs above are all you need for the pip install paths. If instead you
    cloned the repo for development, the `make` targets are split by scope:

    - **Shared infrastructure — run from the repo root** (one Kind cluster +
      monitoring + Langfuse shared across versions):
      `make kind-cluster-*`, `make monitoring-install` / `monitoring-uninstall`,
      `make langfuse-provision` / `langfuse-install` / `langfuse-clean`,
      `make hosts-entry`. Use `make langfuse-provision` to auto-create one shared
      Langfuse project + API token and fan the keys into each version's `.env` —
      no more copying keys out of the Langfuse UI by hand.
    - **Per-version app targets — run from `cd v4`** (this version only):
      `make kind-build-kubeintellect`, `kind-deploy-kubeintellect`,
      `kind-redeploy-kubeintellect`, `vm-deploy-kubeintellect`,
      `aks-deploy-kubeintellect`, plus Python dev targets
      (`install` / `run` / `dev` / `test` / `lint`), `docs`, `helm-package`,
      `check-dist`, and `cli`.

    Langfuse UI login: `admin@kubeintellect.local` / `langfuse-admin`. Loki has
    no web UI — browse logs via Grafana → Explore → Loki. Reach Grafana with a
    port-forward (no ingress is exposed).

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
kq help                              # list every kq subcommand
```

`kq help` (or `kq --help`) lists all subcommands — `config`, `findings`, `digest`,
`replay`, `postmortem`, `detector`, `preference`, `v5-status` — each documented below.
A mistyped command (e.g. `kq fndings`) prints a `Did you mean: findings?` suggestion.

| Setting | Env var | Default |
|---|---|---|
| Server URL | `KUBE_Q_URL` | `https://api.kubeintellect.com` |
| API key (Bearer token) | `KUBE_Q_API_KEY` | — |

With no configuration, `kq` targets the hosted API (`https://api.kubeintellect.com`).
For a local server, `kubeintellect init` writes `KUBE_Q_URL=http://localhost:8000`
and your key into `~/.kube-q/.env`. To connect manually:

```bash
KUBE_Q_URL=http://localhost:8000 KUBE_Q_API_KEY=<your-key> kq
```

Inside the REPL, approve a pending write operation by typing `yes` (or
`/approve`); reject it with `no` (or `/deny`). To approve every write for the
session, say `approve all` / `auto-approve`. See
[Agent Behaviors → HITL](agent-behaviors.md#always-confirm-gate-overrides-auto_approve)
for what always requires confirmation.

### `kq --auto-approve`

Skip HITL approval prompts entirely — all destructive operations are executed
automatically without asking for confirmation. **Use for testing only.**

```bash
kq --auto-approve --query "restart the checkout deployment"
```

### `kq config …` — view and persist client settings

Manage the `kq` client config in `~/.kube-q/.env` without hand-editing the file.
This is the primary way to persist your server URL and API key.

| Command | Purpose |
|---|---|
| `kq config show` | Print effective config with each value's **source** (env var, `.env`, profile, or built-in default). |
| `kq config set KEY=VALUE` | Persist a value to `~/.kube-q/.env` (creates the file). E.g. `kq config set url=https://api.kubeintellect.com`. |
| `kq config reset KEY` | Remove one key from `~/.kube-q/.env`. |
| `kq config reset` | Wipe `~/.kube-q/.env` entirely (asks for confirmation). |
| `kq config profile list` | List profiles in `~/.kube-q/profiles/`. |
| `kq config profile new NAME` | Create a new profile at `~/.kube-q/profiles/NAME.env`. |
| `kq config profile show NAME` | Print a profile's contents. |
| `kq config profile delete NAME` | Delete a profile. |

Profiles let you keep per-cluster settings (a prod key, a staging URL) and are
loaded after `~/.kube-q/.env` but before a directory-local `./.env`. Inside the
REPL the same actions are available as `/config`, `/config set KEY=VAL`, and
`/config reset [KEY]`.

```bash
kq config set url=http://localhost:8000
kq config set api_key=ki-admin-xxx
kq config show
```

### `kq replay <session-id>`

Replay a recorded session from the server's **flight recorder**
([`GET /v1/episodes/{episode_id}/replay`](api-reference.md#get-v1episodesepisode_idreplay)).
Renders the stored event sequence as a table, then a chain-integrity verdict —
the flight recorder is hash-chained, so tampering with stored records is
detectable.

```bash
kq replay demo-1          # session ID == the value shown by /id in the REPL
```

| Exit code | Meaning |
|---|---|
| `0` | Replay rendered; chain intact. |
| `1` | No recorded episode with that ID, or the request failed. |
| `3` | Replay rendered, but the hash chain is **broken** — records may have been tampered with. |

### `kq findings [--limit N]`

List recent detector firings from the server
([`GET /v1/findings`](api-reference.md#get-v1findings)). Findings are produced
without any LLM calls by the detector engine watching the cluster. Prints a
table of fired-at time, playbook, namespace, object, and evidence; if the
sensorium is disabled server-side, says so and exits `0`.

```bash
kq findings               # last 100 findings
kq findings --limit 20
```

### `kq v5-status`

Show the v5 trust-plane state ([`GET /v1/v5/status`](api-reference.md#get-v1v5status)):
version identity (arm + SemVer), the active `KI_V5_*` experimental flags, and the
fail-closed write brakes — kill switch (highlighted when engaged), change freeze, and
spend cap. Zero LLM calls. With all v5 flags off it reports the v4 baseline.

```bash
kq v5-status
```

### `kq digest [--hours N]`

Render the server's digest of the last N hours as markdown
([`GET /v1/digest`](api-reference.md#get-v1digest)): detector findings,
autonomous investigations, and rollback points — what the cluster did while
you were away.

```bash
kq digest                 # last 24 hours
kq digest --hours 8
```

| Flag | Default | Meaning |
|---|---|---|
| `--hours N` | `24` | Look-back window in hours (the server caps it at `168`). |

### `kq postmortem <session-id>`

Render a grounded incident postmortem for one episode
(`GET /v1/episodes/{id}/postmortem`): a seq-cited timeline reconstructed from the
hash-chained flight recorder, what fired, what was investigated and tried, the
outcome, and an **audit-chain verdict** (intact / broken). Every line cites the
recorded event (`[#seq]`) it came from. An optional LLM narrative
(`POSTMORTEM_LLM_NARRATIVE=true`) prettifies the prose but is constrained to the
recorded events and falls back to the deterministic timeline on any failure.

```bash
kq postmortem demo-1
```

### `kq detector …` — teach a new failure in plain English

Author a detector from a natural-language description (`NL_DETECTOR_AUTHORING_ENABLED=true`).
The description is compiled into a `detect:` block, validated against the playbook
schema, and staged in **shadow** mode: it observes and accrues precision but
**never reaches the watchtower** until a human promotes it.

```bash
kq detector new "pods stuck terminating for more than 5 minutes"   # compile + stage shadow
kq detector list --status shadow                                   # the candidate queue
kq detector shadow <name>                                          # what a shadow detector has fired
kq detector promote <name>                                         # shadow → active (it can now act)
kq detector reject <name>                                          # stop it firing
```

| Subcommand | Meaning |
|---|---|
| `new "<description>"` | Compile + validate + stage as a shadow candidate. |
| `list [--status S]` | List detectors (`candidate`/`shadow`/`active`/`demoted`). |
| `shadow <name>` | Show a shadow detector's firings (review before promoting). |
| `promote <name>` | Promote shadow → active (requires operator/admin). |
| `reject <name>` | Demote a detector so it stops firing. |

### `kq preference …` — view and manage learned operator preferences

KubeIntellect remembers how *you* like to operate — **explicit** rules you set and
**behaviour-inferred** ones (e.g. your default namespace, learned from your history) —
and injects them into future sessions. This command manages the explicit ones
(`PREFERENCE_MEMORY_ENABLED=true`, default on).

```bash
kq preference set remediation dry-run-first     # explicit preference, confidence 1.0
kq preference list                              # explicit + inferred (with confidence/source)
kq preference forget remediation                # remove an explicit preference
kq preference list --user alice                 # per-operator (defaults to "default")
```

| Subcommand | Meaning |
|---|---|
| `set <key> <value> [--user U]` | Store an explicit preference (operator/admin). |
| `list [--user U]` | Show explicit + inferred preferences, ranked by confidence. |
| `forget <key> [--user U]` | Remove an explicit preference (operator/admin). |

Backed by `/v1/preferences` (GET/PUT/DELETE). See [Memory Hierarchy](memory.md#operator-preferences).

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
