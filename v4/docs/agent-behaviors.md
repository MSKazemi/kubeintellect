---
description: >-
  How the KubeIntellect coordinator investigates failures — workflow nodes,
  routing decisions, error interpretation, snapshot bias, parallel discipline,
  playbooks, plans, and the safety rules baked into the coordinator prompt.
---

# Agent Behaviors

Each turn moves through a fixed LangGraph workflow before the coordinator emits
its first token, and every coordinator response is shaped by five feature-flagged
behaviors plus a set of always-on safety rules baked into the system prompt.

## Workflow nodes (per turn)

```
START → memory_loader → context_fetcher → coordinator
                                              │
        ┌─────────────────────────────────────┼─────────────────────────────┐
        ▼ TARGETED                            ▼ RCA_REQUIRED                ▼ direct
  targeted_investigator              subagent_executor × 4 (Send fan-out)  END
  (3 parallel reads,                  pod | metrics | logs | events
   back to coordinator)                       │ (fan-in)
        │                                     ▼
        └─────────► coordinator (synthesis) → END
```

| Node | What it does |
|---|---|
| `memory_loader` | Loads pinned context from Postgres (user prefs, failure hints, recent RCA, session notes). SQLite mode skips this silently. |
| `context_fetcher` | Runs `kubectl get pods --all-namespaces` + `get events --field-selector=type=Warning` in parallel. Sets `snapshot_has_issues`, `snapshot_has_warnings`, `snapshot_pod_count`, `snapshot_built_at`, `snapshot_read_failed`, `cluster_id`, and matches playbooks against the snapshot. Checks each command's exit code: a failed read sets `snapshot_read_failed` and is never parsed as cluster data. |
| `coordinator` | LLM with the four tools (`run_kubectl`, `run_helm`, `query_prometheus`, `query_loki`). Decides: direct answer, `TARGETED`, or `RCA_REQUIRED`. On synthesis turns, merges subagent findings into one `RCAResult`. |
| `targeted_investigator` | Runs three parallel `kubectl` reads (`describe pod`, `get events`, `get deployments`) for a single failing resource and appends them to the snapshot, then routes back to the coordinator for the final answer. |
| `subagent_executor` (× 4) | Domain specialist subagents — pod, metrics, logs, events. Each is a ReAct loop over the same tools, capped at 3–5 tool calls, returning a typed `AgentFinding`. |

