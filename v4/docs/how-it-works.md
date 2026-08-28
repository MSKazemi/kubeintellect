# How it works

The short version: **detection costs zero tokens, and nothing mutates your cluster without
passing a gate that the prompt cannot talk its way through.**

Everything below is the path a real signal takes through V4. The stage names are the actual
modules — `app/sensorium/`, `app/detectors/`, `app/autonomy/`, `app/tools/aci/mutating.py`,
`app/db/flight_recorder.py` — not a simplified teaching diagram.

<div class="ki-flow" role="img" aria-label="Signal flow: cluster signals enter the sensorium, are evaluated by compiled zero-token detectors, and only a firing detector invokes the LLM. Any proposed mutation passes the autonomy ladder and the write chokepoint, which returns auto, approve, or deny. Every decision is appended to a hash-chained flight recorder.">

<svg viewBox="0 0 600 792" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <marker id="ki-arrow" viewBox="0 0 10 10" refX="9" refY="5"
            markerWidth="5" markerHeight="5" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" class="ki-arrowhead"/>
    </marker>
    <path id="ki-track" d="M 300 126 V 232" fill="none"/>
  </defs>

  <!-- ── Zone 1: always on, no model ───────────────────────────────── -->
  <rect class="ki-zone ki-zone-free" x="25" y="30" width="550" height="290" rx="12"/>
  <text class="ki-zone-label" x="45" y="55">ALWAYS ON  ·  ZERO TOKENS</text>

  <rect class="ki-node" x="180" y="70" width="240" height="56" rx="9"/>
  <text class="ki-node-text" x="300" y="94">Cluster</text>
  <text class="ki-sub"       x="300" y="112">kubectl watch · PromQL · LogQL</text>

  <path class="ki-edge" d="M 300 126 V 148" marker-end="url(#ki-arrow)"/>

  <rect class="ki-node" x="180" y="152" width="240" height="56" rx="9"/>
  <text class="ki-node-text" x="300" y="176">Sensorium</text>
  <text class="ki-sub"       x="300" y="194">normalise → Observation</text>

  <path class="ki-edge" d="M 300 208 V 230" marker-end="url(#ki-arrow)"/>

  <rect class="ki-node ki-node-key" x="180" y="234" width="240" height="56" rx="9"/>
  <text class="ki-node-text" x="300" y="258">Detectors</text>
  <text class="ki-sub"       x="300" y="276">compiled predicates · no model</text>

  <!-- the pulse that never stops: perception is continuous -->
  <circle class="ki-pulse" r="5">
    <animateMotion dur="2.6s" repeatCount="indefinite" calcMode="linear">
      <mpath href="#ki-track"/>
    </animateMotion>
  </circle>

  <!-- ── The threshold ─────────────────────────────────────────────── -->
  <path class="ki-edge ki-edge-fire" d="M 300 290 V 344" marker-end="url(#ki-arrow)"/>
  <text class="ki-threshold"     x="392" y="312">a detector fires</text>
  <text class="ki-threshold-sub" x="392" y="330">— only now —</text>

  <!-- ── Zone 2: the model ─────────────────────────────────────────── -->
  <rect class="ki-zone ki-zone-llm" x="25" y="350" width="550" height="110" rx="12"/>
  <text class="ki-zone-label ki-zone-label-llm" x="45" y="375">LLM INVOKED</text>
  <rect class="ki-node ki-node-llm" x="180" y="386" width="240" height="56" rx="9"/>
  <text class="ki-node-text" x="300" y="410">Investigation</text>
  <text class="ki-sub"       x="300" y="428">correlate evidence → root cause</text>

  <!-- ── Down to the write path ────────────────────────────────────── -->
  <path class="ki-edge" d="M 300 460 V 500" marker-end="url(#ki-arrow)"/>
  <text class="ki-sub ki-sub-left" x="312" y="484">a mutation is proposed</text>

  <rect class="ki-node" x="55" y="504" width="230" height="56" rx="9"/>
  <text class="ki-node-text" x="170" y="528">Autonomy ladder</text>
  <text class="ki-sub"       x="170" y="546">A0 · A1 · A2 · A3</text>

  <rect class="ki-node ki-node-gate" x="315" y="504" width="230" height="56" rx="9"/>
  <text class="ki-node-text" x="430" y="528">Write chokepoint</text>
  <text class="ki-sub"       x="430" y="546">decide_write()</text>

  <path class="ki-edge" d="M 285 532 H 313" marker-end="url(#ki-arrow)"/>

  <!-- three outcomes -->
  <path class="ki-edge" d="M 430 560 V 596" marker-end="url(#ki-arrow)"/>
  <text class="ki-out ki-out-auto"    x="180" y="618">auto</text>
  <text class="ki-out ki-out-approve" x="300" y="618">approve → human</text>
  <text class="ki-out ki-out-deny"    x="470" y="618">deny</text>
  <path class="ki-edge ki-edge-dim" d="M 430 596 H 200"/>
  <path class="ki-edge ki-edge-dim" d="M 430 596 H 490"/>
  <path class="ki-edge ki-edge-dim" d="M 200 596 V 604"/>
  <path class="ki-edge ki-edge-dim" d="M 490 596 V 604"/>

  <!-- Flight recorder spans the width -->
  <path class="ki-edge" d="M 300 630 V 668" marker-end="url(#ki-arrow)"/>
  <rect class="ki-node ki-node-log" x="90" y="672" width="420" height="56" rx="9"/>
  <text class="ki-node-text" x="300" y="696">Flight recorder</text>
  <text class="ki-sub"       x="300" y="714">append-only · hash-chained per episode</text>
  <text class="ki-sub ki-sub-quiet" x="300" y="756">every decision above is recorded, including the denials</text>
