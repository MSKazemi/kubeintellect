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
| **Rollback points armed** | pre-mutation state captures (see [Flight Recorder → Rollback points](flight-recorder.md#rollback-points)) |
| **User sessions** | count of interactive sessions in the window |

A quiet cluster produces an honest one-liner:
`Quiet watch: no findings in the last 24h.`

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
