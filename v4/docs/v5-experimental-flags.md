---
description: >-
  Reference table for KubeIntellect's additive, default-off v5 experimental flags
  (KI_V5_* / CORTEX_V5_*) — K8s-ACI verbs, OTel spans, harness fan-out,
  verification ladder, responsiveness budgets, and the L0 file plane.
---
# v5 experimental flags (`KI_V5_*` / `CORTEX_V5_*`)

All v5 slices are **additive and default-off**: with `CORTEX_V5_ENABLED=false` and every `KI_V5_*`
unset, the server is byte-identical to V4 (ADR-019). `CORTEX_V5_ENABLED` is the master switch —
most slices require it **and** their own flag. Inspect the live state with
[`kq v5-status`](cli-reference.md#kq-v5-status) or [`GET /v1/v5/status`](api-reference.md#get-v1v5status).

!!! warning "⚠️ = declared but not wired (audited 2026-08-19)"

    A flag marked ⚠️ is read by **no code path**. Setting it changes nothing. 25 of the flags below
    are in this state: the configuration surface was written ahead of the implementation. They are
    kept (rather than deleted) because each names a designed slice.

    **The product now says so itself.** Setting one of these does not put it in `active_flags` /
    `experimental_flags`; it appears instead under **`set_but_unwired_flags`** in
    [`GET /healthz`](api-reference.md#get-healthz) and
    [`GET /v1/v5/status`](api-reference.md#get-v1v5status), and the startup log line marks it
    `[set but NOT WIRED, no effect: …]`. Until 2026-08-19 it was reported as *active*, which meant
    an operator could turn on a switch, read back that the feature was on, and be wrong.

    This covers **knobs as well as switches**. Until 2026-08-20 the report read only booleans, so
    the eleven `float`/`int` entries in the list — `KI_V5_AGENT_COST_RATE_CAP` and
    `KI_V5_SPEND_OUT_PRICE_PER_1K` among them — could not appear in it at *any* value, and the
    paragraph above was false for them. A knob counts as set when it differs from the default in
    this table, not when it is merely non-zero: `KI_V5_AGENT_COST_RATE_CAP=0` is a deliberate
    instruction, and reporting it as untouched was the point of the bug.

    The list is `UNWIRED_EXPERIMENTAL_FLAGS` in `app/core/version.py`, and
    `tests/test_v5_flag_wiring.py` checks it against real `settings.<FLAG>` usage — so it can only
    shrink: wiring a flag without deleting its entry fails the suite, and so does adding a new
    unwired one.

| Flag | Default | Purpose |
|---|---|---|
| `CORTEX_V5_ENABLED` | `False` | Cortex V5 / K8s-ACI (v5 design specs/00, specs/01; ADR-101) Additive, default-off v5 slices layered on the V4 server (NOT a fork) |
| `KI_V5_ACI_READ_VERBS_ENABLED` ⚠️ | `False` | K8s-ACI v0: bounded, normalized, never-silent read-only verbs (inspect/search/logs/diff_change) wrapping run_kubectl |
| `KI_V5_ACI_MAX_LINES` | `100` | Output-window bound for ACI verbs (100-line rule from the ACI SOTA). |
| `KI_V5_ACI_MAX_CHARS` | `8000` | Soft character cap applied after the line window (≈2k-token guard). |
| `KI_V5_OTEL_SPANS_ENABLED` | `False` | OTel GenAI spans (specs/02): emit agent/tool/mcp/mutation spans as additive hash-chained decision_log rows (kind=ki_otel_span) |
| `KI_V5_HARNESS_FANOUT` | `False` | Harness gather-loop (ADR-101): apply the never-silent, line-aligned ≤2k summary bound to gather-tool output instead of v4's silent mid-line 8k chop… |
| `KI_V5_HARNESS_MAX_SUBAGENTS` | `4` | Upper bound on concurrent read-only investigation subagents the harness runner may fan out per gather round (ADR-101) |
| `KI_V5_HARNESS_MAX_SUBAGENT_ROUNDS` | `3` | Bound on ACI-verb rounds an individual investigation subagent may take before it must summarize (P2 body; keeps each isolated investigator cheap an… |
| `KI_V5_HARNESS_SUBAGENT_LARGE_MODEL` | `False` | Run fan-out investigation subagents on the larger (coordinator) model tier instead of the small specialist |
| `KI_V5_VERIFY_LADDER` | `False` | Verification ladder, read side (P2, 01-architecture §3.6): an adversarial fresh-context reviewer checks the synthesized RCA against the gathered ev… |
| `KI_V5_RESPONSIVENESS` | `False` | Responsiveness (P2, REQ-developer-19): never let a long investigation present as a silent block — emit a progress heartbeat every KI_V5_HEARTBEAT_S… |
| `KI_V5_HEARTBEAT_SECONDS` | `10.0` | SSE gap ceiling for a running phase (>10 s rule) |
| `KI_V5_FIRST_SIGNAL_BUDGET_S` | `30.0` | p90 first-useful-result target |
| `KI_V5_FULL_BUDGET_S` | `120.0` | p90 full-investigation target |
| `KI_V5_ESCALATION_BRIEFS` | `False` | Escalation-avoidance briefs (P2, A-CH-17-07): append a responder-skill-calibrated brief with EXPLICIT escalate-only-if bounds to an investigation,… |
| `KI_V5_RESPONDER_LEVEL` | `"intermediate"` | Default responder skill the brief calibrates to (junior\|intermediate\|senior) when the caller does not specify one. |
| `KI_V5_RUNBOOK_SKILLS` | `False` | Runbooks-as-skills v0 (P2): render the matched playbooks as on-demand SKILL.md blocks injected into the gather prompt — only the ones whose trigger… |
| `KI_V5_FILE_PLANE` | `False` | L0 file plane (P2): the consolidation worker regenerates CLUSTER.md + MEMORY.md ≤25KB projections of the memory (files are a projection; Postgres s… |
| `KI_V5_FILE_PLANE_DIR` | `"ki-memory"` | output dir for CLUSTER.md / MEMORY.md |
| `KI_V5_FILE_PLANE_MAX_BYTES` | `25_000` | per-file byte ceiling (the ≤25KB rule) |
| `KI_V5_INVESTIGATION_WRITEBACK` | `False` | Investigation write-back (P2): confirm/contradict traversed KG edges via reconcile_edge after an investigation, so the topology self-corrects at ne… |
| `KI_V5_CHANGE_FIRST_RCA` | `False` | Change-first RCA (P2, A-CH-02-07): rank candidate root causes by the recent-change prior (≈79% of outages follow a change) |
| `KI_V5_CHANGE_LEDGER` | `False` | Change ledger (P1 evidence substrate): capture the changes KubeIntellect itself applies (its mutating kubectl commands) into a per-cluster ledger t… |
| `KI_V5_BLAST_RADIUS_BUDGET` ⚠️ | `False` | **No effect — read by no code.** Its only consumer short-circuited the kill switch and change freeze to "allow", so setting it disabled a brake rather than adding one (fixed 2026-08-20); the brakes below are now unconditional. Reported under `set_but_unwired_flags` by `/v1/v5/status` if you set it |
| `KI_V5_KILL_SWITCH` | `False` | engage ⇒ deny ALL autonomous writes (fail-closed). **Not gated on `KI_V5_BLAST_RADIUS_BUDGET`** — a kill switch is an operator saying stop, not a feature to opt into |
| `KI_V5_CHANGE_FREEZE` | `False` | deny every autonomous write while set (a declared change moratorium). Applies regardless of `KI_V5_BLAST_RADIUS_BUDGET`, and — since 2026-08-20 — on **both** write gates, not only the watchtower's |
| `KI_V5_SPEND_CAP_USD` | `0.0` | per-scope spend ceiling (0 = unlimited; needs usage source) |
| `KI_V5_SPEND_IN_PRICE_PER_1K` ⚠️ | `0.0025` | USD per 1k input tokens (default gpt-4o-ish) |
| `KI_V5_SPEND_OUT_PRICE_PER_1K` ⚠️ | `0.01` | USD per 1k output tokens |
| `KI_V5_STAGED_PROPAGATION` ⚠️ | `False` | Staged propagation (P3 blast-radius): a multi-target change is released in bounded stages with a wait window between them — never instant-global (r… |
| `KI_V5_STAGE_SIZE` ⚠️ | `1` | max targets applied per stage |
| `KI_V5_STAGE_WINDOW_SECONDS` ⚠️ | `300.0` | min wait between stages |
| `KI_V5_FAILURE_DOMAIN_BUDGET` ⚠️ | `False` | Change-schedule + failure-domain budget (P3 blast-radius, REQ-sysadmin-18): a disruptive op is denied if it would push a failure domain (zone/rack)… |
| `KI_V5_MAX_UNAVAILABLE_PER_ZONE` ⚠️ | `0.34` | never take >~1/3 of a failure domain down at once |
| `KI_V5_STATISTICAL_PROMOTION` ⚠️ | `False` | Statistical autonomy promotion engine (P3 Trust plane, ADR-102): an action-class earns a rung only when its shadow-agreement Wilson-LCB clears the… |
| `KI_V5_OFFLINE_SHADOW_WEIGHT` ⚠️ | `0.5` | ADR-102 arm-3 offline-shadow weighting: offline-derived promotion outcomes count this much (≤ the 0.5 cap) vs a live shadow run's 1.0 |
| `KI_V5_ACI_MUTATING_VERBS` ⚠️ | `False` | ACI mutating-verb chokepoint (P3 Action/Trust): every proposed mutation is stamped with a rollback class and routed through a write-authority decis… |
| `KI_V5_MODEL_ROUTING` ⚠️ | `False` | Heterogeneous model routing + air-gap floor (P4, ADR-103): small/local model for triage & tool-call formatting, frontier for RCA synthesis; when di… |
| `KI_V5_AIRGAP_FLOOR` ⚠️ | `False` | (see config.py) |
| `KI_V5_CHANGE_WATCHDOG` | `False` | Per-change ephemeral watchdog (P4, A-CH-02-14): a change in the ledger arms a bounded, TTL-scoped read-only investigation that "reads the diff" — c… |
| `KI_V5_WATCHDOG_TTL_SECONDS` | `300` | (see config.py) |
| `KI_V5_WATCHDOG_MAX_ACTIVE` | `5` | (see config.py) |
| `KI_V5_RIGHTSIZING` ⚠️ | `False` | Rightsizing recommendations (P4, A-CH-08): evidence-grounded resource-limit advice from observed OOM/peak-memory/CPU-throttle signals (recommendati… |
| `KI_V5_PREDICTIVE_FUSION` | `False` | Predictive-detection fusion (P4): a predicted (ADR-010 TrendPredicate) finding now LAUNCHES a read-only investigation of the leading indicators bef… |
| `KI_V5_NL_DETECTOR_LADDER` ⚠️ | `False` | NL-detector → statistical ladder (P4, ADR-012): a shadow NL-authored detector accumulates precision stats; when its true-positive-rate LCB clears t… |
| `KI_V5_DETECTOR_MIN_FIRINGS` ⚠️ | `20` | min shadow firings before eligible |
| `KI_V5_DETECTOR_PRECISION_THETA` ⚠️ | `0.90` | true-positive-rate LCB bar to surface for review |
| `KI_V5_AGENTIC_WORKLOAD_DETECTOR` ⚠️ | `False` | Agentic-workload SRE + GPU-health detectors (P4, D17/SD-D): the incumbent-empty surface — agent runaway (tool-call rate/cost), sandbox-escape, and… |
| `KI_V5_GPU_HEALTH_DETECTOR` ⚠️ | `False` | (see config.py) |
| `KI_V5_AGENT_TOOL_RATE_CAP` ⚠️ | `60.0` | tool calls/min above this ⇒ runaway |
| `KI_V5_AGENT_COST_RATE_CAP` ⚠️ | `1.0` | USD/min above this ⇒ runaway spend |
| `KI_V5_PREDICTIVE_PRECAPTURE` | `False` | Predictive pre-capture (P4, A-CH-20-16): a predicted finding with a near-term ETA arms high-verbosity recorders / a CRIU checkpoint BEFORE the fail… |
| `KI_V5_PRECAPTURE_ETA_MIN` | `15.0` | only pre-capture when ETA ≤ this (targeted spend) |
| `KI_V5_FLEET_EXCHANGE` ⚠️ | `False` | Fleet memory exchange (P5, ADR-105): cross-cluster memory sharing with strict tenant isolation (a cluster only ever reads its own tenant's fleet kn… |
| `KI_V5_FLEET_SIGNAL_POOLING` ⚠️ | `False` | Fleet-wide signal pooling (P5): pool agent-runaway / detector hits across a tenant's clusters — the same pattern on >= N clusters is a fleet-wide i… |
| `KI_V5_FLEET_PATTERN_MIN_CLUSTERS` ⚠️ | `3` | same signal on >= this many clusters ⇒ fleet alert |
| `KI_V5_CAPABILITY_SANDBOX` ⚠️ | `False` | Two-axis capability sandbox (P3 Trust): the agent acts through an impersonated ServiceAccount scoped to a capability role (read-only / namespace-wr… |
| `KI_V5_SANDBOX_SA_NAMESPACE` | `"kubeintellect"` | (see config.py) |
| `KI_V5_SANDBOX_READONLY_SA` | `"ki-readonly"` | (see config.py) |
| `KI_V5_SANDBOX_WRITER_SA` | `"ki-writer"` | (see config.py) |

_60 flags — generated from `app/core/config.py`. Booleans default `False` (off)._