</svg>

</div>

<style>
.ki-flow svg { width: 100%; height: auto; display: block; margin: 1.2rem 0; }
.ki-flow text { font-family: var(--md-text-font-family, system-ui, sans-serif); text-anchor: middle; }

.ki-zone { fill: none; stroke-width: 1; stroke-dasharray: 5 5; }
.ki-zone-free { stroke: var(--md-default-fg-color--lighter); }
.ki-zone-llm  { stroke: var(--md-primary-fg-color); }
/* NOTE the `.ki-flow` prefix on the two text-anchor rules below: `.ki-flow text`
   has higher specificity (0-1-1) than a bare class (0-1-0), so an unprefixed
   `text-anchor: start` here silently loses to the `middle` default and the label
   renders centred on its x — off the left edge of the canvas, clipped. */
.ki-flow .ki-zone-label { font-size: 12px; letter-spacing: .1em; text-anchor: start;
                 fill: var(--md-default-fg-color--light); font-weight: 700; }
.ki-zone-label-llm { fill: var(--md-primary-fg-color); }

.ki-node { fill: var(--md-code-bg-color); stroke: var(--md-default-fg-color--lighter); stroke-width: 1; }
.ki-node-key  { stroke: var(--md-default-fg-color--light); stroke-width: 1.5; }
.ki-node-llm  { stroke: var(--md-primary-fg-color); stroke-width: 1.5; }
.ki-node-gate { stroke: var(--md-primary-fg-color); stroke-width: 2; }
.ki-node-log  { stroke-dasharray: 4 4; }
.ki-node-text { font-size: 17px; font-weight: 600; fill: var(--md-default-fg-color); }
.ki-sub       { font-size: 12px; fill: var(--md-default-fg-color--light); }
.ki-flow .ki-sub-left { text-anchor: start; }
.ki-sub-quiet { font-style: italic; }