`route_coordinator` is the only conditional edge: it returns a string, `END`, or
`list[Send]` for parallel fan-out (LangGraph's native fan-out mechanism). Fan-in
back to the coordinator happens automatically once all four `Send` branches
write into the `findings` reducer.

> **A failed tool call never serialises as a result.** `run_kubectl` returns `[kubectl exited N]`
> and `run_helm` returns `[helm exited N]`, both carrying the command's own stderr; partial output
> printed before the error is labelled *"absence from it is NOT evidence"*. Neither tool pipes,
> filters or parses stderr — that is how an unreachable cluster came to read as a clean empty
> listing, and a clean listing came to read as a protection failure.
>
> Those markers are also **read by machine**, not only by the model: the autonomy layer's health
> oracle and its transactional gate classify a tool result before deciding whether a mitigation
> worked. Adding the marker in front of kubectl's own error line is what broke that classifier on
> 2026-08-24 — it matches on line prefixes, so every failure briefly read as a normal result. A
> change to how a tool reports failure is a change to that contract.
>
> **Two read verbs are exempt, because their non-zero exit is an answer.** `kubectl diff` exits 1
> when it *finds* differences (0 = none, >1 = the tool failed) and `kubectl auth can-i` exits 1
> when the answer is `no`. Both are returned as the plain result. Without the exemption a complete
> diff arrived wrapped in a failure caveat, and `can-i` was asymmetric — a clean `yes` against a
> `no` the agent had been told not to trust, which is unusable for reasoning about its own
> permissions.

### Responder brief

With `KI_V5_ESCALATION_BRIEFS` on, an **investigate** answer gains a responder brief: a short
plan calibrated to `KI_V5_RESPONDER_LEVEL`, with explicit *escalate only if* bounds. Three things
about it are always true, so the brief can be read at a glance:

- It **always** states a confidence, and states it as the **brief's own** — what the brief writer
  thinks of the plan it just wrote, not what the root-cause analysis thinks of the diagnosis. A
  brief that could not rate itself says so; it does not fall silent. (Until 2026-08-24 the line was
  printed only when the number was non-zero, so a brief rating itself 5% carried a caveat and one
  rating itself 0% carried none.)
- When the model failed or returned an unusable brief, the heading is marked **FALLBACK** and the
  conservative default is used — escalate if unsure, never a "safe to proceed alone" signal.
- The three standing escalation conditions are appended to whatever the model produced, so they
  are present on every brief.

### Verification ladder — adversarial RCA review

With `KI_V5_VERIFY_LADDER` on, a fresh-context reviewer that sees **only** the claim and the raw
evidence — never the investigation's own reasoning — looks for statements the evidence does not
support, and its verdict is appended to the answer. The reviewer **fails open**: it can never
block or contradict an answer. Since 2026-08-24 failing open no longer means failing *silent*,
because the user's only cue that verification happened is what the block says:

- A reviewer that died, timed out or returned an unparseable verdict now renders
  **"⚠ Verification NOT PERFORMED"**. It used to render nothing at all — byte-identical to a
  clean review — so a total outage of the reviewer was indistinguishable from its approval.
- A flagged review **always** states a confidence. Printing it only when the number was non-zero
  made the block quietest at maximum alarm: `0.0` means the reviewer has *no* confidence in the
  RCA, and that was exactly the value that removed the line.
- "The reviewer stated no confidence value" is a distinct outcome from a stated `0%` — an absent
  or unparseable field is the absence of a verdict, not a verdict of zero.
- **The evidence handed to the reviewer says when it is partial.** That text is the reviewer's
  entire world — it is given the claim and this, and asked which statements the evidence does not
  support. It is bounded, and the bound used to be a silent slice: an over-budget gather ended
  mid-row, on a line that read like a complete one, with nothing saying anything had been dropped.
  A reviewer whose grounds for objecting were manufactured by the truncation would then flag a
  well-evidenced claim as unsupported. The cut is now line-aligned (unless the nearest line break
  would waste more than half the budget) and carries an explicit marker naming how many characters
  were dropped, in the same terms `run_kubectl` uses for partial tool output: absence of a fact
  from that text is not evidence that it was not observed.
- **A notice a tool already wrote survives the cortex bound, on both sides of the harness flag.**
  Tool results are bounded again before they enter cortex context. That bound was a plain
  8 000-character slice — the same budget `run_kubectl` caps itself at, and since `run_kubectl`
  appends its `[truncated: N chars omitted …]` line *after* its own cap, the slice removed that
  line on every over-cap listing, and took a `[Protected] … withheld` sentence off a filtered one
  the same way. The cut of the rows stays silent by default (making it speak is what the ADR-101
  harness flag is for); what changed on 2026-08-24 is that a sentence another layer already wrote
  is lifted out of the cut and re-attached rather than destroyed by it.
- **The cortex tiers are now told what a truncation marker means.** They were not: the gather and
  synthesis prompts carried no instruction about partial output, so the marker that survives the
  bound reached a model with no rule for it. Both now carry the same clause the coordinator does,
  from a single definition. Triage is deliberately different — it answers in strict JSON, so it is
  given the inference (`[truncated` / `[Protected]` means the context is partial; absence from it
  is unknown, not healthy; prefer `investigate`) rather than an instruction to print anything. Its
  own `snapshot[:3000]` slice was the third silent cut of this family and now announces its loss
  and keeps the withheld-namespace sentence.
- **A tool that cannot answer at all says `[unavailable]`.** The gather prompt has always told the
  model not to retry a tool that "replies that it is not configured or unavailable". Driving all
  eight such replies on 2026-08-24 showed only the two "the URL is unset" ones contained either
  word: a missing binary, a refused backend connection and an unreachable cluster — the three
  cases where a retry provably cannot succeed — carried neither, so the rule had no trigger where
  it mattered. All eight now append the marker, the prompt names it, and it is a policy line so
  the trims carry it. It stays *off* answerable failures: a missing pod or an RBAC denial is a
  fact about the cluster that a differently shaped command can still get at. One classifier
  defect surfaced on the way — kubectl's most common cluster-down message ("The connection to the
  server ... was refused") put four words between "connection" and "refused" and so matched no
  pattern at all, producing no hint and no classification.


---

## Behaviors

The KubeIntellect coordinator implements five additive behaviors that shape how
it investigates Kubernetes issues. Each is feature-flagged in
[Configuration → Agent behavior flags](configuration.md#agent-behavior-flags).

| Behavior | Default |
|----------|---------|
| [kubectl error interpreter](#kubectl-error-interpreter) | on |
| [Snapshot sufficiency gate](#snapshot-sufficiency-gate) | `lenient` |
| [Gather-then-conclude discipline](#gather-then-conclude-discipline) | always on |
| [Playbook library](#playbook-library) | on |
| [Visible investigation plan](#visible-investigation-plan) | on |

---

## kubectl error interpreter

When `kubectl` exits non-zero, the tool layer scans `stderr` for known patterns
(NotFound, Forbidden, connection refused, missing CRD, immutable field, …) and
appends a single-line hint after the original error. The LLM sees both — the
raw error is never replaced.

**Why.** Stops the agent from looping on errors it could have skipped. Example:

```
Error from server (NotFound): pods "payments-1" not found
→ Pod may have been rescheduled — re-run `kubectl get pods -n <ns>` to find the new name.
```

Every non-zero exit is stated outright, hint or no hint: the result begins
`[kubectl exited <code>]`, so a failure never has to be inferred from the wording of a
message the agent did not write. When `stderr` is empty the result says so
(`(kubectl wrote nothing to stderr)`) rather than reading as a blank tool result.

**A partial failure shows both halves.** `kubectl get pods -A` with one namespace
forbidden exits non-zero *and* prints the namespaces it could read. Until 2026-08-24 the
tool layer kept stdout and discarded stderr whenever stdout had anything, so that
listing reached the agent looking like a complete census with no sign a namespace had
been denied. Both are now returned, with the surviving rows explicitly marked as
possibly partial — absence from them is not evidence.

**Disable:** `KUBECTL_ERROR_HINTS_ENABLED=false` (the exit-code line and the partial-output
notice are not gated by it — they are statements of fact, not interpretation).

---

## Snapshot sufficiency gate

`context_fetcher` runs at the start of every turn and pre-fetches a cluster
snapshot (pod list + Warning events). C2 adds a soft prompt bias: when the
snapshot is healthy and the user asks a list-shaped, read-only question, the
coordinator is encouraged to answer from the snapshot without an extra
`kubectl get pods`.

**Always falls back to fresh data when:**
- The question targets a specific named pod / deployment / service.
- The user asks about logs, metrics, history, "yesterday", "last N hours".
- The coordinator just performed a mutation (verifies via fresh `get`).
- The query contains `now` / `right now` / `currently`.
- The snapshot is older than `SNAPSHOT_FRESHNESS_SECONDS` (default 30s).

**Why a soft bias, not a hard gate?** Pod state changes fast in Kubernetes; a
hard gate could return stale answers. The soft bias only fires for clean
snapshots and list-shaped questions, with explicit always-fetch escape hatches.

**When the snapshot could not be taken.** Each `kubectl` read is checked by exit
code. If either fails — expired credentials, an unreachable API server, missing
RBAC — the snapshot is marked unavailable, the sufficiency block asserts **no**
pod count and **no** health flags, and the coordinator is told to always fetch
and to surface the error rather than report an empty or healthy cluster. The
error text is still shown to the reader; what stops is *counting* it. Before
2026-08-20 the exit code was ignored and kubectl's stderr was parsed as a pod
table, so a failed read became `0 pods, issues=false, warnings=false` — with the
bias then encouraging the model to answer "how many pods" and "is the cluster
healthy" straight from it.

**Modes:** `off` (no bias — pre-C2 behavior), `lenient` (default — bias only
when truly applicable), `strict` (aggressive bias — opt in for trusted
deployments).

**Set:** `SNAPSHOT_SUFFICIENCY_MODE=off|lenient|strict`,
`SNAPSHOT_FRESHNESS_SECONDS=30`.

---

## Gather-then-conclude discipline

A prompt-only directive: when tools are needed, follow PLAN → FETCH → SYNTHESIZE.
Emit all independent tool calls in a single response (parallel), then synthesize
once. Never interleave partial answers with more tool calls.

**Exception:** sequential dependencies (e.g. find a pod's name → describe that
pod) are allowed; even then, gather everything else in parallel at each step.

This is always on — it is part of the coordinator's core system prompt.

---

## Playbook library

For the top recurring Kubernetes failure modes, KubeIntellect ships a YAML
playbook with a deterministic investigation sequence. When `context_fetcher`
detects a matching pattern in the snapshot, the coordinator's system prompt
includes the playbook(s) inline — guiding it to follow proven steps before
improvising.

**Playbooks shipped (23):**

*Pod / container lifecycle*

- `CrashLoopBackOff`
- `OOMKilled`
- `ImagePullBackOff` / `ErrImagePull`
- `CreateContainerConfigError` (missing ConfigMap / Secret refs)
- `ContainerCreatingStuck` (volume / CSI)
- `InitContainerFailing`
- `ReadinessProbeFailing` (pod held out of Service endpoints)
- `LivenessProbeFailing` (kubelet restarts a container that is alive but not answering)
- `CommandHardcodedFailure` (hardcoded `exit 1` / error in container command)
- `Evicted` (node-pressure eviction)
- `TerminatingStuck` (finalizers)

*Scheduling / capacity*

- `PendingInsufficientResources`
- `PendingSchedulingConstraints` (taints / affinity / nodeSelector)
- `QuotaExceeded` (ResourceQuota)
- `NodeNotReady`
- `PvcPending` (claim never binds — missing StorageClass or no matching PV)

*Workloads, networking & admission*

- `JobBackoffLimitExceeded`
- `ServiceNoEndpoints` (selector / label drift)
- `ServiceUnreachable`
- `NetworkPolicyBlocking` (policy drops traffic silently — no event is ever emitted)
- `DeploymentRolloutStuck` (`ProgressDeadlineExceeded` — new ReplicaSet never becomes Available)
- `WebhookAdmissionRejected`
- `HPANotScaling` (metrics-server missing/unreachable, or container missing `resources.requests.cpu`)

**Schema** (drop a YAML file into `app/agent/playbooks/`):

```yaml
name: <unique pattern name>
triggers:
  - pod_status_regex: "<regex on STATUS column>"
  - event_reason_regex: "<regex on Warning event REASON>"
  - event_message_regex: "<regex on Warning event MESSAGE>"
investigation_steps:
  - "<imperative step 1>"
  - "<imperative step 2>"
expected_evidence:
  - "<what to look for>"
recommended_fix_template: |
  <multi-line fix template; placeholders welcome>
```

A playbook matches if any of its triggers matches. The coordinator still has
agency — it can deviate when the situation warrants — but the playbook gives it
a strong default.

**Disable:** `PLAYBOOKS_ENABLED=false`.

---

## Visible investigation plan

For queries requiring three or more tool calls, the coordinator writes its plan
as the first line of the response:

```
INVESTIGATION_PLAN:
- Check pod status in default namespace
- Describe the crashing pod
- Query Loki for errors in the last 30m
- Propose a fix
```

The plan block is parsed out of the message body and emitted as a structured
`PlanEvent` on the SSE stream. UI clients (kube-q, browsers) can render it as a
checklist; Langfuse traces show it for post-mortem review.

**Why.** Makes multi-step investigations transparent and gives the agent an
anchor to stay on-track. Trivial single-call queries skip the plan (threshold:
≥ 3 steps).

**Disable:** `INVESTIGATION_PLAN_ENABLED=false`.

---

## Routing decision

Every coordinator turn produces exactly one of three routing outcomes.

| Outcome | When | What the coordinator emits |
|---|---|---|
| **direct** | Simple list / status / single-resource query, or a mutation. | A normal answer (with tool calls, possibly a plan, possibly a `TARGETED:` block). |
| **TARGETED** | One specific resource is failing and needs deeper inspection. | `TARGETED: namespace=<ns>, pod=<pod>, issue=<one-line>` on its own line. The `targeted_investigator` runs three parallel reads (describe, events, deployments), appends them to the snapshot, and the coordinator answers with the enriched context. |
| **RCA_REQUIRED** | Multi-pod / cross-namespace outage, unknown root cause, cascading failures. | The literal token `RCA_REQUIRED`. The router fans out to four specialist subagents in parallel; the coordinator then synthesizes their findings into one `RCAResult`. |

The sentinel text is parsed out of the message stream — it never reaches the
user. `TARGETED` should always be preferred over `RCA_REQUIRED` for
single-resource issues; the four-subagent fan-out is reserved for genuinely
ambiguous, cross-cutting failures.

---

## Always-on safety rules (in the coordinator prompt)

These rules are not feature-flagged — they live in the coordinator system prompt
and apply to every turn.

### Mutation batching (HITL safety)

At most **one mutation per response**. Reads (`get`, `describe`, `logs`, `top`)
may still be batched in parallel — only `patch / apply / create / delete /
scale / set / rollout` are restricted. Batching multiple mutations in one
response causes redundant approval prompts and re-queues unapproved calls when
HITL fires mid-batch. The `_fill_orphan_tool_calls` helper injects "skipped"
placeholders for any `tool_call` without a matching `ToolMessage` so the LLM
doesn't re-propose them on the next loop.

### Fix verification (after every mutation)

After every successful `kubectl patch / apply / create / delete`, the
coordinator must perform one more `kubectl get` on the affected resource and
report the actual post-fix state ("Pod is now Running (verified)"). The
[reflexion subsystem](reflexion.md) re-runs this check independently — fix
verification at the prompt level is the agent's contract; reflexion verification
is the system's gate before promoting a pattern.

### Service-endpoint cross-check (namespace-level queries)

For any namespace-level investigation (*"check ns X", "what's wrong in X",
"diagnose X"*), the coordinator must include `kubectl get endpoints -n <ns>`
**and** `kubectl get services -n <ns>` in the initial parallel batch — alongside
`get pods` and `get events`. A service whose `ENDPOINTS` column is `<none>`
while its target pods are `Running` is a silent fault: no warning event fires
for selector/label drift. This cross-check is the only reliable way to surface
it.

### Spec-before-logs for CrashLoopBackOff

When diagnosing a CrashLoop pod, the coordinator must read
`spec.containers[].command` and `spec.containers[].args` from
`kubectl describe pod` *before* inferring a root cause from log output. A log
line like `"DB not configured"` may be hardcoded in the container's `command`,
in which case no env or secret patch will fix it.

### Tool selection by time intent

| Phrasing | Tool |
|---|---|
| "Current / active issues", "now", "today" | `kubectl get pods --all-namespaces` + `describe` for Last State |
| "Last N hours/days", "yesterday", "last night" | `query_prometheus` with `range_minutes`, `query_loki` with `since=Nh` |
| "Pods with issues" (no qualifier) | `kubectl get pods --all-namespaces` — pods not in Running/Completed/Succeeded right now |

Using `range_minutes>0` for "current issues" surfaces already-resolved problems
and produces false positives.

### Shell-metacharacter constraints

The runner blocks any `kubectl` command containing `;`, `&`, `` ` ``, `$`, or
`\`. (Pipes and redirection are excluded — `|` is reimplemented in Python and
`<` / `>` are harmless under `shell=False`.) The constraint applies to the full
command string, including arguments inside `--patch '[...]'` or `-- sh -c "..."`.

For container `command` / `args` changes (which usually contain shell
metacharacters), the only reliable path is:

1. `kubectl get <kind> <name> -n <ns> -o yaml` to fetch the current spec.
2. Build the corrected manifest in the response.
3. `kubectl apply -f -` with the manifest passed via stdin.

`kubectl edit` is rejected outright — there is no interactive terminal in the
container or the pip install.

### Session-history compression

The coordinator caps message history at the last 20 messages (about 5 prior
exchanges). When the cap fires, the dropped messages are summarised
deterministically (no extra LLM call) into a *Earlier Session Context
(compressed)* block injected into the system prompt — preserving topics,
commands run, and key tool results without bloating the context window.

Tool output is also capped: `kubectl` table output keeps the header plus the
first 30 rows plus any rows matching `error|warning|failed|pending|oomkilled|
crashloop|backoff|imagepull|containercreating`; everything else is truncated
at 2 000 characters with an explicit `[truncated]` marker that the coordinator
must surface to the user.

### Proactive Fix Mode (`auto_approve=true`)

When the request body sets `auto_approve=true` (used by the evaluation harness
and trusted automation), HITL gates are bypassed and a *Proactive Fix Mode*
block is appended to the system prompt: the coordinator must apply identified
fixes immediately, choose the safest default for ambiguous parameters, verify
with a fresh `get`, and stop with an explicit "cannot determine" message rather
than guessing.

### Always-confirm gate (overrides `auto_approve`)

A small set of cascading-blast actions ALWAYS prompt for confirmation, even on
`auto_approve=true` sessions. The HITL interrupt fires with `risk_level=high`
and `always_confirm=true`; there is no way to silently auto-approve them.

| Verb pattern | Why it cannot auto-approve |
|---|---|
| `delete namespace\|ns` | Cascades to every resource in the namespace; no rollback. |
| `delete pv\|persistentvolume` | Releases user data; CSI drivers may delete the underlying disk. |
| `delete crd\|customresourcedefinition` | Cascades to every CR of that kind cluster-wide. |
| `set image \|set resources` | Live mutation of running workloads; use `rollout undo` to revert. |
| `drain` | Evicts every pod on the node; depends on PDB compliance. |

Defined in `app/tools/kubectl_tool.py` (`_ALWAYS_CONFIRM_DELETE_TARGETS`,
`_ALWAYS_CONFIRM_SET_SUBCOMMANDS`, `_requires_always_confirm`). Plain
`delete pod`, `apply`, `patch`, `scale` continue to auto-approve under
`auto_approve=true`.

The gate reads the target through `_operand_after_verb()`, the same parse
`_extract_verb` and `_extract_resource_type` use, so **where the flags sit does
not change what the gate sees**: `kubectl delete -n prod namespace shop`,
`kubectl -n prod delete namespace shop` and `kubectl delete --force namespace
shop` all confirm. Until 2026-08-20 it read a fixed `args[2]`, so any flag
between the verb and its target silently turned the gate off — a flag is not a
decision about blast radius.

The role layer runs *before* the always-confirm gate, so existing role
permissions still apply: `readonly` keys can't reach the gate at all,
`operator` keys are still blocked on high-risk verbs (`delete`, `drain`,
`replace`, `taint`) before HITL runs. The always-confirm gate only matters
for `admin` and `superadmin` keys (and for `operator` running `set image|
resources`, which is medium-risk but always-confirm).

---

## How they compose

A typical investigation of a CrashLoopBackOff pod, with all behaviors on:

1. **Snapshot gate** — `context_fetcher` builds the snapshot, sees the unhealthy pod, sets
   `snapshot_has_issues=true` and matches the `CrashLoopBackOff` playbook.
2. The coordinator system prompt now includes the snapshot, the
   playbook details (describe → previous logs → events), and the snapshot
   sufficiency block (which won't fire because issues are present — we always
   fetch when unhealthy).
3. **Investigation plan** — for a 3+ step query, the coordinator emits
   `INVESTIGATION_PLAN: …` first; UI shows the checklist.
4. **Parallel discipline** — coordinator emits all independent tool calls in one response.
5. **Error interpreter** — if any kubectl call returns a known error pattern, the hint is
   appended before the LLM sees it, avoiding retry loops.
6. Final answer references each plan step and proposes a fix from the
   playbook's `recommended_fix_template`.
7. **Reflexion outcome write** — if the answer ran a mutation, the
   [reflexion subsystem](reflexion.md) verifies the cluster post-fix and records
   the outcome (cluster-scoped, with cooldown).

Each phase can be flipped independently if you need to roll one back.

---

## Related: Reflexion subsystem

The five behaviors above shape *one turn*. On top of them, KubeIntellect runs a
**reflexion subsystem** that records outcomes across turns and promotes
recurring, verified patterns back into future prompts. Cluster-scoped, with
verification gates, cooldown, and retention.

See [Reflexion Subsystem](reflexion.md) for the design and operational view.

---

## V4 additions

Everything above remains accurate and is the default execution path. V4 adds a
perception layer that works *between* your questions, and an opt-in
next-generation reasoning graph.

### Playbook schema v2 — `detect:` blocks

Each playbook may now carry a `detect:` block: machine-checkable predicates
that fire **without any LLM call**. The LLM-side `triggers:` /
`investigation_steps:` / `recommended_fix_template:` keys are unchanged. From
the real `crashloopbackoff.yaml`:

```yaml
name: CrashLoopBackOff
detect:
  watch_predicates:
    - kind: Pod
      status_regex: "^CrashLoopBackOff$"
    - kind: Event
      reason_regex: "^BackOff$"
      message_regex: "Back-off restarting failed container"
      involved_kind: Pod
  promql:
    - 'kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"} == 1'
    - 'increase(kube_pod_container_status_restarts_total[10m]) > 3'
  debounce_seconds: 60
triggers:
  - pod_status_regex: "CrashLoopBackOff"
  # … (unchanged v1 keys follow)
```

- `watch_predicates` match the live watch stream: `kind: Pod` /
  `kind: Node` regexes run against the computed STATUS column; `kind: Event`
  regexes run against Warning-event reason and message (when both
  `reason_regex` and `message_regex` are present, **both** must match;
  `involved_kind` optionally narrows the involved object).
- `debounce_seconds` delays firing until the condition has persisted —
  one restart is not a crash loop.
- `detect: null` marks a playbook as **LLM-only** (no machine signal exists,
  or the signal is owned by another playbook).

!!! warning "A tuning knob set to `0` used to be read as *absent*"
    Every trend-predicate knob was parsed as `entry.get(key) or default`, which cannot tell
    "not set" from "set to zero" — so an author who wrote `0` silently got the default, and
    the detector that loaded was not the one that had been written. The two cases pull in
    opposite directions, so one rule does not cover them:

    - **`min_r2: 0` is a real setting.** The projection gates on `r2 < min_r2`, so zero
      deliberately turns the fit-quality check off. Restoring `0.5` makes the detector
      *quieter* than authored — a false negative, the failure nobody notices. It is now
      honoured, silently and correctly.
    - **`window_minutes`, `projection_horizon_minutes` and `fire_if_eta_within_minutes` at
      zero** produce a predicate the engine can never fire (a zero window fits no samples; an
      ETA of zero or less is already dropped before the `fire_if_eta_within_minutes` test).
      These fall back to the default **with a warning** naming the field, the authored value,
      the reason it cannot work, and that the loaded detector differs from the written one.
    - `debounce_seconds` accepts `0` (the documented "fire immediately"), and refuses a
      negative value, which would disable debouncing entirely rather than shorten it.

    An absent key and an explicit YAML `null` still take the default in silence — those are
    genuinely "not set".

!!! warning "`promql:` is recorded, not evaluated"
    Only `watch_predicates` and `trend_predicates` run. `promql:` is parsed,
    stored and exported, but **no code path evaluates it** — a detector whose
    only predicate is PromQL can never fire, and is now rejected at parse time
    rather than loading as a valid detector that silently does nothing. The 21
    `promql:` queries in the shipped playbooks all sit alongside real
    `watch_predicates`, so every shipped detector still fires; what those queries
    do *not* provide is any additional detection. Treat them as documentation of
    the metric signal until evaluation is built.

!!! warning "`kind:` is the observation channel, not the Kubernetes object"
    `kind:` selects which normalised stream the predicate reads — it is one of
    exactly **`Pod`**, **`Event`**, **`Node`**, and nothing else. Writing the
    object you care about (`kind: PersistentVolumeClaim`, `kind: Deployment`)
    still parses and compiles — `WatchPredicate.matches()` then falls through to
    `False` for any other value, so it matches nothing, ever. Two gates now catch
    it instead of letting it load as a valid detector that silently does nothing:
    `validate_detect_block` rejects it on the NL-authoring path, and
    `tests/test_every_detector_can_actually_fire.py` fails for a shipped playbook.
    The same two gates reject a `Pod`/`Node` predicate with no `status_regex`, and
    a `reason_regex`/`status_regex` that can only be satisfied by something no
    cluster emits — a space inside an anchored alternation (#114) is the classic.
    To narrow an Event to a subject, use `involved_kind:`:

    ```yaml
    - kind: Event                              # the channel
      reason_regex: "^(FailedBinding|ProvisioningFailed)$"
      involved_kind: PersistentVolumeClaim     # the object
    ```

    **`triggers:` and `detect:` are two independent features.** A playbook can
    have a perfect `triggers:` block and a permanently dead `detect:` block; only
    the zero-token detection is lost, and nothing goes red. So a playbook PR needs
    a test that the compiled predicate *fires* on a realistic event and does *not*
    fire on a neighbouring one — see `TestPvcPendingDetector` and
    `TestProbeDetectorsDoNotCrossFire` in `tests/test_detectors.py` for the shape.
    Two class guards (`test_every_watch_predicate_uses_a_known_observation_kind`,
    `test_no_reason_regex_alternative_contains_whitespace`) catch the two ways
    this has actually happened, and
    `tests/test_every_detector_can_actually_fire.py` generalises them: every
    string a `reason_regex`/`status_regex` can produce must be a value a cluster
    actually emits.

    The reverse — a perfect `detect:` block and a dead `triggers:` block — is
    quieter still, because it costs the playbook its place in the *router*.
    `Trigger` reads exactly `pod_status_regex`, `event_reason_regex` and
    `event_message_regex`; any other key is dropped, so `reason_regex:` one level
    up compiles to a trigger with nothing in it, and `match_playbooks` iterates
    it forever without matching. The loader now warns, and
    `tests/test_every_playbook_is_reachable.py` fails.

    Where a reason is shared by more than one failure — the kubelet emits
    `Unhealthy` for *both* readiness and liveness probes — the `message_regex`
    co-condition is what separates them. Without it, two playbooks fire on one
    event and the operator is told about a restart loop that is not happening.

Of the 23 shipped playbooks, **20 compile to detectors**; 3 are LLM-only
(`CommandHardcodedFailure` — disambiguated from CrashLoopBackOff only by
reading the pod spec — `ServiceUnreachable`, and `NetworkPolicyBlocking`,
where the packet is discarded in the CNI datapath so no machine signal
reaches the API server at all).

### Sensorium + detector engine (zero-token detection)

With `SENSORIUM_ENABLED=true` (default), the server keeps
`kubectl get pods -A --watch` and `kubectl get events -A --watch` streams
open, normalises each change into an observation, and evaluates every
compiled detector against it. **No LLM tokens are spent on detection.**

Three mechanisms keep the findings signal clean:

- **Debounce** — a matching observation *arms* a `(playbook, namespace,
  object)` key; it fires only if still armed after `debounce_seconds`.
- **Transition dedup** — a fired key does not re-fire while the condition
  persists. A pod status that stops matching clears the key (allowing a
  future re-fire); event-armed keys expire after 10 minutes without a new
  match.
- **Staleness filter** — on (re)connect, the watch replays recent event
  history; events older than the watcher's start time are discarded so
  bootstrap-era warnings don't fire detectors minutes after the fact.

The sensorium degrades gracefully: missing kubectl, RBAC failures, or dropped
watches disable perception (with exponential-backoff reconnect) but never
affect request handling.

Promoted (`active`) and `shadow` detectors are reloaded from the database every
`DB_DETECTOR_REFRESH_SECONDS`, so a promotion takes effect without a restart. **A refresh whose
query fails keeps the detectors already loaded** — it does not reload an empty set. Until
2026-08-24 it did: the read failed open to empty tuples and the refresh assigned them straight
into the live engine, so a transient DNS blip disarmed every promoted detector for a whole
refresh interval, with nothing logged to connect the outage to the loss of coverage. An empty
result from a *successful* query still applies, because that is a human demoting detectors and
is a real answer.

Two log lines make the state readable:

```text
sensorium: db detectors — 3 active, 0 shadow (was 0/0)
sensorium: db-detector refresh failed, KEEPING the <active>/<shadow> db detectors already loaded: …
load_db_detectors: <n> of <total> stored detectors were not loaded — nl:x: predicate is not valid JSON; …
```

The first is logged whenever the counts change, **including a drop to zero** — coverage going
away is the news, and the previous condition (`if active or shadow:`) announced arrival and hid
removal. The third exists because four of the five ways a stored row is discarded used to
`continue` in silence, leaving a table of malformed rows indistinguishable from an empty one.

**Disable:** `SENSORIUM_ENABLED=false`.

### The findings feed

Detector firings are **findings** — visible immediately, durable in the
[flight recorder](flight-recorder.md) under episode `findings:<cluster-id>`:

```bash
kq findings                 # table of recent firings
kq findings --limit 20
curl "$KUBE_Q_URL/v1/findings?limit=100"
```

`GET /v1/findings` returns `{"sensorium": "active", "sensorium_reason": "", "detectors": N,
"predictive": "active", "predictive_detectors": M, "predictive_error": null,
"streams": [...], "findings": [...]}` — each finding carries its playbook,
namespace, object, one-line evidence, and timestamps. `sensorium` is `active`
only while a watch stream is connected; in any other state an empty `findings`
list means *nothing was watched*, not *nothing happened*. `predictive` is the
independent claim for the anticipatory detectors, which read Prometheus rather
than the watch stream: `blind` means the last trend sweep could not query it, so
no prediction *could* have fired (see
[the endpoint reference](api-reference.md#get-v1findings)). What happens *after* a finding fires is
governed by the [autonomy ladder](autonomy.md).

### Cortex V4 (opt-in)

`CORTEX_V4_ENABLED` (default **`False`**) switches the per-turn graph from the
V2 coordinator described above to an explicit-node graph:

```
memory_loader → context_fetcher → triage → (gather_llm ⇄ gather_tools)* → synthesize → remember
```

What changes when it is on:

- **Structured triage plan with live transitions.** A fast triage model
  classifies the request (`chat` vs `investigate`) and produces the
  investigation plan as structured JSON — not parsed out of prose. Each tool
  batch advances the plan cursor and emits a `PlanEvent`, so `kq` shows steps
  moving `pending → in_progress → done` (or `skipped`) in real time.
  A batch in which **every** tool call errored ends the step as **`failed`** (`✗`, red),
  not `done`. `done` was set unconditionally once the batch returned — and a tool exception
  is deliberately turned into an ordinary `ToolMessage` so the model can react to it, so the
  batch returns identically whether every tool worked or every tool failed. The result was a
  green `✓` over a step that gathered nothing. A batch where *some* calls succeeded is still
  `done` — it did gather evidence — but every failure is named in a server-side warning
  either way.
- **True token streaming.** Only the synthesis model streams; the triage and
  tool-loop tiers have streaming disabled, so every token on the wire belongs
  to the final answer.
- **Tiered models.** Small/fast models handle triage and the tool loop; the
  large model writes the answer. With the existing `azure` / `openai`
  providers this reuses the coordinator/subagent deployments. An optional
  **Anthropic provider** is available: `LLM_PROVIDER=anthropic` with
  `ANTHROPIC_API_KEY`, `ANTHROPIC_LARGE_MODEL` (default `claude-sonnet-4-6`)
  and `ANTHROPIC_SMALL_MODEL` (default `claude-haiku-4-5-20251001`) — requires
  the `langchain-anthropic` extra.
- **Bounded gathering.** The LLM↔tools loop is capped at
  `CORTEX_MAX_GATHER_ROUNDS` (default `8`) iterations per turn.

HITL approval gates, role enforcement, the reflexion outcome path, and episode
writes are identical in both graphs. With the flag off (default), nothing in
this section changes the documented V2 behavior.
