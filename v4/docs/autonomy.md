---
description: >-
  Autonomous operations in KubeIntellect V4 — the watchtower that opens
  investigations without being asked, the A0–A3 autonomy ladder, per-namespace
  levels, the A3 allowlist, and the morning digest.
---

# Autonomous Operations (V4)

V2 KubeIntellect answers when you ask. V4 adds the inverse: when a compiled
detector fires on the live watch stream (see
[Agent Behaviors → V4 additions](agent-behaviors.md#v4-additions)), the
**watchtower** opens an investigation on its own — through the same session
machinery, the same tools, the same HITL gates, and the same
[flight recorder](flight-recorder.md) that a chat session uses. You read the
results in the [findings feed](#watching-it-happen) and the
[morning digest](#the-morning-digest).

How much it is allowed to do on its own is governed by the **autonomy ladder**,
configured per namespace.

---

## The autonomy ladder

| Level | Name | What happens when a detector fires |
|---|---|---|
| **A0** | observe | The finding is logged and visible in `kq findings` / the digest. No LLM investigation. |
| **A1** | investigate | The watchtower opens an autonomous investigation; the report is published (findings feed + episode). No mutations. |
| **A2** | propose | The investigation may also propose exact fix commands — but does not execute destructive actions. |
| **A3** | auto-fix | The proposed fix is executed without approval — **only** for `(playbook, namespace)` pairs on the explicit allowlist — and post-verified with a fresh read. |

Default level: **A1** — the agent investigates and reports, never touches the
cluster on its own.

### Configuration

| Flag | Default | Effect |
|---|---|---|
| `WATCHTOWER_ENABLED` | `True` | Master switch for autonomous investigations. Off = detectors still fire (findings/digest keep working), but nothing is investigated automatically. |
| `WATCHTOWER_ROLE` | `operator` | The role the autonomous identity runs as. The normal role ceiling applies inside the tools — an `operator` watchtower cannot run high-risk verbs even at A3. |
| `AUTONOMY_LEVEL` | `A1` | Default level for every namespace without an override. |
| `AUTONOMY_NAMESPACE_LEVELS` | `""` (empty) | Per-namespace overrides, comma-separated: `prod=A0,dev=A2`. |
| `AUTONOMY_A3_ALLOWLIST` | `""` (empty) | The only path to auto-fix: comma-separated `playbook/namespace` pairs, e.g. `CrashLoopBackOff/dev,ImagePullBackOff/staging`. The namespace part supports a trailing `*` glob: `CrashLoopBackOff/dev-*`. |

A3 requires **both** conditions: the namespace's effective level must be `A3`
*and* the firing playbook must match an allowlist entry for that namespace.
An empty allowlist means A3 behaves like A2 — nothing auto-executes.

```bash
# Example: investigate everywhere, propose in dev, auto-fix only
# CrashLoopBackOff in dev-* namespaces.
AUTONOMY_LEVEL=A1
AUTONOMY_NAMESPACE_LEVELS=dev=A2,dev-sandbox=A3
AUTONOMY_A3_ALLOWLIST=CrashLoopBackOff/dev-*
```

### Protected namespaces are always A0

Namespaces in `KUBECTL_BLOCKED_NAMESPACES` (default: `kubeintellect`,
`monitoring`, `kube-system`, `kube-public`, `kube-node-lease`,
`ingress-nginx`, `cert-manager`) are **pinned to A0 for autonomous action
regardless of configuration**. The watchtower never investigates or mutates
infrastructure namespaces on its own, even if you set
`AUTONOMY_NAMESPACE_LEVELS=kube-system=A3` by mistake.

Namespace names are compared case-insensitively and with surrounding whitespace stripped, the
same way `run_kubectl` compares them, so the two components cannot disagree about which
namespace is protected. Until 2026-08-20 that was a comment rather than a fact: the ladder
folded the namespace under test while the blocklist it compared against kept whatever case was
configured, and the kubectl gate folded neither side. `KUBECTL_BLOCKED_NAMESPACES="Kube-System"`
therefore left `kube-system` unpinned **and** unguarded. Both sides fold now, and a test asserts
the property directly — a namespace is pinned to `A0` exactly when the kubectl gate refuses it.

An entry that cannot match any namespace at all — a glob, a slash — protects nothing and is
reported at startup and under `unenforceable_guard_config` in
[`GET /v1/v5/status`](api-reference.md#get-v1v5status). Note the asymmetry with the setting two
sections up: `AUTONOMY_A3_ALLOWLIST` **does** support a trailing `*` on the namespace;
`KUBECTL_BLOCKED_NAMESPACES` does not.

### Cluster-scoped objects are capped at A1

A Node, PersistentVolume or ClusterRole has **no namespace**, so a model built on namespaces
cannot judge it. A Warning event about one (`NodeNotReady`, `Rebooted`,
`KubeletHasDiskPressure`) arrives with an empty namespace. Those findings are **investigated
and reported, never auto-fixed** — the effective level is capped at A1 no matter what
`AUTONOMY_LEVEL`, `AUTONOMY_NAMESPACE_LEVELS` or `AUTONOMY_A3_ALLOWLIST` say, and `a3_allowed`
refuses them outright. If you have pinned everything to A0, they stay at A0: the cap is a
ceiling, not a floor.

> ⚠️ **Fixed 2026-08-20.** An empty namespace previously fell through to the configured
> default. Because `fnmatch("", "*")` is true and `*` is the natural way to write "all my
> namespaces", an operator who set `AUTONOMY_A3_ALLOWLIST=SomePlaybook/*` silently made
> **Nodes auto-fixable** — the object where an unattended remediation (cordon, drain, delete)
> is least recoverable. Measured by feeding the watcher a real-shaped `NodeNotReady` event:
> `a3_allowed("NodeNotReady", "")` returned `True`. See
> `tests/test_autonomy_cluster_scoped_objects.py`.

---

## Watchtower guard rails

Two built-in guards prevent investigation storms:

- **Cooldown** — each `(playbook, namespace, object)` key gets at most one
  autonomous investigation per **30 minutes**. A flapping pod produces one
  investigation, not fifty.
- **Concurrency cap** — at most **2** autonomous investigations run at the
  same time; further findings queue behind the semaphore.

Both values are fixed in this release (not configurable via env).

Every autonomous investigation runs as session `auto-<finding-id>` and is
recorded by the flight recorder, so you can replay it later:

```bash
kq replay auto-fnd-1a2b3c4d5e6f
```

See [Flight Recorder & Replay](flight-recorder.md).

---

## Stopping the agent (break glass)

Two settings deny **every** autonomous write — the A3 auto-fix path included — without touching the
ladder configuration:

| Control | How to engage | Effect |
|---|---|---|
| **Kill switch** | `KI_V5_KILL_SWITCH=true` | every autonomous write is denied |
| **Change freeze** | `KI_V5_CHANGE_FREEZE=true` | every autonomous write is denied while it is set |

**Both are read from the environment once, when the process starts**, so engaging one means setting
it and restarting the pods. On a default chart install:

```bash
kubectl set env deploy/kubeintellect KI_V5_KILL_SWITCH=true   # explicit env beats the chart
                                                              # ConfigMap; rolls the pods
kq v5-status                                                  # confirm before trusting it
```

Know what is *not* there before you need it. There is **no API route and no `kq` command that
engages a brake** — `GET /v1/v5/status` reports the state and nothing changes it — and the chart
exposes neither setting as a Helm value. `app/autonomy/budget.py` does carry an in-process
`engage_kill_switch()`, but nothing outside tests calls it, and it sets a module global in **one**
process: with more than one replica it would stop only the replica that ran it. Until it has both a
surface and shared state, the restart above is the way to stop the agent.

Both controls are read through **one function each** — `kill_switch_engaged()` and
`change_freeze_active()` — and both write gates (the watchtower's A3 path and the ACI write
chokepoint) call those functions rather than reading a source directly. That is not tidiness: until
2026-08-20 the change freeze had no such reader, and the two gates disagreed about it — the
watchtower stopped, the write chokepoint did not.

Confirm the state — the same values the write path obeys:

```bash
kq v5-status          # kill_switch_engaged is printed in red when engaged
```

```json
GET /v1/v5/status → { "kill_switch_engaged": true, "change_freeze": false, … }
```

**Neither control is gated on `KI_V5_BLAST_RADIUS_BUDGET`.** That flag governs the
spend/blast-radius budget machinery; a kill switch is not a feature to opt into, so it binds
whether or not the budget gate is enabled. (Until 2026-08-20 it *was* gated on that flag, which
defaults to off — so an engaged kill switch was reported in the API and printed in red by the
CLI while the watchtower went on auto-fixing.)

What the brakes do **not** do: they bind the *agent's* write authority only. They never block a
running workload, a human `kubectl`, or your own break-glass access — those paths are independent
of the agent stack (04-trust §6).

A brake denies the write and lets the investigation continue: the finding is still investigated at
A1 and the proposed fix is still reported, it is simply not applied.

---

## Watching it happen

```bash
kq findings                 # recent detector firings (zero-token)
kq findings --limit 20
```

`kq findings` calls `GET /v1/findings` and lists each firing with its
playbook, namespace, object, and one-line evidence. If the server prints
*"Sensorium is disabled"*, set `SENSORIUM_ENABLED=true` (default) and check
the server logs for `sensorium: started`.

---

## The morning digest

The digest answers *"what happened while I was away"* — built
deterministically (no LLM call) from the flight recorder and the episode
store, never from a separate history.

```bash
kq digest                   # last 24 hours, rendered as Markdown
kq digest --hours 8
```

Or directly:

```bash
curl "$KUBE_Q_URL/v1/digest?hours=24&format=markdown"
curl "$KUBE_Q_URL/v1/digest?hours=24&format=json"
```

`GET /v1/digest` accepts `hours` (default `24`, max `168`) and
`format=json|markdown`. Sections:

| Section | Source |
|---|---|
| **Detector findings** | `finding` records in the flight recorder |
| **Autonomous investigations** | watchtower episodes, with outcome and verification status |
| **Pre-mutation state captures** | each with whether it is actually restorable — a redacted or truncated capture is evidence, not a restore point (see [Flight Recorder → Restorable vs recorded](flight-recorder.md#restorable-vs-recorded)) |
| **User sessions** | count of interactive sessions in the window |

A quiet cluster produces an honest one-liner:
`Quiet watch: no findings in the last 24h.`

!!! warning "\"Quiet\" is only claimed when the sources were readable **and something was watching**"
    An empty digest is reassuring only if the recorder could be read, so the
    digest never says *quiet* unless it was. If the flight recorder is disabled,
    the server is in [SQLite mode](flight-recorder.md#configuration-limitations)
    (which has no `decision_log` table), the watchtower is off, or a query fails,
    the digest is marked `degraded` and leads with
    `Digest INCOMPLETE … This is NOT a quiet watch: <reason>` — plus a warning
    block naming every source that could not answer. Whatever *is* readable is
    still reported. Before 2026-08-20 all of those states produced the same
    `Quiet watch` line as a genuinely quiet night, so a night with recording
    switched off was indistinguishable from a night on which nothing went wrong.

    Those are all checks on the **record**, and the record can be flawless and
    empty because nothing was ever looking. So the digest asks the **perception**
    side too, through the same classifier
    [`GET /v1/findings`](api-reference.md#get-v1findings) uses: if no `kubectl`
    watch stream is connected, or predictive detection is
    [blind](api-reference.md#get-v1findings), that is a `degraded_reason` as well.
    Measured 2026-08-20 with every recording source healthy and nothing watching,
    `/v1/findings` answered `{"sensorium": "starting", "findings": []}` and
    `kq findings` refused to call it clear — while `kq digest` over the same window
    said *"Quiet watch: no findings in the last 24h."* The two surfaces now read one
    classifier, so they cannot answer differently. What they cannot do is
    reconstruct the window: stream health is a *current* state, so the reported gaps
    are a lower bound on the blindness in the window, never an upper one.

    `kq digest` still exits `0` when the digest is degraded — it is a successful
    report of a degraded state, not a failed command. Scripts that need to branch
    on it should read `degraded` from the JSON form.

---

## Verify it works

You never type a query in this walkthrough — that is the point.

**1. Inject a crash-looping pod** in any non-protected namespace:

```bash
kubectl create namespace demo-auto
kubectl run crashy -n demo-auto --image=busybox --restart=Always -- sh -c "exit 1"
```

**2. Watch the finding appear** (zero LLM tokens spent):

```bash
kq findings
# fired     playbook          namespace   object   evidence
# 14:02:31  CrashLoopBackOff  demo-auto   crashy   pod status=CrashLoopBackOff
```

The CrashLoopBackOff detector carries a **60-second debounce by design** — a
single restart is not a crash loop. Expect roughly **3 minutes from injection
to a finished autonomous report** (measured on a Kind cluster): the pod has to
restart into `CrashLoopBackOff`, the debounce has to elapse, and the A1
investigation has to run.

**3. Read the report** in the digest:

```bash
kq digest --hours 1
# ## Detector findings (zero-token)
# - 14:02 **CrashLoopBackOff** demo-auto/crashy
# ## Autonomous investigations
# - 14:04 [report_only] ns=demo-auto: The pod crashy is crash-looping because ...
```

**4. Clean up:**

```bash
kubectl delete namespace demo-auto
```

---

## Related

- [Agent Behaviors → V4 additions](agent-behaviors.md#v4-additions) — the sensorium and detector engine that produce findings.
- [Flight Recorder & Replay](flight-recorder.md) — the tamper-evident record of every autonomous action.
- [Memory Hierarchy](memory.md) — how investigations become recallable episodes.
- [Security](security.md) — roles, HITL, and protected namespaces.