.ki-edge { stroke: var(--md-default-fg-color--light); stroke-width: 1.5; fill: none; }
.ki-edge-dim { stroke: var(--md-default-fg-color--lighter); stroke-dasharray: 4 4; }
.ki-edge-fire { stroke: var(--md-primary-fg-color); stroke-width: 2; }
.ki-arrowhead { fill: var(--md-default-fg-color--light); }

.ki-threshold     { font-size: 13px; font-weight: 700; fill: var(--md-primary-fg-color); }
.ki-threshold-sub { font-size: 11px; fill: var(--md-default-fg-color--light); }

.ki-out { font-size: 14px; font-weight: 600; }
.ki-out-auto    { fill: var(--md-default-fg-color--light); }
.ki-out-approve { fill: var(--md-primary-fg-color); }
.ki-out-deny    { fill: var(--md-default-fg-color--light); }

.ki-pulse { fill: var(--md-primary-fg-color); }

/* Respect the reader's motion preference — the diagram is fully legible when still. */
@media (prefers-reduced-motion: reduce) {
  .ki-flow animateMotion { display: none; }
  .ki-pulse { display: none; }
}
</style>

## Reading the diagram

**The pulse never stops.** Perception is continuous: `app/sensorium/` runs always-on watchers
that normalise cluster signals into `Observation` records and feed them to the detector engine.
This replaced the per-query snapshot as the primary perception path (the snapshot survives as a
chat-turn fallback).

!!! note "Replayed history is not news"

    A `kubectl --watch` connection replays recent event history the moment it opens, so
    without a filter every reconnect would re-fire detectors on warnings that are minutes
    old. `app/sensorium/k8s_watcher.py` drops any `Event` whose own timestamp predates the
    watch epoch by more than 30 seconds.

    An event the watcher **cannot** date — no `lastTimestamp`/`eventTime`/`creationTimestamp`,
    or one it cannot parse — is processed anyway: refusing to act on a real incident because
    of a timestamp quirk is the worse failure for a watchtower. That choice is never silent.
    The server log carries one `k8s_watcher: … the event-staleness filter cannot run` warning
    per distinct reason per connection, so "the filter passed this event" and "the filter did
    not run" are always distinguishable.

    Timestamps with no timezone suffix are read as UTC, which is what Kubernetes emits. Reading
    them as host-local time instead shifted every age by the host's UTC offset — east of UTC
    that dropped *current* events as stale, which is a missed detection rather than a cosmetic
    error.

**The left zone costs nothing.** `app/detectors/` is described in its own docstring as *"innate
immunity — compiled, zero-token detection of known failures"* (ADR-006). Detectors evaluate
compiled predicates — status regexes, event reason/message matching, instant PromQL — against
the observation stream. No model is involved. Running an LLM in a hot loop over cluster state is
the obvious design, and it is both expensive and noisy.

**The threshold is the whole point.** The model is invoked *only* when a detector fires. That is
the line marked `finding` — everything left of it is free and always on; everything right of it
costs tokens and happens rarely.

**What gates an autonomous write today.** `app/autonomy/` (ADR-003) caps what may happen at all:
A0 observe → A1 investigate + report → A2 propose (HITL) → A3 auto-fix, allowlisted patterns
only and rollback-armed, configured per cluster with per-namespace overrides. On the live A3
path (`app/autonomy/watchtower.py`) a write must clear all three of them: the cluster's ladder
level, an explicit `(playbook, namespace)` allowlist entry, and `auto_write_permitted()`, which
denies outright on an engaged kill switch or a declared change freeze.

**Where that path is going.** `app/tools/aci/mutating.py` stamps a proposed mutation with a
**rollback class** and composes one write-authority decision — the blast-radius/spend gate, the
action class's earned rung, and reversibility — returning **auto**, **approve** or **deny**
*before anything executes*, and dry-running the command server-side before an `auto` stands.
That chokepoint is built and tested, but **it is not yet wired into the graph**: `earned_rung`
still arrives as its `L2` default. It is the designed destination for A3, not a brake running
in your cluster today.

