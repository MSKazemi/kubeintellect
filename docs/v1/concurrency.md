# Multi-User Concurrency in KubeIntellect

This document describes what happens when two (or more) users query KubeIntellect simultaneously from different computers.

---

## TL;DR

**It works correctly for normal queries.** Each user's workflow runs in its own isolated LangGraph thread, keyed by `conversation_id`. LangGraph state is now persisted in PostgreSQL (`AsyncPostgresSaver`) — no state is lost on pod restart and multiple replicas can share it. Tool registry metadata is also in PostgreSQL (`tool_registry` table), so concurrent writes from multiple pods are safe.

---

## Request Flow for Concurrent Users

```
User A (PC-1) ──POST /v1/chat/completions──┐
                                            ├──► FastAPI async event loop
User B (PC-2) ──POST /v1/chat/completions──┘         │
                                                      │
                          ┌───────────────────────────┤
                          │                           │
                   coroutine A                 coroutine B
                thread_id=thread_convA    thread_id=thread_convB
                          │                           │
             AsyncPostgresSaver[A]     AsyncPostgresSaver[B]
              (isolated by thread_id,   (isolated by thread_id,
               durable across restarts)  durable across restarts)
                          │                           │
                   LangGraph agents         LangGraph agents
                  (shared graph object,    (same shared graph object,
                   separate state)          separate state)
                          │                           │
                   Kubernetes API             Kubernetes API
```

---

## Component-by-Component Analysis

### 1. FastAPI / ASGI layer

- **Single-process async** (uvicorn default: 1 worker). Requests are handled as coroutines on a single event loop — they interleave at `await` points but do not run in parallel threads.
- Both requests are accepted immediately and processed concurrently without blocking each other.
- **If deployed with multiple uvicorn workers** (`--workers N`), all of the shared-memory concerns below apply across OS processes as well.

### 2. LangGraph workflow (`kubeintellect_app`)

- A **single compiled graph** (`kubeintellect_app`) is created at FastAPI startup (not at import time) and shared by all requests.
- State isolation is provided by `thread_id` in the `configurable` dict:
  ```python
  thread_id = f"thread_{conversation_id}"   # unique per LibreChat conversation
  config["configurable"] = {"thread_id": thread_id}
  ```
- The `AsyncPostgresSaver` checkpointer stores each thread's state in PostgreSQL, keyed by `thread_id`. User A and User B never touch the same row, so their graph states are **fully isolated**.
- **No locking is needed between users** for the graph state itself.

### 3. AsyncPostgresSaver (durable LangGraph checkpointer)

| Property | Detail |
|---|---|
| Storage | PostgreSQL via `AsyncConnectionPool` (psycopg3) |
| Isolation | Per `thread_id` — users are isolated |
| Persistence | **Durable** — state survives pod restart |
| Growth | Managed by LangGraph; old checkpoints can be pruned |

Replaces the former in-process `MemorySaver`. The connection pool is created at startup (`app/orchestration/workflow.py → initialize_workflow_async()`) and closed on shutdown (`close_langgraph_checkpointer()`). LangGraph creates its own schema tables (`langgraph_checkpoint*`) via `checkpointer.setup()` on first startup.

### 4. PostgresCheckpointer (HITL checkpoints)

- Uses `ThreadedConnectionPool` (psycopg2, min/max configurable).
- HITL state is keyed by `(user_id, thread_id)` — unique per conversation.
- Both users can hit a HITL breakpoint at the same time; their checkpoints go to separate rows in `workflow_checkpoints`.
- **Safe for concurrent use.**

### 5. Worker agents (`worker_agents` global dict)

- `worker_agents` is a module-level dict of `{name: agent_runnable}`.
- During normal request processing, agents are only **read** from this dict — safe for concurrent access.
- `reload_dynamic_tools_into_agent()` **mutates** `worker_agents`. If two CodeGenerator flows complete at the same time (two users each triggered code generation), both calls execute concurrently without a lock. This is a **race condition**: one reload may see a partially-updated dict.
- In practice, CodeGenerator is rare and the mutation is fast (dict assignment), so the window is narrow — but it is not safe under strict concurrent CodeGenerator completions.

### 6. Tool registry (PostgreSQL `tool_registry` table)

- Tool metadata is stored in the `tool_registry` PostgreSQL table, managed by `ToolRegistryService` (`app/services/tool_registry_service.py`).
- Uses the shared `PostgresCheckpointer` psycopg2 connection pool — no extra connections.
- Concurrent reads and writes are safe across any number of pods; PostgreSQL row-level locking handles isolation.
- Tool code files (`.py`) still live on the PVC, but the PVC is only **written once per tool** at generation time — no concurrent write races on file content.
- **Safe for concurrent multi-pod use.**

### 7. Kubernetes API calls

- Both users' tool calls hit the Kubernetes API independently.
- **Read operations** (list pods, get logs, describe deployment) are always safe to run in parallel — the K8s API is stateless for reads.
- **Write/delete operations** from two users targeting the **same resource** can conflict:
  - User A deletes a deployment while User B is patching it → K8s returns 404 or 409.
  - KubeIntellect tools return `"Error: ..."` strings in these cases; the LLM typically explains this to the user.
  - There is **no application-level locking** or user intent coordination for write operations.

---

## Summary Table

| Concern | Status | Notes |
|---|---|---|
| Concurrent request handling | ✅ Safe | FastAPI async; no blocking |
| Graph state isolation (per user) | ✅ Safe | Isolated by `thread_id` in AsyncPostgresSaver |
| HITL state persistence across restarts | ✅ Fixed | AsyncPostgresSaver — durable in Postgres |
| HITL checkpoints (custom) | ✅ Safe | ThreadedConnectionPool; keyed per conversation |
| Tool registry (PostgreSQL) | ✅ Safe | `tool_registry` table; row-level locking; safe across all pods |
| K8s read operations | ✅ Safe | Stateless reads |
| K8s write/delete conflicts | ⚠️ Partial | K8s returns 404/409; no app-level coordination |
| `reload_dynamic_tools_into_agent` race | ⚠️ Issue | Concurrent CodeGenerator completions can race (threading.Lock added) |

---

## Multi-replica architecture

KubeIntellect is designed for horizontal scaling. The following design decisions enable safe multi-pod deployments:

- **LangGraph state in PostgreSQL** (`AsyncPostgresSaver`) — durable across restarts; HITL resume works across pod boundaries.
- **Runtime-tools PVC as `ReadWriteMany`** (Azure Files via `azurefile-csi-retain`) — all pods share the same generated tool files.
- **HPA support** — `hpa.enabled: true` in `values-azure.yaml`; the Horizontal Pod Autoscaler is pre-configured for production.
- **Concurrency cap** — `MAX_CONCURRENT_WORKFLOWS` (`asyncio.Semaphore`) prevents TPM exhaustion under load.
- **Tool reload lock** — `threading.Lock` in `agents.py` serialises concurrent `reload_dynamic_tools_into_agent()` calls.
- **Tool registry in PostgreSQL** — `ToolRegistryService` uses row-level locking; safe for concurrent writes from multiple pods.
