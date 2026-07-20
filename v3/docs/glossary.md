---
description: >-
  Definitions of the KubeIntellect v3 terms — coordinator, subagent, DeepAgents,
  virtual filesystem, snapshot, playbook, HITL, checkpointer, memory, and more.
---

# Glossary

Terms used throughout the KubeIntellect v3 documentation and code.

| Term | Meaning |
|---|---|
| **Coordinator** | The single top-level agent (`app/agent/main_agent.py`, built with `create_deep_agent`). It answers simple questions directly, plans multi-step work, dispatches subagents, and synthesises their findings. Runs on the larger model (default `gpt-4o`). |
| **Subagent** | A specialist agent the coordinator delegates to via the `task()` tool. v3 has **five**: `pod_specialist`, `metrics_specialist`, `logs_specialist`, `events_specialist`, and `deep_investigator` (`app/agent/subagents.py`). Each runs on the cheaper model (default `gpt-4o-mini`). |
| **`deep_investigator`** | The fifth subagent — a catch-all for cross-domain or unusual probes (compare namespaces, validate a NetworkPolicy, correlate a metric + log spike, RBAC audit). It has the full tool surface (`run_kubectl`, `query_prometheus`, `query_loki`), unlike the four single-domain specialists. |
| **DeepAgents** | The framework KubeIntellect v3 is built on. It provides the coordinator/subagent topology, the `task()` delegation tool, the virtual filesystem (`write_file` / `read_file` / `ls`), and `write_todos` planning. |
| **RCA** | Root-Cause Analysis — the fan-out investigation the coordinator runs for a "why is X broken" question: it dispatches the four domain specialists in parallel and synthesises `/findings/*.md` into one evidence-cited answer. |
| **Virtual filesystem** | An in-state, in-memory filesystem provided by DeepAgents. The coordinator and subagents read and write files like `/snapshot.md` and `/findings/pod.md` here — it is not the host disk; it lives in the LangGraph checkpoint. |
| **Cluster snapshot** | `/snapshot.md` — an all-namespace `kubectl get pods` + Warning-events summary with front-matter (`built_at`, `pod_count`, `has_issues`, `has_warnings`). Pre-seeded at session start by the runner and refreshed on demand via `refresh_snapshot()` (`app/tools/snapshot_tool.py`). |
| **Snapshot refresh** | Re-running the two kubectl probes to rebuild `/snapshot.md`. The coordinator refreshes when the snapshot is missing, older than ~30 s, or the cluster was just mutated. |
| **Playbook** | A YAML file (`app/agent/playbooks/`) describing a known failure pattern: triggers, investigation steps, expected evidence, and a fix template. Fetched on demand via `lookup_playbook(symptom)`. v3 ships 10. |
| **`write_todos`** | The DeepAgents planning tool. For a 3+-step investigation the coordinator writes a short todo list first; it is surfaced to clients as a `plan` `ki_event`. |
| **HITL** | Human-in-the-Loop — the approval gate. Before any destructive `kubectl` verb runs, `run_kubectl` raises a LangGraph `interrupt()`; the graph pauses until the user replies `yes`/`approve` or `no`/`deny` in the same session (`app/tools/kubectl_tool.py`, `app/agent/hitl.py`). |
| **`auto_approve`** | A per-request flag (or the session phrase `approve all`) that arms a HITL bypass — write operations run without an approval prompt. Intended for trusted automation or evaluation. |
| **Risk level** | The classification a destructive verb gets: **high** (delete, drain, replace, taint) or **medium** (patch, apply, scale, exec, cordon, uncordon, create, run). Shown on the approval prompt as `risk_level`. |
| **Memory** | v3's cross-session recall — the `read_memory` / `write_memory` tools backed by PostgreSQL (`app/db/memory_store.py`). Topics: `user_prefs`, `failure_hints`, `past_rca`, `session_notes`. No-ops in SQLite mode. See [Memory](memory.md). |
| **Failure pattern** | A recurring root cause auto-seeded into the `failure_patterns` table when a `write_memory` is recorded at confidence ≥ 0.9. Promoted to a reusable "failure hint" once its occurrence count reaches 2. |
| **Checkpointer / thread** | The LangGraph store that persists conversation and interrupt state per session (`AsyncPostgresSaver` for PostgreSQL, `AsyncSqliteSaver` for SQLite). A **thread** is one session, keyed by `X-Session-ID`; the checkpointer is what lets a HITL approval arrive hours later. |
| **`ki_event`** | The side-channel event type in the SSE stream (`status`, `tool_call`, `tool_result`, `plan`). Carried with an empty `choices` array so OpenAI-only clients ignore it. See [API Reference](api-reference.md). |
| **Tool** | A function the agent can call. Direct coordinator tools: `run_kubectl`, `query_prometheus`, `query_loki`, `refresh_snapshot`, `lookup_playbook`, `read_memory`, `write_memory`, plus the DeepAgents built-ins (`task`, `write_todos`, `write_file`, `read_file`, `ls`). |
| **Role / RBAC tier** | The four-tier permission model resolved from the API key: `superadmin`, `admin`, `operator`, `readonly` (`app/api/v1/auth.py`). It bounds which kubectl verbs the agent may even attempt. See [Security](security.md). |
| **Demo key** | A short-lived, read-only API key of the form `ki-ro-<payload>.<sig>`, validated by HMAC (`AUTH_BACKEND=hmac` + `DEMO_KEY_HMAC_SECRET`) without a static list or restart. |
| **`kq` / kube-q** | The standalone query client (`pip install kube-q`, command `kq`). A thin SSE client that streams answers from the server; it never touches your cluster directly. See [CLI Reference](cli-reference.md). |
| **LangGraph** | The stateful agent-orchestration library underneath DeepAgents. It provides the graph runtime, the `interrupt()` used for HITL, and the checkpointer. |
| **Snapshot sufficiency** | The coordinator's prompt bias toward answering list-shaped questions from a healthy `/snapshot.md` instead of re-querying — always falling back to fresh data for logs, metrics, history, named resources, or after a mutation. |

---

## Related

- [Architecture](architecture.md) — how these pieces fit together.
- [Agent Behaviors](agent-behaviors.md) — how the coordinator investigates.
- [Memory](memory.md) — the cross-session memory subsystem.
- [Security](security.md) — roles, HITL, and secret protection.
