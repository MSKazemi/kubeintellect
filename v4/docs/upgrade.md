---
description: >-
  How to move between KubeIntellect generations and safely enable the default-off
  V4 platform layers and V5 experimental flags — one flag at a time, with rollback.
---

# Upgrade & Feature-Flag Guide

KubeIntellect grows by **flags, not forks**. The current implementation is V4: the
proven V2 reasoning graph plus feature-gated V4 platform layers, with a set of
additive V5 experimental slices layered on top. Everything risky ships
**default-off**, and you turn it on one flag at a time — with a clear way to roll
back. This page is the runbook for doing that safely.

---

## The generation model (v1 → v5)

KubeIntellect is described in five generations, but they are not five codebases:

- **V1** — the original single-agent tool (legacy; lives on the `v1-legacy` branch).
- **V2** — the LangGraph coordinator + parallel subagents + playbooks + reflexion. This is the **default reasoning engine** that answers every query today.
- **V4** — the current branch: **V2 plus** the feature-flagged platform layers (sensorium + detectors, memory hierarchy, flight recorder, autonomy ladder + watchtower, and the opt-in V4 cortex).
- **V5** — additive, default-off experimental slices layered on the V4 server (not a fork). With every `KI_V5_*` flag unset and `CORTEX_V5_ENABLED=false`, the server is byte-identical to V4.

There is no `v3` in the feature story (`exp/v3` was an abandoned spike). For the
reasoning-engine differences between V2 and the V4 cortex, see
[V2 vs V4](v2-vs-v4-models.md).

---

## The safe-enablement philosophy

Three rules govern every flag on this page:

1. **Risky ships off.** Anything that could change cluster state on its own, spend
   unexpected tokens, or alter behavior in a hard-to-reason-about way defaults to
   off. The default configuration is safe to run in production.
2. **One flag at a time.** Enable a single flag, restart, verify it does what you
   expect, then move to the next. This keeps cause and effect legible.
3. **Failures degrade, never break.** Memory, flight-recorder, and sensorium write
   paths are fire-and-forget — a failure there is logged as a warning and **never
   breaks a user response**. Protected namespaces are always pinned to autonomy
   `A0`, and detector candidates always require human review before they can act.

---

## V4 platform layers you might toggle

These are the layers that make up the V4 surface. Most are **on by default**; the
opt-in ones are called out. Defaults below are verified against
`app/core/config.py`.

| Flag | Default | Unlocks | Prerequisites | Risk |
|---|---|---|---|---|
| `FLIGHT_RECORDER_ENABLED` | `true` | Hash-chained, append-only decision log; `kq replay`, `kq postmortem`, rollback points. | **PostgreSQL** | Low — audit only; an outage degrades auditability, never availability. |
| `SENSORIUM_ENABLED` | `true` | Always-on `kubectl --watch` perception feeding the zero-token detector engine (`kq findings`). | kubectl + RBAC (degrades gracefully) | Low — read-only observation. |
| `MEMORY_HIERARCHY_ENABLED` | `true` | L1 episodes + L2 temporal knowledge graph + consolidation worker. | **PostgreSQL** | Low — recall only; fail-open writes. |
| `PREFERENCE_MEMORY_ENABLED` | `true` | Learned operator preferences (explicit + inferred), `kq preference`. | **PostgreSQL** | Low — fail-open. |
| `POSTMORTEM_ENABLED` | `true` | Read-only grounded postmortem view; `kq postmortem`. | Flight recorder (PostgreSQL) | Low — read-only. |
| `WATCHTOWER_ENABLED` | `true` | Autonomous follow-up on detector findings, bounded by the autonomy ladder. | Sensorium | Medium — governed entirely by `AUTONOMY_LEVEL`. |
| `AUTONOMY_LEVEL` | `A1` | Ladder: `A0` observe · `A1` investigate+report · `A2` propose · `A3` auto-fix. | Watchtower | `A1`/`A2` read-only; **`A3` mutates** — allowlist-gated only. |
| `CORTEX_V4_ENABLED` | `false` | The V4 explicit-node reasoning graph (triage→gather→synthesize→remember) with tiered models. | — | Medium — swaps the reasoning engine; opt-in preview (flips when `cluster_resolved` reaches V2 parity). |
| `PREDICTIVE_DETECTION_ENABLED` | `false` | Anticipatory (trend) detection that warns before a slow-burn failure; still zero-token, capped at `A1`. | `PROMETHEUS_URL` | Low–Medium — never auto-fixes; fail-open. |
| `NL_DETECTOR_AUTHORING_ENABLED` | `false` | Compile a plain-English failure into a **shadow** detector; `kq detector`. | Sensorium | Low — shadow detectors never reach the watchtower until a human promotes them. |
| `POSTMORTEM_LLM_NARRATIVE` | `false` | Adds an LLM narrative on top of the deterministic postmortem timeline. | `POSTMORTEM_ENABLED` | Low — the only token-spending part; falls back to the deterministic timeline on failure. |

> **Anthropic note.** `LLM_PROVIDER=anthropic` is only wired through the V4 cortex —
> it has no effect unless `CORTEX_V4_ENABLED=true`.

### The Memory V5 slices (experimental, all default-off)