`promotion_outcomes` — the ADR-102 store that would earn that rung — **does** now have a
production writer, behind `KI_V5_STATISTICAL_PROMOTION` (default off). When an autonomous
investigation actually mutates the cluster, the coordinator re-snapshots it and grades the
result; a graded attempt is recorded as one sample for the `watchtower-autofix` action class.
Three things are deliberately *not* recorded, because each would be a sample the store invented:
a fix a human asked for (not this class), a report-only investigation (nothing was attempted),
and an outcome whose post-fix cluster read failed — that is reported as *unverified*, and a read
is most likely to fail right after the disruptive change being graded. Evidence therefore
accrues — and, behind the same flag, it is now **read on the A3 path**: before the watchtower
lets an investigation fix anything, it asks the store whether `watchtower-autofix`'s recorded
record has taken that authority away, and closes the gate if it has (two postcondition failures
within 24 h will do it). That read runs **one way only: it can revoke, never grant.** Every
sample in the store comes from a fix the watchtower was already allowed to make, so promoting on
them would be circular — the grant producing the evidence for the grant — and it would deadlock,
since a class with no samples could never take the action that produces one. ADR-102 earns rungs
from *shadow* agreement, which this system does not run yet. The allowlist therefore stays the
only way up. If the flag is on and there is no store to read, the brake is not operating and the
log says so; if there is a store and it cannot be read, auto-fix is revoked rather than assumed
clean. All of that is visible on `GET /v1/v5/status` under `autonomy_promotion` — `operating`
rather than `enabled` is the field that answers whether the brake is in your write path. The chokepoint above is a separate, still-unwired destination — this brake sits in the
watchtower, where the live A3 decision actually is.

The decision function itself is built and tested, and its asymmetry runs one way only: a
critical incident blocks promotion **and** forces demotion. That second half was missing —
the CUSUM fast trip (2 postcondition failures in 24 h ⇒ −1 rung) skipped failures marked
critical, and the two ADR triggers that exist for critical incidents are L4-scoped, so below
L4 the worst failures were the only ones no fast trigger watched. The anti-flap band had the
opposite fault: a Wilson lower bound is driven by sample size as well as by failures, so a class
with a **flawless 15-for-15** record sat under θ − 0.05 and was demoted a rung with the reason
*"last-50 LCB < θ − 0.05"* — while a class with **no** record at all was not demotable, making
evidence of competence strictly worse than no evidence. The band now abstains wherever a perfect
record at that sample size could not clear it, so a breach is always attributable to failures;
the CUSUM trip, which does not depend on sample size, is unchanged.

Both are enforced server-side, in the write path rather than in the prompt. A prompt-level
"please ask first" constraint is not a security boundary, and shipping one that pretends to be
would be worse than shipping nothing.

**Everything lands in the log.** `app/db/flight_recorder.py` (ADR-005) appends every typed event
to a hash-chained decision log, chained per episode so after-the-fact tampering is detectable.
`kq replay <session-id>` reconstructs a session and exits non-zero if the chain is broken — or
if the recorder lost events, which it records in the chain rather than letting a hole pass as a
complete record ([flight recorder](flight-recorder.md#tamper-evidence)).

!!! note "What this diagram does not claim"

    `mutating.py` is the **decision core** — it classifies and decides, with no cluster and no
    execution. Actual execution, server-side `--dry-run`, and Kyverno/VAP admission are
    separately cluster-gated; this seam is what they plug into. The diagram shows the decision
    path, which is the part that holds regardless of how execution is wired.

## Where to go next

- [Architecture](architecture.md) — the full component breakdown
- [Autonomous operations](autonomy.md) — the ladder in operational detail
- [Security](security.md) — RBAC, the approval gate, and the trust boundary
- [Flight recorder](flight-recorder.md) — the audit chain and `kq replay`
