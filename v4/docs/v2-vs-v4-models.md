---
description: >-
  How KubeIntellect's two reasoning graphs differ — the default ReAct graph
  versus the opt-in Cortex graph — when to choose each, and how model tiers are assigned when you
  enable CORTEX_V4_ENABLED.
---
# ReAct vs Cortex graphs (V2 vs V4) — Reasoning Engines & Models

How KubeIntellect's two reasoning engines differ, when to choose each, what changes when
you enable the Cortex graph, and how the model tiers are assigned. This is the user-facing companion
to the [Architecture](architecture.md) reference and the
[V4 platform layers](architecture.md#v4-architecture-feature-flagged-preview) table.

KubeIntellect ships **two reasoning graphs side by side**, selected at server
startup by a single flag, `CORTEX_V4_ENABLED` (see [Configuration](configuration.md)):

- **off (default)** → the **ReAct graph** — a LangGraph `create_react_agent` loop that
  carries the V2 coordinator forward.
- **on** → the **Cortex graph** — a hand-rolled, explicit-node graph with tiered models,
  built for the V4 generation.

Everything else in the V4 platform (sensorium + detectors, memory hierarchy, flight
recorder, autonomy ladder + watchtower) is **on by default** and is independent of
this flag. This page is only about the *reasoning engine* and the *models it uses*.

> **Names.** *V1–V4* are product **generations**: what you install is the v4
> platform, whichever graph it runs. Earlier docs called the two graphs "the V2 graph"
> and "the V4 cortex"; this page calls them the **ReAct graph** and the **Cortex graph**.
> Code, log messages and the flag keep the old names — `CORTEX_V4_ENABLED` is unchanged,
> so existing configuration keeps working, and "the V2 graph" in a server message means
> the ReAct graph.

---

## Side-by-side comparison

| Dimension | ReAct graph (default) | Cortex graph (`CORTEX_V4_ENABLED=true`) |
|---|---|---|
| Engine | `create_react_agent` prebuilt (ReAct loop) | Explicit nodes: **triage → gather (LLM ⇄ tools) → synthesize → remember** |
| Models | Single tier — one model for the whole turn | **Tiered** — small/fast model for triage + specialists, large model for synthesis only |
| Streaming | Token buffering needed to suppress intermediate ReAct chatter | Native — only the synthesis node streams tokens; triage/specialist tiers stream nothing |
| Investigation plan | Inferred after the fact (regex over sentinel strings like `RCA_REQUIRED`, `TARGETED:`) | First-class state; `PlanEvent`s fire as nodes execute |
| Tool-call / HITL accounting | Inferred by counting tool calls | Exact — driven by explicit state transitions |
| Provider support | Azure / OpenAI-compatible (`openai`, `qwen`, `local`) | Azure / OpenAI-compatible **and** Anthropic |
| Gather loop bound | n/a | `CORTEX_MAX_GATHER_ROUNDS` (default 8) |

The three "ReAct workarounds" — token buffering, plan-string regex parsing, and
orphaned-tool-call patching after HITL interrupts — exist only because of the
ReAct prebuilt, and are removed on the Cortex path.

---

## The model tiers (Cortex only)

The headline difference is **model tiering**. The ReAct graph uses one model for an
entire turn. The Cortex graph splits the work by cost/capability:

| Stage | Tier | Azure / OpenAI default | Anthropic default |
|---|---|---|---|
| Triage + specialists (pod / metrics / logs) | small / fast | `gpt-4o-mini` (`AZURE_SUBAGENT_DEPLOYMENT`) | `claude-haiku-4-5` (`ANTHROPIC_SMALL_MODEL`) |
| Synthesis (the final answer) | large | `gpt-4o` (`AZURE_COORDINATOR_DEPLOYMENT`) | `claude-sonnet-4-6` (`ANTHROPIC_LARGE_MODEL`) |

The premise: classification and evidence-gathering don't need a frontier model;
only the final synthesis does. Routing the cheap stages to a small model lowers
cost and latency per turn while reserving the large model for where it matters.
A model factory abstracts the provider, so the same tiering applies whichever
`LLM_PROVIDER` you pick. **Anthropic is only wired through the Cortex graph** — setting
`LLM_PROVIDER=anthropic` while `CORTEX_V4_ENABLED=false` is refused at startup (the
server exits with status 1). It used to fall back silently to OpenAI with
`OPENAI_API_KEY`; see [#192](https://github.com/MSKazemi/kubeintellect/issues/192).

---

## What happens at runtime when you turn the Cortex graph on

The flag is read once, at graph-build time, so a **server restart is required**.
Per query, the Cortex path then:

1. Swaps the reasoning engine — every query flows through the explicit
   triage→gather→synthesize→remember nodes instead of the ReAct loop.
2. Applies the model tiers above (cheap triage/specialists, large synthesis).
3. Streams only synthesis tokens to the client; emits exact `PlanEvent`s.
4. Writes an episode at turn end via the `remember` node (feeding the
   [memory hierarchy](memory.md)).

What does **not** change: checkpointing, HITL approval, the Postgres saver, and all
other V4 platform layers behave identically. To confirm the Cortex graph is live, look for this
server log line on startup:

```
CORTEX_V4_ENABLED — building the V4 explicit-node graph
```

It also **compounds with autonomy**: with the watchtower on (default,
`AUTONOMY_LEVEL=A1`), detector firings open autonomous investigations — and those
now run through the Cortex graph too, not just interactive chat.

---

## Why it ships off-by-default — the LOFA-L4 A/B

The Cortex graph is **kept side-by-side with the ReAct graph on purpose**: the flag lets
the same query run through either engine so they can be compared head-to-head. That
comparison is the **LOFA-L4 A/B**.

**LOFA** = *leap-of-faith assumption* — a pre-registered risky bet with a named
fallback that fires if a measured kill-criterion is hit (rather than pushing through
with heroics). **LOFA L4** is the bet behind the cortex rebuild:

> *Tiered models (small triage/specialists + large synthesis) will not degrade answer
> quality versus V2's single tier.*

The **A/B path** tests it by running identical scenarios through both engines and
scoring them on a multi-dimension judge.

- **Kill criterion:** if the Cortex judge score drops more than 10% below the ReAct
  graph, fall back
  to a single tier (plus caching) — i.e. abandon tiering.
- **Result:** the gate **passed and exceeded the ReAct baseline** on identical live
  scenarios — tiering did not hurt quality, it helped.

So if LOFA-L4 passed, why is the flag still off? Because the **judge gate is not the
same as the ship gate**. The remaining flip criterion is **`cluster_resolved`
parity with the ReAct graph** on the fault subset — the trusted "did the cluster actually get
healthy" trend metric. Until the Cortex graph matches the ReAct graph on that operational
measure, the ReAct graph stays the default and Cortex remains an opt-in preview.

> **Correction to earlier guidance:** the LOFA-L4 *evaluation* gate has already
> passed (and beat the ReAct baseline). The flag is off-by-default because of the
> *separate* `cluster_resolved`-parity ship criterion, not because the evaluation is
> unfinished. Detailed eval numbers live in the private `design/` tier.

---

## Which graph to choose

| Your use | Graph | Why |
|---|---|---|
| The default, and anything that should end in a fix | ReAct graph | It still leads on `cluster_resolved`, the measure that decides the default (above) |
| Diagnosis first — root-cause answers you read and act on yourself | Cortex graph | It passed the quality gate above the ReAct baseline, and its tiered models make each turn cheaper |
| `LLM_PROVIDER=anthropic` | Cortex graph | Required — the ReAct graph refuses to start with Anthropic |
| A3 fixes gated on a fully supported diagnosis (`SELF_GOVERN_ENABLED=true`) | Cortex graph | The `ground_check` node exists only on the Cortex graph; on the ReAct graph A3 is not grounding-gated ([Autonomy](autonomy.md)) |

HITL approval, RBAC and the kubectl safety guard apply on both graphs — choosing a
graph never changes what needs a human's approval.

---

## How to switch engines

Set the flag in your environment / `.env` (see [Configuration](configuration.md) for
precedence and Helm wiring), then restart the server:

```bash
# Cortex graph (tiered models, explicit-node graph)
CORTEX_V4_ENABLED=true

# back to the default ReAct graph
CORTEX_V4_ENABLED=false
```

Because the Cortex graph is still an opt-in preview, treat enabling it as running the
evaluation engine: watch answer quality and cluster-resolution outcomes against the
ReAct graph before relying on it in production.

---

## Concepts explained simply (plain English)

New to the terminology? This section explains the jargon used above without
assuming background. Read it first if "Cortex", "LOFA", or "L1/L2/L4" are unfamiliar.

### Heads-up: there are two different "L" numbering systems

The single most confusing thing is that **two unrelated things both use the letter
"L"**, and they have nothing to do with each other:

| System | What it numbers | Numbers used |
|---|---|---|
| **Memory tiers** | Layers of the memory hierarchy | L0, L1, L2, L3 |
| **LOFA gates** | Risky design bets that had to be proven | L1, L2, L3, L4 |

So **"LOFA L4" is about *models*, not memory.** Memory only goes up to **L3** —
there is no "memory L4." Keep the two systems separate and the rest is easy.

### What is the "Cortex"?

The Cortex is the V4 generation's **reasoning engine** — the "brain" that decides how to answer your
question. When you ask *"why is my pod crashing?"*, the Cortex runs the steps:

> **triage → gather (look at the cluster) → synthesize (write the answer) → remember**

The default ReAct graph (carried forward from V2) uses a pre-built black-box loop for
this. The Cortex rebuilds it with explicit,
named steps you can control. It is switched on with `CORTEX_V4_ENABLED=true`.

### What is a "bet" / "LOFA"?

**LOFA = Leap-Of-Faith Assumption.** It is just a **risky guess the team believed but
had not yet proven**, and committed real work to. Calling it a "bet" captures that
risk — you could be wrong.

The discipline: before testing, you write down the exact result that would prove the
guess **wrong** — the **kill criterion** — so you can't fool yourself later. If that
result happens, you take a planned fallback instead of pushing on.

Everyday analogy: opening a food truck and guessing *"people will pay extra for a
healthy menu."* That's a bet. The kill criterion: *"if fewer than 50 people a week buy
it, I drop it and go back to burgers."* You decide the give-up line up front, then test.

There were four such bets in V4, numbered L1–L4:

| LOFA | The risky guess | About |
|---|---|---|
| **L1** | The cluster memory stays fresh fast enough (< 30s lag) | speed of memory |
| **L2** | The zero-token detectors are accurate (few false alarms) | detector quality |
| **L3** | The system can investigate problems on its own well enough | autonomy |
| **L4** | A *cheap small* model for easy steps + a *big* model only for the final answer won't hurt quality | **models** |

**LOFA L4** is the bet behind the Cortex's tiered models. Its kill criterion was *"if
the cheap+big mix scores more than 10% worse than the ReAct graph's single model, give up and go
back to one model."* It passed — the mix actually scored **better**, so it was kept.

### The memories: what V2 vs V4 remember

**V2 has one kind of memory** (the [reflexion subsystem](reflexion.md)): two tables
that remember *"this fix worked for this kind of failure before."* It cannot answer
*"what changed in the cluster 5 minutes ago."*

**V4 keeps that and adds a four-layer hierarchy** (see [Memory hierarchy](memory.md)):

| Tier | Name | Remembers | Plain meaning |
|---|---|---|---|
| **L0** | Working memory | The current chat's messages | "What we're talking about right now" |
| **L1** | Episodes | Every past investigation, summarized | "Last time something like this happened, here's what we found" |
| **L2** | Temporal knowledge graph | Cluster parts + how they connected **over time** | "This pod moved to another node 3 minutes ago" |
| **L3** | Procedural | Playbooks + V2's proven fix patterns + learned detectors | "The proven recipe for this problem" |

The two new powers V2 never had:

- **L1 (episodes)** — remembers *whole past investigations*, not just fix snippets.
- **L2 (temporal graph)** — tracks the cluster's structure *with timestamps*, so
  *"what changed between 14:02 and 14:07"* is one fast query instead of the AI digging
  around.

One sentence: **V2 remembers fixes that worked; V4 also remembers entire past
investigations (L1) and the cluster's history over time (L2)** — so it can reason
about *what changed and when*, which V2 could not.

### Jargon recap

- **ReAct graph** — the default reasoning engine, carried forward from V2 (the "V2 graph"
  in code and logs).
- **Cortex graph** — the V4 generation's rebuilt reasoning brain (explicit steps); opt-in.
- **LOFA / bet** — a risky guess with a pre-written "here's how we'll know it failed".
- **LOFA L4** — the bet that mixing cheap + expensive models keeps quality high (about
  *models*, **not** memory).
- **Memory L0–L3** — the four memory layers; the *only* place "memory" is numbered.

---

## Related docs

- [Architecture](architecture.md) — full system reference and the V4 layers table
- [Agent behaviors](agent-behaviors.md) — coordinator behaviors shared by both engines
- [Configuration](configuration.md) — every flag, default, and Helm value
- [Memory hierarchy](memory.md) — what the `remember` node feeds
- [Autonomous operations](autonomy.md) — the watchtower that also runs through the Cortex graph when it is on
