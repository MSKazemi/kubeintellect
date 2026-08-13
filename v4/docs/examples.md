---
description: >-
  A cookbook of real KubeIntellect query → answer walkthroughs — diagnosing
  CrashLoopBackOff and OOMKilled, surveying cluster health, investigating with
  metrics and logs, and approval-gated fixes.
---

# Examples & Cookbook

This page is a set of **worked examples** — the same plain-English questions from
[What you can ask](capabilities.md), followed all the way through to an answer. For
each scenario you get three things: the **question** you type, **what KubeIntellect
does** (which tools and subagents it reaches for, in prose), and a **representative
answer** in the shape the agent actually returns.

The transcripts below are **illustrative**, in the style of the
[terminal example on the home page](index.md#see-it-in-action). The numbers in them
(restart counts, memory figures, timestamps) are representative, not measured — your
cluster's real values appear in your real output.

Most of these scenarios map to the five intentionally-broken workloads that
`kubeintellect init` can deploy into the `demo-rca` namespace, so you can reproduce
them yourself — see [Try it on purpose-built broken pods](capabilities.md#try-it-on-purpose-built-broken-pods).

!!! tip "You don't need `kubectl` syntax"
    Every example is asked the way you'd ask a colleague. KubeIntellect picks the
    tool — `run_kubectl`, `query_prometheus` (PromQL), or `query_loki` (LogQL) — for
    you. Say the time range you mean: *"right now"* reads the live cluster; *"last
    night"* queries Prometheus / Loki. See the note in
    [Capabilities](capabilities.md#investigate-with-metrics-and-logs).

---

## 1 · CrashLoopBackOff — `crash-loop`

**You ask:**

```text
why is crash-loop crashing and how do I fix it?
```

**What KubeIntellect does.** This is a genuine failure, so the coordinator runs a
**root-cause analysis (RCA)**: it fans out four specialist subagents in parallel —
pod, metrics, logs, events. The `CrashLoopBackOff` playbook matches the snapshot and
guides the investigation. The pod-agent reads `describe` and the container's last
state; the log-agent pulls the previous container's logs (`--previous`); the
events-agent scans recent Warning events. No write is proposed unless you ask for a
fix.

**Representative answer:**

```text
  Dispatching to DiagnosticsOrchestrator …
  ├─ pod-agent    → crash-loop restarting (last exit 1, 6 restarts, back-off 40s)
  ├─ log-agent    → "config file /etc/app/config.yaml: no such file or directory"
  └─ events-agent → BackOff: restarting failed container (×6)

Root cause: crash-loop exits non-zero on start because the config file it
expects at /etc/app/config.yaml is not mounted — the container command fails
before the app comes up, and the kubelet keeps restarting it into CrashLoopBackOff.
Confidence: high.

Supporting evidence: exit code 1 on every restart; the same "no such file"
line in the previous container's logs; no config ConfigMap mounted on the pod.

Recommended fix: mount the missing ConfigMap (or correct the command/args so
the app finds its config), then let the deployment roll out and verify.
```

The answer follows the standard RCA shape — root cause, confidence, supporting
evidence, reasoning, and a recommended fix. See
[What a root-cause answer looks like](capabilities.md#what-a-root-cause-answer-looks-like).

---

## 2 · OOMKilled — `oom-killer`

**You ask:**

```text
why does oom-killer keep restarting?
```

**What KubeIntellect does.** The `OOMKilled` playbook fires. The pod-agent reads the
container's last terminated state (an OOMKill shows as exit code 137); if
`PROMETHEUS_URL` is set, the metrics-agent pulls the working-set memory series to
show the trend against the limit; the log-agent looks for an out-of-memory signature
in the application logs.

**Representative answer:**

```text
  Dispatching to DiagnosticsOrchestrator …
  ├─ pod-agent     → last state Terminated, reason OOMKilled (exit 137)
  ├─ metrics-agent → memory.working_set climbing to the 64Mi limit before each kill
  └─ log-agent     → allocator errors just before termination

Root cause: oom-killer is being OOMKilled — its working-set memory grows past
the container memory limit (64Mi), so the kernel OOM-killer terminates it and
the kubelet restarts it. Confidence: high.

Recommended fix: raise the memory limit to a level above the observed peak,
e.g. patch it to 256Mi, or reduce the workload's memory footprint.
```

If you then say *"increase the memory limit on the oom-killer pod to 256Mi"*, that
is a **write** — it pauses for approval (see [scenario 8](#8-approval-gated-fix-scale-and-patch)).

!!! note "Metrics are optional"
    Without `PROMETHEUS_URL` set, KubeIntellect still diagnoses the OOMKill from the
    pod's terminated state and events alone — the metrics-agent line simply doesn't
    appear. See [Capabilities → What it can reach](capabilities.md#what-it-can-reach).

---

## 3 · ImagePullBackOff — `bad-image`

**You ask:**

```text
why can't bad-image pull its image?
```

**What KubeIntellect does.** This is usually a **targeted** investigation, not a full
four-agent RCA: the answer lives in the pod's events. The coordinator reads the pod
`describe` and the Warning events, where the container runtime records exactly why the
pull failed (tag not found, registry unauthorized, or a typo in the image reference).

**Representative answer:**

```text
Root cause: bad-image is stuck in ImagePullBackOff because the referenced
image tag does not exist in the registry — the Failed/ErrImagePull event reads
"manifest for …:doesnotexist not found". The kubelet backs off and retries.
Confidence: high.

Supporting evidence: pod status ImagePullBackOff; Warning event
Failed — Error: ErrImagePull; no matching tag in the registry.

Recommended fix: correct the image reference to a tag that exists (or push
the intended tag), then the deployment will pull and start.
```

---

## 4 · Pending — `resource-hog`

**You ask:**

```text
why is resource-hog pending?
```

**What KubeIntellect does.** The `Pending` playbook covers two common causes —
insufficient CPU/memory, and scheduling constraints (taints / affinity /
nodeSelector). The events-agent reads the `FailedScheduling` event, whose message
states which predicate the scheduler could not satisfy; the pod-agent reads the pod's
resource **requests** so the answer can compare them against node capacity.

**Representative answer:**

```text
Root cause: resource-hog is Pending because no node can satisfy its CPU
request — it requests more CPU than any single node has allocatable, so the
scheduler reports "0/… nodes are available: Insufficient cpu". Confidence: high.

Supporting evidence: FailedScheduling event (Insufficient cpu); the pod's CPU
request exceeds every node's allocatable CPU.

Recommended fix: lower the CPU request to fit an existing node, or add capacity
(a larger node) so the scheduler can place it.
```

---

## 5 · Service has no endpoints — `api-server`

**You ask:**

```text
why does the api-server service have no endpoints?
```

**What KubeIntellect does.** The "Service has no endpoints (selector/label drift)"
playbook fires. The coordinator compares the Service's **selector** against the
**labels** on the pods that should back it, and confirms whether the target pods are
even `Ready` — an empty `Endpoints` object means the selector matches nothing (or the
matching pods aren't Ready).

**Representative answer:**

```text
Root cause: the api-server Service has no endpoints because its selector
(app=api-server) does not match any Ready pod — the backing pods carry a
different label (app=apiserver), so the endpoints controller populates nothing
and traffic to the Service has nowhere to go. Confidence: high.

Supporting evidence: Service selector app=api-server; the running pods are
labelled app=apiserver; Endpoints object is empty.

Recommended fix: align the labels — either correct the Service selector or the
pod template labels so they match — then endpoints populate automatically.
```

---

## 6 · Survey cluster health

**You ask:**

```text
what pods are broken right now across all namespaces?
```

**What KubeIntellect does.** This is a healthy, list-shaped question over live state,
so it's answered directly from the **pre-fetched cluster snapshot** — no RCA fan-out,
often no extra tool calls at all (the snapshot-sufficiency behavior biases toward the
snapshot for exactly this shape of question). Because you said *"right now"*, it reads
live cluster state rather than history.

**Representative answer:**

```text
3 pods are not Running across all namespaces:

  NAMESPACE   POD                    STATUS             RESTARTS
  demo-rca    crash-loop-…           CrashLoopBackOff   6
  demo-rca    oom-killer-…           CrashLoopBackOff   4 (OOMKilled)
  demo-rca    resource-hog-…         Pending            0

Everything else is Running. Ask "why is <pod> …?" to investigate any one of them.
```

!!! tip "Live vs history"
    *"Right now / current / today"* reads the live cluster. *"Last night / yesterday /
    last 6 hours"* queries Prometheus / Loki. Being explicit gets you the right tool
    and avoids surfacing already-resolved problems as if they were current.

---

## 7 · Investigate with metrics and logs

**You ask:**

```text
show me error logs for the worker deployment in the last 30 minutes
```

**What KubeIntellect does.** A time-ranged, log-shaped question routes to
`query_loki` (LogQL) over the window you named — here, the last 30 minutes. The
coordinator selects the worker deployment's log stream and filters for error lines.
This requires `LOKI_URL` to be set; without it, KubeIntellect says logs aren't
configured and still answers every `kubectl`-shaped question.

**Representative answer:**

```text
Queried Loki for the worker deployment, last 30m. 12 error lines, clustered
in the last 4 minutes:

  14:41  ERROR  connection refused: postgres:5432
  14:42  ERROR  connection refused: postgres:5432
  … (10 more, same signature)

These are all the same failure — the worker can't reach postgres:5432. That
points at the database endpoint, not the worker itself; check whether the
postgres Service has endpoints and the pod is Ready.
```

A metrics question works the same way through `query_prometheus` — e.g. *"what was
the memory usage of the api pod over the last 6 hours?"* routes to a PromQL range
query. See [Capabilities → Investigate with metrics and logs](capabilities.md#investigate-with-metrics-and-logs).

---

## 8 · Approval-gated fix (scale and patch)

Everything above is read-only. The moment you ask for a **change**, the
human-in-the-loop (HITL) gate engages: KubeIntellect shows the exact command and
waits for your approval before any mutating `kubectl` verb runs.

**You ask:**

```text
scale the web deployment to 5 replicas
```

**What KubeIntellect does.** It resolves the intent to a concrete mutating command,
classifies the risk, and **pauses** — nothing runs until you approve. On approval it
executes the command and then re-reads the resource to report the actual post-fix
state.

**Representative answer:**

```text
🟡 Approval Required — risk level: MEDIUM

Command:
  kubectl scale deployment/web --replicas=5 -n prod

Type `yes` or `/approve` to proceed, or `no` / `/deny` to cancel.
```

Approve it in the REPL by typing `yes` (or `/approve`):

```text
You ❯ yes
  ✓ scaled deployment/web to 5 replicas — 5/5 pods Ready (verified)
```

The same gate applies to a patch — e.g. *"increase the memory limit on the oom-killer
pod to 256Mi"* proposes a `kubectl patch` / `set resources` and waits. A small set of
cascading-blast actions (`delete namespace`, `delete pv`, `delete crd`, `set
image/resources`, `drain`) **always** require confirmation, even in auto-approve mode.

!!! warning "`kq --auto-approve` is for testing only"
    `kq --auto-approve` skips the HITL prompts entirely — all destructive operations
    run without asking. Use it only in test environments. See
    [CLI Reference → `kq --auto-approve`](cli-reference.md#kq-auto-approve).

To approve every write for the rest of a session, say `approve all` / `auto-approve`
in the REPL. Full rules:
[Capabilities → Safe changes](capabilities.md#safe-changes-human-in-the-loop) and
[Autonomous Operations](autonomy.md), which govern how far the agent may go *without*
you when it opens its own investigations.

---

## 9 · A fix it finds on its own (autonomous)

You don't have to ask at all. With the watchtower on (the default,
`AUTONOMY_LEVEL=A1`), a compiled detector that fires on the live watch stream opens
its **own** investigation through the same tools and the same HITL gates, then
publishes the report to the findings feed and the morning digest — for zero LLM
tokens on the detection itself.

```bash
kq findings                 # recent detector firings (zero-token)
kq digest --hours 8         # what the watchtower did while you were away
```

At the default A1 level the watchtower **investigates and reports only** — it never
mutates the cluster. Auto-fix (A3) is opt-in, allowlist-gated, and post-verified. The
full ladder (A0–A3), per-namespace levels, and the A3 allowlist are in
[Autonomous Operations](autonomy.md).

---


## 10 · Config — `kq config`

You want to inspect or change where `kq` points without hand-editing `~/.kube-q/.env`.
## 10 · Replay — `kq replay`

You need to audit a prior session and verify its hash-chained decision log is intact.
## 10 · Postmortem — `kq postmortem`

You need a grounded, seq-cited postmortem to paste into an incident ticket.
## 10 · Export — `kq export`

You need a machine-readable diagnosis report for archiving or feeding another tool.
## 10 · Detector — `kq detector`

You want to teach KubeIntellect a new failure pattern in plain English.

**You run:**

```bash
kq config show
```

**Real output** — produced by running `kq config show` (kube-q 1.5.0):

```console
                 kube-q config  (/home/hariom/.kube-q/.env)                  
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ Key                             ┃ Value                         ┃ Source  ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ KUBE_Q_URL                      │ https://api.kubeintellect.com │ default │
│ KUBE_Q_API_KEY                  │                               │ default │
│ KUBE_Q_TIMEOUT                  │ 120.0                         │ default │
│ KUBE_Q_HEALTH_TIMEOUT           │ 5.0                           │ default │
│ KUBE_Q_NAMESPACE_TIMEOUT        │ 3.0                           │ default │
│ KUBE_Q_STARTUP_RETRY_TIMEOUT    │ 0                             │ default │
│ KUBE_Q_STARTUP_RETRY_INTERVAL   │ 5                             │ default │
│ KUBE_Q_STREAM                   │ True                          │ default │
│ KUBE_Q_OUTPUT                   │ rich                          │ default │
│ KUBE_Q_LOG_LEVEL                │ INFO                          │ default │
│ KUBE_Q_SKIP_HEALTH_CHECK        │ False                         │ default │
│ KUBE_Q_MODEL                    │ kubeintellect-v2              │ default │
│ KUBE_Q_USER_NAME                │ You                           │ default │
│ KUBE_Q_AGENT_NAME               │ kube-q                        │ default │
│ KUBE_Q_COST_PER_1K_PROMPT       │                               │ default │
│ KUBE_Q_COST_PER_1K_COMPLETION   │                               │ default │
│ KUBE_Q_LOGO                     │                               │ default │
│ KUBE_Q_TAGLINE                  │                               │ default │
│ KUBE_Q_BACKEND                  │ kube-q                        │ default │
│ KUBE_Q_OPENAI_API_KEY           │                               │ default │
│ KUBE_Q_OPENAI_ENDPOINT          │ https://api.openai.com        │ default │
│ KUBE_Q_OPENAI_MODEL             │ gpt-4o-mini                   │ default │
│ KUBE_Q_AZURE_OPENAI_API_KEY     │                               │ default │
│ KUBE_Q_AZURE_OPENAI_ENDPOINT    │                               │ default │
│ KUBE_Q_AZURE_OPENAI_DEPLOYMENT  │                               │ default │
│ KUBE_Q_AZURE_OPENAI_API_VERSION │ 2024-06-01                    │ default │
│ KUBE_Q_CONTEXT                  │                               │ default │
│ KUBE_Q_PROFILE                  │                               │ default │
└─────────────────────────────────┴───────────────────────────────┴─────────┘
```

This is a zero-token local operation — it never contacts the server. The output lists the available subcommands and flags exactly as `kq --help` does, so completions and help never drift. See [CLI Reference → `kq config show` ](cli-reference.md#kq-config-showsetresetprofile) for the full flag list and examples.
kq replay --help
```

**Real output** — produced by running `kq replay --help` (kube-q 1.5.0):

```console
`kq replay <episode_id>` — replay a recorded episode from the flight recorder.

Fetches GET /v1/episodes/{id}/replay (KubeIntellect's durable, hash-chained
decision log) and renders the event sequence with a chain-integrity verdict.

Usage:
  kq replay <episode_id>          # episode_id == session_id (see /id in the REPL)
```

This is a zero-token local operation — it never contacts the server. The output lists the available subcommands and flags exactly as `kq --help` does, so completions and help never drift. See [CLI Reference → `kq replay --help` ](cli-reference.md#kq-replay) for the full flag list and examples.
kq postmortem --help
```

**Real output** — produced by running `kq postmortem --help` (kube-q 1.5.0):

```console
`kq postmortem <session-id>` — a grounded incident postmortem.

Renders the server's flight-recorder postmortem: a seq-cited timeline, what
fired, what was investigated and tried, the outcome, and an audit-chain verdict.
```

This is a zero-token local operation — it never contacts the server. The output lists the available subcommands and flags exactly as `kq --help` does, so completions and help never drift. See [CLI Reference → `kq postmortem --help` ](cli-reference.md#kq-postmortem) for the full flag list and examples.
kq export --help
```

**Real output** — produced by running `kq export --help` (kube-q 1.5.0):

```console
`kq export <session-id> [--format json|yaml] [--output PATH]` — export a diagnosis report.

Serializes the server's grounded postmortem (ADR-011) for one episode to JSON or
YAML, for archiving, attaching to a ticket, or feeding another tool.

The exported document is the *same* structure `kq postmortem` renders — a view
over the hash-chained decision_log. Nothing is synthesized here: if the recorder
has no events for the episode, this command exports nothing and says so, rather
than emitting a plausible-looking empty report.

Exit codes:
  0  exported, audit chain intact
  1  fetch or write failed
  2  usage error
  3  exported, but the audit chain is BROKEN (matches `kq replay`)
  4  no recorded events for that episode — nothing exported
```

This is a zero-token local operation — it never contacts the server. The output lists the available subcommands and flags exactly as `kq --help` does, so completions and help never drift. See [CLI Reference → `kq export --help` ](cli-reference.md#kq-export) for the full flag list and examples.
kq detector --help
```

**Real output** — produced by running `kq detector --help` (kube-q 1.5.0):

```console
`kq detector` — teach the operator a new failure pattern in plain English (ADR-012).

  kq detector new "<plain-English failure description>"   compile + stage as shadow
  kq detector list [--status shadow|active|demoted]        the candidate queue
  kq detector shadow <name>                                what a shadow detector fired
  kq detector promote <name>                               shadow -> active (it can now act)
  kq detector reject <name>                                stop it firing

New detectors enter SHADOW mode: they observe and accrue precision but never act
until you promote them.
```

This is a zero-token local operation — it never contacts the server. The output lists the available subcommands and flags exactly as `kq --help` does, so completions and help never drift. See [CLI Reference → `kq detector --help` ](cli-reference.md#kq-detector-newlistpromote) for the full flag list and examples.

---

## Related

- [What you can ask](capabilities.md) — the full capability catalog and example-query list.
- [CLI Reference](cli-reference.md) — the `kq` query client, including `--auto-approve`, `findings`, and `digest`.
- [Autonomous Operations](autonomy.md) — the watchtower, the A0–A3 autonomy ladder, and the morning digest.
- [Quickstart](quickstart.md) — get a server running so you can try these yourself.
