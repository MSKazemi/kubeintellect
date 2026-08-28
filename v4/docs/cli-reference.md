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

**Exit code.** `init` exits **1** — printing `── Setup INCOMPLETE ──` and naming the
settings — when the file it has just written cannot run KubeIntellect: no LLM key, an
endpoint that is not an `https://` URL, a `DATABASE_URL` that is not a PostgreSQL DSN.
It stops there rather than offering to start a server that would answer no question it
is asked, or a login service that would start one every morning. The config file and
your API key are still written, so a re-run picks up where you left off. Warnings — a
missing kubeconfig before `kubeintellect kind-setup`, an unset admin key — do not fail
the setup. This is the same `error`/`warn` classification
[`kubeintellect status`](#kubeintellect-status) gates on, so the two commands cannot
disagree about the same file.

Until 2026-08-24 `init` printed `── Setup complete ──` four lines under its own
`[error] OPENAI_API_KEY is not set`, and exited `0`.

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

**Exit code.** `status` exits **1** if any row is `✗` or the configuration has an
error-level issue, and **0** otherwise, so it can gate a script or a container
healthcheck. A `-` row means *not configured* — a choice, not a fault — and never
fails. When it exits 1 the last line names what is broken:

```bash
kubeintellect status && kubeintellect serve       # only starts on a green board

$ kubeintellect status
  ...
  ✗  Not working: LLM, DB, kubeconfig
$ echo $?
1
```

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

The command also stamps a **schema version ledger** (`schema_migrations`): which version was
applied, a fingerprint of the DDL, and when. The server reads it once at startup and reports the
verdict under `db_schema` on `GET /healthz`:

| `db_schema.state` | Meaning |
|---|---|
| `current` | The database is the shape this build writes to. |
| `stale` | `schema.sql` changed since `db-init` last ran — columns this build writes to may not exist. **Run `kubeintellect db-init`.** |
| `ahead` | The database is at a *newer* schema than this build: the deployment was rolled back and the database was not. Re-running `db-init` will not restore the older shape. |
| `unrecorded` | No ledger row — a database created before the ledger existed, or one `db-init` never ran against. |
| `unknown` | The check itself could not run; see `reason`. This is not a verdict. |

This matters because every memory, recorder and audit write is fire-and-forget: a missing column
is logged and swallowed, so a stale schema does not raise — memory quietly stops recording while
the rest of `/healthz` stays green. A stale or ahead schema deliberately does **not** fail
liveness; a failing probe would turn a fixable database into a restart loop.

### `kubeintellect backup-manifest`

Measure the live database and emit a JSON manifest — schema version and DDL fingerprint, exact row
counts for every table whose loss is a data-loss event, and how far each hash chain got. Take it
beside your `pg_dump`.

```bash
kubeintellect backup-manifest --out kubeintellect-$(date +%F).manifest.json --note nightly
```

| Flag | Default | Meaning |
|---|---|---|
| `--out` | stdout | Write the manifest to this file |
| `--note` | *empty* | Free text recorded in the manifest |

### `kubeintellect verify-restore`

Re-measure this database against a manifest and report **every** discrepancy. **Exits 1** if
anything is missing, so it is safe to wire into a restore rehearsal.

```bash
kubeintellect verify-restore kubeintellect-2026-01-01.manifest.json
```

This catches the failure nothing else can see: a restore that drops the newest rows of
`decision_log` or `memory_audit` breaks no hash link, so the shortened record still verifies
intact. See [Operations → Backup & restore](operations.md#backup--restore).

### `kubeintellect chain-export`

Archive one hash-chained ledger into a **self-verifying** file: the rows verbatim, the anchor as
it stood, the link verdict computed while the database was in front of it, and a SHA-256 over all
of that. Read-only — it deletes nothing.

```bash
kubeintellect chain-export memory_audit prod-eu-1 --out memory-audit-prod-eu-1.json
kubeintellect chain-export decision_log 7f4a…  --through-seq 500 --note "before the prune"
```

| Flag | Default | Meaning |
|---|---|---|
| `--through-seq` | whole chain | Archive only up to this `seq`, inclusive |
| `--out` | stdout | Write the archive to this file |
| `--note` | *empty* | Free text recorded in the archive |

`pg_dump` gives you a *copy* of a chain; a copy of tamper-evidence that cannot itself be checked
is not evidence. This is the difference. Exporting a **broken** chain is deliberately allowed and
says so on stderr — the archive is how you keep the evidence of a break.

!!! warning "A content hash is not a signature"
    It proves the archive has not been edited since it was written. It does **not** prove who
    wrote it: anyone who can rewrite the archive can recompute the hash. Store the archive
    somewhere this database's own operators cannot silently replace it — that, not the hash
    field, is what makes it evidence.

### `kubeintellect chain-verify-export`

Check an archive. **No database needed** — that is the point of the archive.

```bash
kubeintellect chain-verify-export memory-audit-prod-eu-1.json
```

Recomputes the content hash and re-chains the rows from the `prev_hash` the archive recorded, and
reports **every** problem rather than the first. **Exits 1** if anything is wrong. An archive that
was edited, an archive whose rows do not chain, and an archive whose recorded verdict disagrees
with what its rows say now are three different messages, because they need three different
responses. Note that an archive verifies a *segment*: it does not have to start at `seq 0`, which
is why this does not reuse the whole-chain verifier.

!!! note "Retention still refuses to prune either ledger automatically"
    The scheduled pruner will not touch `decision_log` or `memory_audit`, and that has not
    changed. Removing chain rows is a deliberate, per-chain act — `chain-truncate` below.

### `kubeintellect chain-truncate`

Delete the rows an archive holds, **after** recording why the resulting gap is legitimate. The
only destructive command in this group.

```bash
kubeintellect chain-export memory_audit prod-eu-1 --through-seq 5000 --out archive.json
kubeintellect chain-verify-export archive.json     # and store it off this box first
kubeintellect chain-truncate archive.json          # dry run — prints what it would remove
kubeintellect chain-truncate archive.json --yes --note "90-day retention"
```

| Flag | Default | Meaning |
|---|---|---|
| `--yes` | *off* | Actually delete. Without it this is a dry run and nothing is written |
| `--note` | *empty* | Why these rows were removed; stored with the truncation record |

It writes a row to `chain_truncation` — chain, scope, `through_seq`, the seq the chain resumes
at, the hash it resumes from, and the archive's hash — **and then** deletes, both in one
transaction. That order is the design: interrupted halfway you get a declared gap that does not
exist yet, which verifies fine. The opposite order would leave an undeclared gap, i.e. a
permanent tamper alarm on your own housekeeping.

It refuses, before writing anything, unless every one of these holds against the live database:

- the archive verifies on its own terms and covers at least one row;
- the row at `through_seq` is still present and still carries the archive's `end_hash` — an
  archive of a chain that has since changed does not describe what would be deleted;
- rows survive past `through_seq`. Removing a chain entirely is deletion, not truncation, and the
  head anchor would report it as such forever;
- the surviving chain links to the archive: its first row's `prev_hash` **is** the archive's last
  hash. This seam is what lets the verifier resume, and it is why a forged truncation record
  cannot launder an edit — it has to be consistent with rows it does not control.

!!! warning "Requires schema v2 — re-run `kubeintellect db-init` first"
    `chain_truncation` arrived with schema version 2. On an install still on v1 the command fails
    on the INSERT (nothing is deleted, the transaction rolls back), and — more quietly — a chain
    truncated by a *newer* node would read as `unverified` on a node that cannot see the table.

!!! note "What the verifier does with it"
    A chain that still starts at `seq 0` is verified exactly as before; the record is consulted
    only when the front of a chain is missing, which without a record is still reported as
    **TAMPERED**. A record that does not match the surviving rows is TAMPERED too. A record that
    cannot be *read* is `unverified` — never `intact`.

### `kubeintellect provenance`

Print the exact commands that verify a released artifact came from this project's build — the
container image and its SBOM, the Helm chart, the `kq` binaries and the PyPI wheels — together
with the signer identity each one must pin to.

```bash
kubeintellect provenance                 # this build's own version
kubeintellect provenance --tag v2.3.1
```

| Flag | Default | Meaning |
|---|---|---|
| `--tag` | this build's version | Release tag to verify |

It prints commands; it runs none of them. The checks themselves need `gh` ≥ 2.49 (or
`pypi-attestations` for the wheels) on the machine that pulled the artifact. The commands are
generated from the same constants the publishing workflows are named by, so a renamed workflow
fails the test suite rather than silently producing a verification line that names nothing. Also
prints the channels that are deliberately **not** attested, and why. See
[Security → Supply chain](security.md#8-supply-chain-verifying-what-you-installed).

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
`replay`, `postmortem`, `export`, `detector`, `preference`, `v5-status`,
`completion` — each documented below. A mistyped command (e.g. `kq fndings`) prints a
`Did you mean: findings?` suggestion.

### `kq completion [bash|zsh|fish]`

Print a shell completion script so `<TAB>` completes `kq` subcommands, their verbs
(`config show`, `detector new`, …), and flags. Enable it once:

```bash
# bash — add to ~/.bashrc
source <(kq completion bash)
# zsh — add to ~/.zshrc (before compinit)
source <(kq completion zsh)
# fish
kq completion fish | source     # or: kq completion fish > ~/.config/fish/completions/kq.fish
```

Then `kq <TAB>` lists the commands, `kq fi<TAB>` → `findings`, and
`kq findings --<TAB>` → `--limit`.

| Exit code | Meaning |
|---|---|
| `0` | The completion script was written to stdout — or, with no argument or `--help`, the usage was printed. |
| `2` | Unsupported shell name. Nothing is written to stdout, which matters because the documented usage is `source <(kq completion bash)`: a shell that `source`s an empty stdout silently gets no completions rather than an error. |

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
rest of the current **turn**, say `approve all` / `auto-approve` — it does not carry over to
your next question; use `kq --auto-approve` for a whole session. See
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
loaded after `~/.kube-q/.env` but before a directory-local `./.env`. The full
precedence, lowest to highest, is `~/.kube-q/.env` → the active profile →
`./.env` → variables exported in your shell, and every layer really does
override the one below it. Inside the
REPL the same actions are available as `/config`, `/config set KEY=VAL`, and
`/config reset [KEY]`.

```bash
kq config set url=http://localhost:8000
kq config set api_key=ki-admin-xxx
kq config show
```

**Exit code.** `kq config show` exits **2** when any value fails validation, so it
works as a pre-flight check (`kq config show || exit 1`) in an install script or CI.
It used to print `⚠ Invalid values detected` and exit `0` — the printed answer and
the exit code disagreeing, with only the latter read by a script. `2` is the same
code the CLI already uses when a command loads config strictly and finds it invalid.

Each validation error is printed in full: the offending value, an example of a valid
one, and the env var to edit. Only the first line used to survive, which kept the
complaint and dropped the remedy.

| Exit code | Meaning |
|---|---|
| `0` | The operation succeeded. `reset` on a key that was not set, and `profile delete` on a profile that does not exist, are also `0` — the requested state is the state you have. |
| `1` | The named thing was not where it had to be: `profile show`/`profile list` on a missing profile, `profile new` on a name already taken, or the underlying file could not be removed. Nothing was changed. |
| `2` | Usage error, an unknown key, or a value that fails validation — including `show` finding an *existing* value invalid, as above. |

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
| `0` | Replay rendered; chain **verified intact** and the episode is complete. Also returned by `kq replay --help`. |
| `2` | Usage error — `kq replay` takes exactly one session ID. |
| `1` | No recorded episode with that ID, or the request failed. The server's own explanation is printed with it — this request is streamed, and until 2026-08-24 the unread body meant every server `detail` on this path was replaced by httpx's bare status line. |
| `3` | Replay rendered, but the hash chain is **broken** — records may have been tampered with. Also returned *without* a rendering when the server answers `409`: the chain anchor proves the episode had records and none survive. That is total truncation, not a missing episode, and until 2026-08-24 it exited `1` alongside a mistyped ID. |
| `4` | Replay rendered, but the chain was **not verified** — either the server's integrity verdict never arrived, or it arrived saying `chain_verified: false` because the server could not read this episode's chain anchor. Either way the records shown are unverified; until 2026-08-24 the second case exited `0` with a `✓ chain intact` line. |
| `5` | Chain intact, but the episode is **incomplete** — the recorder lost events and recorded the loss (`recorder_gap`). |

`4` exists because unverified is not the same as intact. If the verdict frame is missing or
unparseable, the command says so and fails closed rather than returning `0` — so a script that
gates on `kq replay ... && promote` cannot mistake a missing check for a passed one.

`5` exists because *intact* is not the same as *complete*. A hash chain proves no stored
record was altered; it cannot prove a record that was never stored is missing. The recorder is
fire-and-forget, so it writes its own losses into the chain
([flight recorder](flight-recorder.md#tamper-evidence)) and this command surfaces them with
the count and the cause rather than printing a clean ✓.

### `kq findings [--limit N]`

List recent detector firings from the server
([`GET /v1/findings`](api-reference.md#get-v1findings)). Findings are produced
without any LLM calls by the detector engine watching the cluster. Prints a
table of fired-at time, playbook, namespace, object, and evidence; if the
sensorium is not perceiving server-side, says **which** of the four causes it is
and exits `0`.

**`sensorium: disabled` covers four unrelated situations, so the command prints the
server's reason rather than the word.** `GET /v1/findings` returns
`sensorium_reason`, one truthful sentence per cause, and the command renders it
verbatim:

| Server situation | What `kq findings` prints |
|---|---|
| `SENSORIUM_ENABLED=false` | *switched off (SENSORIUM_ENABLED=false) — no detector finding could have been produced* |
| No compiled detectors loaded | *loaded no compiled detectors, so it did not start* |
| The sensorium **failed to start** | *FAILED to start (…) — this replica has been perceiving nothing since, and this is an outage rather than a setting* |
| Leader-election **standby** replica | *this replica is a leader-election standby and watches nothing by design … read its findings, not this replica's silence* |

**A connected sensorium can still be losing observations.** The watch queue sheds the
*oldest* observation when it overflows rather than applying backpressure to `kubectl` —
blocking there would stall the watch and cause a reconnect storm plus a full relist, i.e.
more load exactly when the system is already behind. That makes the loss silent at the
point it happens, and `queue.shed_total` in
[`GET /v1/findings`](api-reference.md#get-v1findings) the only record that it happened.
When it is non-zero the command prints **Perception is lossy**, the number dropped and the
queue high-water, and it prints it *above the table too* — shedding makes an empty list not
an all-clear and a non-empty one not a complete list. A server that reports no `queue` at
all is not accused of shedding; absent is not zero.

Between them, `sensorium`, `predictive` and `queue.shed_total` are three independent ways
to be blind while looking connected, and the green *No findings* line is reachable only
when all three are clear.

The last two are why the old single line had to go: it said *"Sensorium is disabled
on this server"*, which for a crashed sensorium is false — it was never disabled —
and for a standby replica points the operator at the one replica that is *supposed*
to be silent. Against a server too old to send `sensorium_reason`, the command says
it does not know rather than naming the likeliest cause. The exit code is `0` in
every one of these cases; branch on `sensorium` / `sensorium_reason` in
[`GET /v1/findings`](api-reference.md#get-v1findings) if a script needs to react.

**It prints the green `No findings` line only when the sensorium is actually
watching.** If no watch stream is connected — disabled, starting, reconnecting
after an RBAC denial, or stopped because kubectl is missing on the server — it
says *"Sensorium is not watching … an empty result here does NOT mean the
cluster is healthy"* and lists each stream with the reason it is down. The
command still exits `0`; branch on `sensorium` in
[`GET /v1/findings`](api-reference.md#get-v1findings) if a script needs to
react.

**The same holds one instrument over.** Anticipatory detection (ADR-010) sees through
Prometheus, not through the watch stream, so the two can disagree: the sensorium can be
`active` while Prometheus is unreachable, in which case no *predicted* finding could have
fired. When the server reports `predictive: blind`, the command prints *"Predictive
detection is blind — Prometheus could not be queried"* with the reason, and downgrades the
summary line to *"N detectors watching, but predictive detection is blind — this is not an
all-clear"*. `predictive: off` (the default — `PREDICTIVE_DETECTION_ENABLED` is `false`) is
a configuration, not an outage, and stays quiet.

```bash
kq findings               # last 100 findings
kq findings --limit 20
```

| Exit code | Meaning |
|---|---|
| `0` | The server answered. **Deliberately also `0` when nothing was watching** — a degraded sensorium, blind predictive detection and a dropped watch queue are all reported in the output, not in the status. The command cannot tell you the cluster is healthy, so it does not encode a health verdict here at all; a script that needs one must read the printed reason or `GET /v1/findings`. |
| `1` | The request failed. No answer was received, which is not an empty answer. |
| `2` | Usage error — `--limit` was not an integer, or an argument was not recognised. |

### `kq v5-status`

Show the v5 trust-plane state ([`GET /v1/v5/status`](api-reference.md#get-v1v5status)):
version identity (arm + SemVer), the active `KI_V5_*` experimental flags, and the
fail-closed write brakes — kill switch (highlighted when engaged), change freeze, and
spend cap. Zero LLM calls. With all v5 flags off it reports the v4 baseline.

```bash
kq v5-status
```

If you set a flag that **no code reads**, it will not appear under `active_flags` — it is listed
in a red `set_but_unwired_flags` row instead, with `these settings have no effect`. That row is
absent when there is nothing to warn about. See
[v5 experimental flags](v5-experimental-flags.md) for which flags are in that state and why.

A third red row, `degraded_experimental_flags`, covers the case one level out: the flag **is**
read by code, but the subsystem it lives in is not running, so it changes nothing anyway. Every
`MEMORY_*` slice runs inside the memory hierarchy, so all of them appear here whenever the
hierarchy is down — including when you turned a slice on and left `MEMORY_HIERARCHY_ENABLED`
off, where there is no hierarchy for it to run inside. **These flags stay listed under
`active_flags`**, because that list is rollout identity — which arm this pod was configured as —
and must not flap when Postgres blips; the degraded row is the liveness answer. A `memory` row
below it always shows the hierarchy's state, the reason, and how many sensorium observations
were dropped — it is what makes an *empty* degraded list evidence rather than silence.

A `memory_chain` row reports the memory audit chain's last **recorded** tamper verdict and how
old it is — never a fresh check, because verifying reads every audit row for the cluster. It is
always shown when the server sends one, including when there is nothing wrong: a row that
appeared only on bad news could not be used to confirm anything. Five states, because none of
the four non-`intact` ones means *fine*:

| `memory_chain` | What it means |
|---|---|
| `intact` | A check ran and nothing contradicted the stored rows. |
| `TAMPERED` | A check ran and **found something**: the rows no longer hash to what they carry, or the chain is shorter than its own head anchor. Treat memory-derived answers as untrusted. This is the only state that makes `memory.healthy` false. |
| `unverified` | A check ran and could not conclude — the audit rows or the anchor were unreadable. **Not** a tamper signal: a detector that cried tamper whenever its own database was down would be one you learn to ignore. |
| `never-checked` | Hardening is on and no verification has completed yet. |
| `off` | `MEMORY_SECURITY_HARDENING` is off, so no chain is being written and there is nothing to verify. Not a clean bill of health. |

The age matters as much as the state: a `STALE` marker in red means the last verdict is older
than the verification interval allows for, so the verifier may have stopped — which otherwise
looks exactly like one that keeps agreeing with itself. Set the cadence with
[`MEMORY_CHAIN_VERIFY_INTERVAL_S`](configuration.md).

A second red row, `unenforceable_guard_config`, does the same for the **guard** settings: a
`KUBECTL_BLOCKED_NAMESPACES` entry that is not a legal namespace name, or an autonomy override
the parser drops. Those settings are the outermost blast-radius control, and every parser for
them discards silently — so an entry that can never match leaves you believing a namespace is
protected when nothing enforces it.

| Exit code | Meaning |
|---|---|
| `0` | The trust-plane state was read and printed. The red rows above are a report, not a failure: a kill switch that is engaged, or a flag that is set but unwired, still exits `0`. |
| `1` | The request failed, so **no** state was read. An empty flag list you never received is not an empty flag list. |
| `2` | Usage error — `kq v5-status` takes no arguments. |

### `kq digest [--hours N]`

Render the server's digest of the last N hours as markdown
([`GET /v1/digest`](api-reference.md#get-v1digest)): detector findings,
autonomous investigations, and rollback points — what the cluster did while
you were away.

```bash
kq digest                 # last 24 hours
kq digest --hours 8
```

**It says *"Quiet watch"* only when the sources were readable and something was
actually watching.** Otherwise it leads with `Digest INCOMPLETE … This is NOT a
quiet watch:` and a block naming every source that could not answer — a disabled
recorder, SQLite mode, a failed query, a disconnected watch stream, blind
predictive detection, or **a watch queue that dropped observations before any
detector saw them**. It shares those last three judgements with
[`kq findings`](#kq-findings-limit-n) — all of them come from one classifier, so the
two commands cannot disagree about whether the same window was covered. That last
reason is the one with no unhealthy field to point at: the stream is connected and
Prometheus answers, and the events were discarded in between. Exit stays `0`; branch
on `degraded` in the JSON form.

| Flag | Default | Meaning |
|---|---|---|
| `--hours N` | `24` | Look-back window in hours (the server caps it at `168`). |

If a source could not be read — recorder disabled, SQLite mode, watchtower off,
or a failed query — the output leads with **⚠️ This digest is incomplete — do not
read it as an all-clear** and names each reason, instead of reporting a quiet
watch. The command still exits `0`; branch on `degraded` in
[`GET /v1/digest`](api-reference.md#get-v1digest) JSON if a script needs to react.

| Exit code | Meaning |
|---|---|
| `0` | The digest was rendered — including an **incomplete** one. Completeness is reported in the body and in `degraded`, never in the status, so `kq digest && all-clear` is wrong by construction; read `degraded`. |
| `1` | The fetch failed and nothing was rendered. |
| `2` | Usage error — `--hours` was not a number, or an argument was not recognised. |

### `kq postmortem <session-id>`

Render a grounded incident postmortem for one episode
(`GET /v1/episodes/{id}/postmortem`): a seq-cited timeline reconstructed from the
hash-chained flight recorder, what fired, what was investigated and tried, the
outcome, and an **audit-chain verdict**. Every line cites the recorded event
(`[#seq]`) it came from. An optional LLM narrative
(`POSTMORTEM_LLM_NARRATIVE=true`) prettifies the prose but is constrained to the
recorded events and falls back to the deterministic timeline on any failure.

**The verdict has three states, not two.** *Audit chain verified intact* and
*AUDIT CHAIN BROKEN* both mean the records were read; a third banner, *AUDIT CHAIN NOT
VERIFIED*, means nothing was read — the recorder was unreachable, or the episode has no
recorded events — and is neither a statement that the records are intact nor that they
were altered, with the reason printed underneath. Until 2026-08-24 those two cases
printed the tamper warning, which is a false claim about records nobody read and trains
the reader to ignore the banner that matters. A separate **RECORD INCOMPLETE** banner
appears when the recorder lost events for the episode: intact, complete and verified are
three different claims.

```bash
kq postmortem demo-1
```

The verdict is also the **exit code**, following the same convention as `kq replay` and
`kq export` — all three render the same audit-chain verdict, so all three branch on it the
same way. Until 2026-08-24 this command exited `0` in every case, including the tamper
warning, so `kq postmortem X > report.md && attach-to-ticket` could not tell a
tamper-evident report from a broken one. The rendered markdown is written either way; the
code says what it is worth.

| Exit code | Meaning |
|---|---|
| `0` | Audit chain verified intact and the episode complete. |
| `1` | Fetch failed. |
| `2` | Usage error. |
| `3` | **Audit chain broken** — the events may have been altered or truncated. |
| `4` | **Not verified** — nothing was read, so neither intact nor altered. Also what an older server, which does not send the verdict fields, is reported as. |
| `5` | Unaltered but **incomplete** — the recorder lost events for this episode. |

### `kq export <session-id>`

Export the same grounded report as `kq postmortem`, serialized to JSON or YAML —
for archiving, attaching to a ticket, or feeding another tool. The document is a
view over the hash-chained decision log; nothing is synthesized.

```bash
kq export demo-1                              # JSON to stdout
kq export demo-1 --format yaml                # YAML to stdout
kq export demo-1 -o reports/demo-1.json       # write a file (parents created)
```

| Flag | Default | Meaning |
|---|---|---|
| `--format`, `-f` | `json` | Output format: `json` or `yaml`. |
| `--output`, `-o` | stdout | Destination file. Parent directories are created. |

Stdout stays machine-parseable — progress notes and warnings go to stderr, so
`kq export demo-1 \| jq .root_cause` works.

If the recorder holds **no events** for the session, nothing is written and the
command reports that instead of emitting an empty-but-plausible report.

| Exit code | Meaning |
|---|---|
| `0` | Exported; audit chain intact and the episode complete. |
| `1` | Fetch or write failed. |
| `2` | Usage error. |
| `3` | Exported, but the **audit chain is broken** — treat the export as untrusted (same convention as `kq replay`). |
| `4` | No recorded events for that session — nothing exported. |
| `5` | Exported and unaltered, but **incomplete** — the recorder lost events (same convention as `kq replay`). |

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

| Exit code | Meaning |
|---|---|
| `0` | The operation succeeded — for `new`, the detector was staged in shadow. |
| `1` | The request failed. |
| `2` | Usage error. |
| `3` | The detector was **rejected on its merits** and nothing changed — `new`: the description would not compile into a stageable detector; `promote`/`reject`: the server answered `409` because the predicate can never match an observation. Distinct from `1` on purpose — `1` is worth retrying and `3` never is. |

`3` matters when scripting. A description the compiler refuses comes back as a normal `200`
response carrying `staged: false` and the errors — not an HTTP failure — so the exit code is the
only machine-readable sign that no detector was created. `kq detector new … && kq detector list`
would otherwise carry on as though one existed.

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

| Exit code | Meaning |
|---|---|
| `0` | The operation succeeded. `list` with nothing remembered is also `0` — it prints *No preferences remembered*, which is an answer, not a failure. |
| `1` | The request failed. Distinguish it from the line above before concluding an operator has no preferences. |
| `2` | Usage error — an unknown subcommand, or `set`/`forget` with the wrong number of arguments. |

### `kq` plugins — add your own slash commands

Drop a Python file into `~/.kube-q/plugins/` and the REPL picks it up at start-up. Each file
calls `register()` as many times as it likes; every registered name becomes a slash command with
tab-completion.

```python
# ~/.kube-q/plugins/hello.py
from kube_q.plugins import register

@register("/hello", help="Greet the current namespace")
def hello(ctx):
    ctx.print(f"hi from {ctx.state.current_namespace or 'no namespace'}")
```

| | |
|---|---|
| Directory | `~/.kube-q/plugins/`, or wherever `KUBE_Q_PLUGIN_DIR` points. |
| Files loaded | Every `*.py` in that directory, in name order. Files starting with `_` are skipped. |
| Command names | Must start with `/` and contain only letters, digits, `-` or `_`. Registering an existing name replaces its handler. |
| List them | `/plugins` inside the REPL. |

The handler receives a `PluginContext` with `args` (everything the user typed after the command),
`state` (the live `SessionState` — conversation id, messages, current context and namespace),
`cfg` (the `ReplConfig`), `console` (the Rich console) and `print(text)` as a shorthand for
writing to it.

!!! warning "Plugins are code you are choosing to run"
    They execute in the `kq` process with full Python access — there is no sandbox. Install only
    files you trust, exactly as you would a shell function in your `.bashrc`.

A plugin that fails to import never stops the REPL from starting: the failure is printed at
start-up and repeated by `/plugins`, with the exception type and message, and nothing the failed
module registered before it crashed stays behind. A handler that raises while *running* is caught
and reported too — the REPL keeps going.

---

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
| `~/.kube-q/plugins/*.py` | you | `kq` slash-command plugins (`KUBE_Q_PLUGIN_DIR` overrides). |
| `~/.kube-q/kube-q.log` | `kq` | Client log (rotating, 5 MB × 3). |
| `~/.config/systemd/user/kubeintellect.service` | `service install` | systemd unit. |

---

## Related

- [Quickstart](quickstart.md) — pick an install path.
- [Configuration Reference](configuration.md) — every environment variable.
- [What you can ask](capabilities.md) — example queries and capabilities.
- [Troubleshooting](troubleshooting.md) — when something doesn't work.
