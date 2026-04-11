# KubeIntellect Security Model

> This document describes the security model for the CodeGenerator agent — the component
> that writes and executes Python code against a live Kubernetes cluster.

---

## Why This Matters

The CodeGenerator agent is the highest-risk component in KubeIntellect. When a user requests
a capability that no static tool covers, the system generates Python code and executes it
directly against the Kubernetes API. If that code is malicious, buggy, or manipulated,
the consequences on a live cluster can be severe (data loss, privilege escalation, secret
exfiltration, resource deletion).

The security model addresses two threat classes:

1. **LLM-generated malicious or hallucinated code** — the LLM could produce code that calls
   non-existent APIs, imports dangerous libraries, or attempts operations beyond the intended scope.
2. **Post-approval PVC tampering** — a tool file stored on the persistent volume could be
   modified after the user approved it, causing a different, potentially malicious version
   to load at the next startup.

---

## Security Pipeline Overview

Every generated tool passes through **five ordered controls** before it can execute on the cluster.
They apply at two distinct moments: **generation time** (before HITL) and **load time** (at startup or reload).

```
LLM generates code
        │
        ▼
[1] AST Hallucination Check        ← generation time
        │  Rejects unknown k8s API calls (sends back to LLM for correction)
        ▼
[2] Safeguard Review               ← generation time
        │  Flags high-risk patterns; annotates HITL prompt with risk warnings
        ▼
[3] HITL Approval Gate             ← human decision
        │  User reads code + risk annotations and approves or denies
        │  (PRIMARY CONTROL — no code reaches the cluster without human sign-off)
        ▼
[4] REPL Timeout (30s)             ← generation time, code testing phase
        │  Kills the test execution if it does not complete within 30 seconds
        ▼
   Tool saved to PVC + SHA-256 checksum stored in PostgreSQL registry
        │
        ▼  (at next startup or reload)
[5a] SHA-256 Integrity Check       ← load time
        │  Compares on-disk file hash against registry hash; skips on mismatch
        ▼
[5b] AST Static Analysis           ← load time
        │  Rejects blocked imports and dangerous calls before exec()
        ▼
   Tool loaded into agent
```

---

## Control 1 — AST Hallucination Check

**File:** `app/utils/ast_validator.py`
**When:** Immediately after code generation, before HITL.

The generated code is parsed by Python's `ast` module. Every attribute access on
`kubernetes.client` (e.g. `client.CoreV1Api`, `client.V1Pod`) is checked against a
whitelist of ~100 known classes and API objects. Any unrecognised name is flagged as a
potential hallucination and the error is fed back to the LLM for correction rather than
proceeding.

**What it prevents:** The LLM calling non-existent Kubernetes API methods (e.g.
`client.DeleteEverythingApi`) which would raise runtime errors or, in adversarial
scenarios, could be used to probe for unexpected behaviours.

**Limitation:** Covers only `kubernetes.client.*` attribute accesses. Does not analyse
general Python logic.

---

## Control 2 — Safeguard Review

**File:** `app/utils/self_refine.py` → `safeguard_review()`
**When:** After code generation and self-refinement, before HITL.

A heuristic rule engine scans the code for high-risk patterns:

- Unbounded delete operations (no namespace parameter — could affect the whole cluster)
- Cluster-wide destructive calls (`delete_namespace`, `drain_node`)
- Removal of resource limits (`resources=None`)
- Privilege escalation keywords (`cluster-admin`, `impersonate`)

If any pattern is found, the tool is **not blocked** — instead, risk annotations are
prepended to the HITL approval prompt so the human reviewer sees a clear warning before
deciding. This keeps the human in the loop rather than silently filtering.

**What it prevents:** A user approving a tool without realising it has cluster-wide blast radius.

---

## Control 3 — HITL Approval Gate (Primary Control)

**File:** `app/api/v1/endpoints/chat_completions.py`, `app/orchestration/workflow.py`
**When:** Before any generated code executes against the cluster.

This is the primary and most important control. LangGraph checkpoints the workflow state
in PostgreSQL and pauses. The generated code and any safeguard risk annotations are
presented to the user in the chat interface. The user must explicitly approve or deny.

- **Approve** → workflow resumes, code is tested in the REPL then saved.
- **Deny** → workflow terminates; no code is saved or executed.

No generated code reaches the Kubernetes API without human sign-off. This gate exists
regardless of whether the AST and safeguard checks passed.

**What it prevents:** Autonomous execution of LLM-generated code. Even a perfectly safe-looking
piece of code requires explicit human consent before it touches the cluster.

---

## Control 4 — REPL Timeout

