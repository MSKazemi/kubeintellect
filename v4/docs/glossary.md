---
description: >-
  Definitions of the KubeIntellect-specific terms used throughout the docs —
  coordinator, subagent, playbook, reflexion, HITL, RCA, and the routing
  sentinels.
---

# Glossary

Plain-English definitions of the terms used across these docs. Most link to the
page where the concept is explained in depth.

| Term | Meaning |
|---|---|
| **Coordinator** | The main reasoning agent. It receives your question, decides how to investigate, calls tools, and writes the final answer. Runs on the larger "coordinator" model (`gpt-4o` on the default Azure/OpenAI provider; configurable per provider). See [Agent Behaviors](agent-behaviors.md). |
| **Subagent** | A domain specialist (one of **pod**, **metrics**, **logs**, **events**) that the coordinator dispatches in parallel during a root-cause analysis. Each returns a structured finding. Runs on the cheaper "subagent" model (`gpt-4o-mini` on the default provider). See [Configuration → LLM provider](configuration.md#llm-provider). |
| **Context fetcher** | The workflow step that runs *before* the agent thinks — it pre-fetches a cluster snapshot (pods + Warning events) so the agent starts with situational awareness. |
| **Memory loader** | The step that loads pinned, cross-session context (user prefs, past root causes, failure hints) into the prompt. Active only on PostgreSQL. |
| **Cluster snapshot** | The pre-fetched picture of cluster health (pod list + Warning events) built at the start of every turn. Drives playbook matching and the snapshot sufficiency gate. |
| **Snapshot sufficiency gate** | A soft bias that lets the agent answer healthy, list-shaped questions straight from the snapshot instead of re-querying. Modes: `off` / `lenient` / `strict`. See [Agent Behaviors](agent-behaviors.md#snapshot-sufficiency-gate). |
| **Playbook** | A YAML investigation recipe for a known failure (CrashLoopBackOff, OOMKilled, …). When the snapshot matches, the playbook is injected into the prompt to guide the agent. 23 ship by default. See [Playbook library](agent-behaviors.md#playbook-library). |
| **Investigation plan** | A visible, up-front checklist the agent emits for queries needing 3+ steps, streamed as a `plan` event so the UI can show it. |
| **RCA** | **Root-Cause Analysis** — the deep investigation path where four subagents run in parallel and the coordinator synthesizes their findings into one conclusion. |
| **RCAResult** | The structured output of an RCA: root cause, confidence, supporting evidence, conflicting evidence, reasoning, and a recommended fix. See [What you can ask](capabilities.md#what-a-root-cause-answer-looks-like). |
| **AgentFinding** | A single subagent's structured contribution to an RCA (signals, hypothesis, confidence, evidence). |
| **HITL** | **Human-in-the-Loop** — the approval gate that pauses before any write operation runs, showing you the exact command. See [HITL gate](agent-behaviors.md#always-confirm-gate-overrides-auto_approve). |
| **Always-confirm gate** | A set of cascading-blast actions (`delete namespace`, `delete pv/crd`, `set image/resources`, `drain`) that always require explicit approval, even in auto-approve mode. |
| **`auto_approve`** | A per-request flag that skips routine approval gates for trusted automation. The always-confirm gate still fires. |
| **Reflexion** | The cross-session learning loop: KubeIntellect records verified root-cause outcomes and promotes recurring, confirmed patterns back into future prompts. PostgreSQL only. See [Reflexion Subsystem](reflexion.md). |
| **Failure pattern** | A recurring, verified problem signature that reflexion has promoted; it is recalled (cluster-scoped) to speed up future diagnoses. |
| **`TARGETED`** | A routing outcome: one specific resource is failing, so the agent runs a focused three-read investigation on it before answering. |
| **`RCA_REQUIRED`** | A routing outcome: the failure is ambiguous or cross-cutting, so the agent fans out to the four subagents for a full RCA. |
| **`ki_event`** | The side-channel message type on the SSE stream that carries progress (status, tool calls, tool results, plans) without disturbing OpenAI-compatible clients. See [API Reference](api-reference.md#post-v1chatcompletions). |
| **Tool** | A capability the agent can invoke: `run_kubectl`, `run_helm` (read-only), `query_prometheus`, `query_loki`. |
| **Role / RBAC tier** | The permission level of an API key: `readonly`, `operator`, `admin`, `superadmin` — determines which kubectl verbs the agent may run. See [Security](security.md). |
| **Demo key** | A short-lived, read-only HMAC API key minted on demand for time-boxed access (e.g. the public browser demo). |
| **Checkpointer / thread** | LangGraph's state store. Each conversation (keyed by `X-Session-ID`) is a thread whose state is persisted so a paused HITL turn can resume. |
| **`kq` / kube-q** | The command-line query client you talk to KubeIntellect with. Separate package (`kube-q`). See [CLI Reference](cli-reference.md#kq-query-client). |
| **LangGraph** | The agent-orchestration framework KubeIntellect is built on — it defines the workflow graph (memory → context → coordinator → subagents → synthesis). |
| **Memory V5** | The experimental, state-of-the-art-grounded upgrade to the memory hierarchy — a set of additive, default-off feature-flagged slices (hybrid recall, bi-temporal KG, PPR blast-radius, write reconciliation, promotion, importance/prospective, security hardening, summary tree). See [Memory](memory.md). |
| **Episodic / semantic / procedural / prospective memory** | The cognitive memory types the hierarchy maps onto: *episodic* = past incidents; *semantic* = distilled cluster facts (the knowledge graph); *procedural* = learned detectors/playbooks; *prospective* = "remember to re-check condition C at/after time T" (e.g. "did the fix hold?"). |
| **Bi-temporal knowledge graph** | The L2 semantic store with two time axes: *event-time* (when a cluster fact held) and *ingest-time* (when the agent learned it). Facts are invalidated (retracted), not deleted, enabling "what did we believe at T vs what was true at T" and point-in-time (`as_of`) queries. |
| **RRF (Reciprocal Rank Fusion)** | The method that fuses the trigram and full-text recall channels into one ranked list without score calibration — lifts episode recall over either channel alone. |
| **PPR (Personalized PageRank)** | Multi-hop "blast-radius" retrieval over the knowledge graph: ranks the entities most related to an incident from a bounded subgraph, in one shot instead of iterative loops. |
| **Promotion pipeline** | The learning loop that turns a verified, recurring incident into a reusable *semantic rule* (IF-context → THEN-guidance) and then a human-reviewed *detector candidate*. |
| **Importance / surprise** | Retention signals: *importance* (incident severity) modulates recall ranking (never retention); *surprise* (novelty vs recent memory) gates redundant low-value writes. |
| **MINJA** | Query-only memory injection — an attacker who can merely chat (no write access) seeds persistent poison that later recall replays as fact. Defended by the memory write-admission guard (see [Security](security.md)). |
| **Right-to-be-forgotten (RTBF)** | An operation that purges a subject's memory (a user's preferences/history or a specific entity) on request. |
| **OpsMemBench** | KubeIntellect's ops-memory benchmark: measures five memory-dependent abilities (cross-incident recall, time-travel reasoning, knowledge updating, learned detection, abstention) that no existing benchmark covers. |

---

## Related

- [Agent Behaviors](agent-behaviors.md) — how these pieces fit together each turn.
- [Architecture](architecture.md) — the developer-level reference.
- [What you can ask](capabilities.md) — these concepts in practice.
