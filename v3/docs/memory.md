---
description: >-
  KubeIntellect v3 cross-session memory — how read_memory / write_memory persist
  and recall root-cause findings, user preferences, and failure patterns across
  sessions, and why it requires PostgreSQL.
---

# Memory

KubeIntellect v3 has an **on-demand, cross-session memory**: the coordinator can
recall what it learned in earlier sessions — a user's preferences, the outcome of
a past root-cause analysis, or a recurring failure pattern — and can persist a
new finding for future sessions to reuse.

Unlike v2, which loaded a memory block into the prompt on *every* turn, v3 makes
memory a pair of **tools the agent calls only when it decides prior context
matters** (`app/tools/memory_tool.py`). The net effect: smaller prompts on
simple turns, real recall on the turns where it counts.

!!! warning "PostgreSQL required"
    Memory is backed by PostgreSQL (`app/db/memory_store.py`, via `asyncpg`).
    In **SQLite mode** the tools no-op gracefully — `read_memory` returns a
    notice and `write_memory` is skipped and logged. Everything else
    (diagnostics, RCA, HITL) works unchanged; only cross-session recall is
    disabled. Confirm your mode with `kubeintellect status` (look for
    `DB: ✓ postgres`).

---

## The two tools

Both are registered on the coordinator in `app/agent/main_agent.py`.

### `read_memory(topic=None)`

Recall long-term memory for the current user/session.

- Called with **no `topic`**, it returns the index of available topics.
- Called **with a `topic`**, it returns the matching context block(s).

```text
read_memory()                       # → topic index
read_memory("crashloop_app_x")      # → notes matching that topic
```

The `user_id` and `session_id` are injected from the LangGraph config
(`configurable.user_id` / `configurable.session_id`), which the API sets from the
request `user` field and the `X-Session-ID` header — so recall is scoped to the
caller.

### `write_memory(topic, note, confidence=0.7)`

Persist a non-obvious finding for future sessions. The coordinator's prompt
directs it to call this **after a confident RCA** — when it has a load-bearing
insight worth recalling next time the same area is asked about.

```text
write_memory(
  topic="crashloop_app_x",
  note="app-x crashloops are usually OOM; container limit needs raising to 512Mi",
  confidence=0.9,
)
```

| Argument | Meaning |
|---|---|
| `topic` | Short identifier, e.g. `crashloop_app_x`, `ingress_ns_foo`. |
| `note` | One- to three-sentence summary worth remembering. |
| `confidence` | `0.0`–`1.0` (clamped). A write at **≥ 0.9** promotes the note toward a reusable failure pattern (see below). |

---

## Topics

`read_memory` surfaces four topics (from the index in
`app/tools/memory_tool.py`), each drawn from a table in the memory store:

| Topic | Source table | What it holds |
|---|---|---|
| `user_prefs` | `user_prefs` | Persistent per-user preferences. |
| `failure_hints` | `failure_patterns` | Auto-seeded, high-confidence recurring failure patterns. |
| `past_rca` | `rca_outcomes` | The last few root-cause outcomes for this user. |
| `session_notes` | `session_notes` | Notes from the current session. |

`load_memory_context()` reads these in priority order and concatenates them into
a single pinned context string.

---

## The self-improvement loop

Memory is not just a scratchpad — it compounds. When `write_memory` records an
outcome, `app/db/memory_store.py` runs the following:

1. **Record the outcome.** Every `write_memory` inserts a row into `rca_outcomes`
   (root cause, confidence, recommended fix). This becomes part of the user's
   `past_rca` recall.
2. **Seed a pattern (confidence ≥ 0.9).** A high-confidence outcome upserts a row
   in `failure_patterns` (`_maybe_seed_pattern`). The first sighting has
   `occurrence_count = 1`; each repeat increments the count and keeps the highest
   confidence seen.
3. **Promote to a failure hint (recurring).** `_load_failure_hints` only surfaces
   patterns with **confidence ≥ 0.9 AND occurrence_count ≥ 2** — so a pattern
   becomes a reusable "Known Failure Pattern" hint only once it has been seen
   more than once. This keeps one-off conclusions from polluting recall.

The result is a feedback loop: confident, *repeated* root causes graduate into
cluster-wide hints the coordinator can read on future investigations.

---

## Token budget

The pinned context returned by `load_memory_context()` is capped at
**~500 tokens** (`_MAX_CONTEXT_CHARS = 1800` characters at ~3.6 chars/token). If
the assembled context exceeds the cap it is truncated with a
`... [context truncated]` marker. This keeps recall cheap and predictable — it
never balloons the prompt.

---

## Reliability

Memory is **best-effort and non-blocking**:

- All reads are wrapped so a missing table or a transient connection error
  returns an empty string rather than failing the turn.
- `record_rca_outcome` swallows and logs write failures — a flaky database never
  breaks a diagnosis.
- In SQLite mode the whole subsystem is bypassed at the tool boundary, so there
  is nothing to fail.

---

## Storage

The tables live in the **same PostgreSQL database** as the LangGraph
checkpointer and the audit log (see [Operations → Database](operations.md#database)):

| Table | Role |
|---|---|
| `user_prefs` | Per-user preferences. |
| `session_notes` | Per-session notes. |
| `rca_outcomes` | The root-cause ledger (feeds `past_rca`). |
| `failure_patterns` | Promoted, recurring patterns (feeds `failure_hints`). |
| `runbooks` | Stored runbook entries. |

Back them up with your normal PostgreSQL tooling — they are ordinary tables, no
special handling required.

---

## Related

- [Agent Behaviors](agent-behaviors.md) — when the coordinator decides to read or
  write memory.
- [Architecture](architecture.md) — where memory sits in the DeepAgents topology.
- [Configuration](configuration.md) — PostgreSQL vs SQLite selection.
- [Operations](operations.md) — backup and retention of the memory tables.