**File:** `app/agents/tools/code_generator_tools.py` → `_run_python_repl()`
**When:** During the code testing phase (after HITL approval, before the tool is saved to PVC).

The generated code is executed in a `PythonREPL` instance inside a `ThreadPoolExecutor`
with a hard 30-second timeout (`_REPL_TIMEOUT_SECONDS = 30`). If execution does not
complete within 30 seconds, the thread is abandoned and the tool generation fails with
a `TimeoutError`.

**What it prevents:** Infinite loops, hung network calls, or long-running operations
that would block the agent indefinitely. Also limits the window of exposure during testing.

**Limitation:** The REPL runs in the same Python process. It is not a container or
sandbox — it shares the process memory space. The timeout is a hard time-kill, not
resource isolation.

---

## Control 5 — Load-Time Integrity (SHA-256 + Static Analysis)

**Files:**
- `app/utils/code_security.py` → `compute_code_checksum()`, `analyze_tool_code()`
- `app/orchestration/tool_loader.py` → `load_runtime_tools_from_pvc()`
**When:** At every application startup and on-demand tool reload.

Two checks run in sequence before `exec()` is called on any tool file:

### 5a — SHA-256 Checksum Verification

When a tool is saved to the PVC, its SHA-256 hash is computed immediately and stored in
the PostgreSQL tool registry (`tool_registry.file_checksum`). At load time, the file is
re-hashed and compared against the stored value. A mismatch causes the tool to be skipped
with an error log entry.

**What it prevents:** A tool file being modified on the PVC between approval and the
next startup — whether by an attacker with PVC access, a storage bug, or accidental
overwrite.

### 5b — AST Static Analysis

The code is parsed and checked for patterns that have no legitimate purpose in a
Kubernetes helper tool:

| Category | Examples |
|---|---|
| Blocked imports | `subprocess`, `socket`, `ctypes`, `requests`, `pickle`, `multiprocessing` |
| Blocked calls | `eval()`, `exec()`, `compile()`, `__import__()` |
| Blocked `os` methods | `os.system()`, `os.fork()`, `os.kill()`, `os.chmod()`, `os.chroot()` |
| Blocked file writes | `open()` with writable modes (`w`, `a`, `x`, `wb`, ...) |

If any violation is found, the tool is blocked from loading and the violation details
are logged. This analysis runs even for tools that passed all generation-time checks,
providing defence-in-depth against tools that may have been modified on disk.

**What it prevents:** A compromised or tampered tool file spawning shell processes,
opening network sockets, reading/writing arbitrary files, or executing dynamic code.

---

## What the Security Model Does NOT Cover

Being precise about limitations is important for operational trust:

| Gap | Notes |
|---|---|
| **Container-level isolation** | The REPL runs in the same process as the application — not in a container, gVisor, or WebAssembly sandbox. A sufficiently sophisticated attack could escape the AST check via indirect execution patterns. |
| **Kubernetes RBAC is the outer boundary** | Even if generated code escapes the AST check, it can only perform operations that the KubeIntellect ServiceAccount RBAC policy permits. The Kubernetes API is the true enforcement boundary. |
| **Prompt injection** | A malicious user could craft a query designed to trick the LLM into generating code that looks benign to the AST checker but has harmful semantics. The HITL gate is the primary defence here. |
| **LLM-level prompt injection** | An attacker with write access to cluster resources (e.g. ConfigMap values, pod logs) could inject instructions into data that the LLM reads during a diagnostic session. |

---

## Summary Table

| Control | When | Blocks | Primary threat |
|---|---|---|---|
| AST Hallucination Check | Generation | Hallucinated API calls → sent back to LLM | LLM errors |
| Safeguard Review | Generation | Flags dangerous patterns → annotates HITL | Missed blast-radius |
| HITL Approval | Generation | All code — no exceptions | Autonomous execution |
| REPL Timeout | Testing | Infinite loops, hung calls | Availability |
| SHA-256 Integrity | Load time | Tampered PVC files | Post-approval tampering |
| AST Static Analysis | Load time | Dangerous imports/calls | Compromised tool files |

---

## Key Files Reference

| File | Role |
|---|---|
| `app/utils/code_security.py` | AST static analysis + SHA-256 checksum |
| `app/utils/ast_validator.py` | K8s API whitelist hallucination check |
| `app/utils/self_refine.py` | Safeguard review (heuristic risk annotation) |
| `app/orchestration/tool_loader.py` | Load-time security gates (checksum + static analysis) |
| `app/agents/tools/code_generator_tools.py` | Generation pipeline (REPL timeout, HITL trigger) |
| `app/api/v1/endpoints/chat_completions.py` | HITL approval/deny handling |