Additive, flag-gated upgrades to the memory hierarchy. **All default to `false`**;
each falls back to the V4 baseline on any error, and each is PostgreSQL-native. Turn
on at most one at a time. Full detail in [Configuration → V4](configuration.md#v4)
and [Memory](memory.md).

| Flag | Unlocks |
|---|---|
| `MEMORY_HYBRID_RETRIEVAL` | Reciprocal-Rank-Fusion recall (trigram + full-text), index-accelerated. |
| `MEMORY_BITEMPORAL_ENABLED` | Transaction-time axis on the KG: `as_of()` point-in-time queries, retract-not-delete. |
| `MEMORY_KG_PPR` | Multi-hop blast-radius via a bounded subgraph + in-process Personalized PageRank. |
| `MEMORY_WRITE_RECONCILE` | Mem0-style ADD/UPDATE/RETRACT/NOOP reconciliation behind a salience gate. |
| `MEMORY_PROMOTION` | The learning loop: verified episodes → `semantic_rules` → human-reviewed detector candidates. |
| `MEMORY_IMPORTANCE` | Importance/surprise-weighted recall ranking (never affects retention). |
| `MEMORY_PROSPECTIVE` | Post-fix "did the fix hold?" re-checks scheduled and fired through the autonomy ladder. |
| `MEMORY_SECURITY_HARDENING` | MINJA write-admission guard (trust + injection-signature + rate-limit + contradiction); RTBF; RLS scaffolding. |
| `MEMORY_SUMMARY_TREE` | RAPTOR/GraphRAG theme summaries, regenerated by KG change-rate. |

### The v5 trust-plane slices (experimental, all default-off)

A separate family of ~60 `KI_V5_*` / `CORTEX_V5_ENABLED` flags — ACI read verbs,
OTel spans, harness fan-out, the verification ladder, change-first RCA, and the
fail-closed write brakes (kill switch, change freeze, spend cap). `CORTEX_V5_ENABLED`
is the master switch; most slices need it **and** their own flag. Inspect the live
state with `kq v5-status`. These are documented in full in
[v5 experimental flags](v5-experimental-flags.md).

---

## Prerequisites: PostgreSQL vs SQLite

The cognitive layers are **PostgreSQL-native**. On SQLite all diagnostics still
work, but these are disabled:

| Needs PostgreSQL | Works on SQLite |
|---|---|
| Flight recorder, replay, postmortem, rollback points | All `kubectl` / Helm / Prometheus / Loki diagnostics |
| Memory hierarchy (episodes + temporal KG), memory loader | HITL approval and every safety gate |
| Reflexion (cross-session learned fixes) | Playbooks, subagent RCA, snapshot sufficiency |
| Operator preferences, all Memory V5 slices | Zero-token detection *itself* (sensorium runs; but findings persist to the DB) |

To switch to PostgreSQL, set `DATABASE_URL` (or the `POSTGRES_*` vars) and run
`kubeintellect db-init`. See [Configuration → Database](configuration.md#database).

---

## Runbook: enable a flag safely

Do this once per flag — never batch-enable a handful and hope.

**1. Check prerequisites.** If the flag is PostgreSQL-native, confirm you are on
PostgreSQL first (`kubeintellect status` shows the database mode). If it needs
Prometheus, confirm `PROMETHEUS_URL` is set.

**2. Set the flag.**

```bash
# pip install — updates ~/.kubeintellect/.env (restarts the service if active)
kubeintellect set PREDICTIVE_DETECTION_ENABLED=true

# Helm — set the corresponding config value in values.yaml, then helm upgrade
```

**3. Restart / redeploy.** Flags are read at startup (the cortex flag, for
example, is read once at graph-build time). `kubeintellect set` restarts the
systemd service automatically; otherwise restart `kubeintellect serve` or run
`helm upgrade`.

**4. Verify it took effect.**

```bash
kubeintellect status         # component health + database mode
kq v5-status                 # active KI_V5_* flags + trust-plane brakes (v5)
kq findings                  # zero-token detector firings (sensorium/predictive)
kq digest --hours 1          # what the watchtower did (autonomy)
```

Also check the server logs — e.g. `sensorium: started`, or
`CORTEX_V4_ENABLED — building the V4 explicit-node graph` when you enable the cortex.

**5. Roll back if needed.** Flip the flag back and restart — the layers are
additive, so disabling one returns to the prior behavior with no migration:

```bash
kubeintellect set PREDICTIVE_DETECTION_ENABLED=false
```

---

## Turning on autonomy carefully

Autonomy is the one area where a flag can *change your cluster*. The safe path:

1. Leave `AUTONOMY_LEVEL=A1` (the default) — the watchtower investigates and
   reports but never mutates. Watch `kq findings` and `kq digest` for a while.
2. Raise specific namespaces to `A2` (propose) with
   `AUTONOMY_NAMESPACE_LEVELS=dev=A2` — still no execution.
3. Only then consider `A3` (auto-fix), which requires **both** a namespace at `A3`
   **and** a matching `AUTONOMY_A3_ALLOWLIST` entry (e.g. `CrashLoopBackOff/dev-*`).
   An empty allowlist makes `A3` behave like `A2`.

Protected namespaces (`KUBECTL_BLOCKED_NAMESPACES`) are pinned to `A0` for
autonomous action no matter what you configure. Full detail in
[Autonomous Operations](autonomy.md).

---

## Related

- [Configuration Reference](configuration.md) — every flag, default, and Helm value.
- [v5 experimental flags](v5-experimental-flags.md) — the full `KI_V5_*` catalog and `kq v5-status`.
- [Autonomous Operations](autonomy.md) — the autonomy ladder, allowlist, and watchtower guard rails.
- [Security](security.md) — HITL, RBAC roles, and the protection layers you keep when enabling features.
- [V2 vs V4](v2-vs-v4-models.md) — what changes when you enable the V4 cortex.
