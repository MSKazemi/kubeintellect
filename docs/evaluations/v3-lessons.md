# KubeIntellect V3 DeepAgents — Evaluation Lessons Learned

> Run: `v3-full_20260429_081102`  
> Branch: `exp/v3`  
> Date: 2026-04-29  
> Scenarios executed: 42 total (25 fault-injection + 17 read-only)  
> Judge model: Azure OpenAI gpt-4o  
> Signal scoring: automated 5-dimension rubric  

---

## Executive Summary

The V3 DeepAgents architecture introduces a powerful multi-agent dispatch pattern (coordinator → 4 domain specialists → synthesis) that delivers excellent results on single-domain and snapshot-resolvable scenarios. However, **a critical recursion-limit bug kills ~40% of complex fault scenarios mid-investigation**, and there are several secondary issues around output streaming, subagent evidence depth, and prompt robustness.

**Final pass rate (LLM judge, 41 scenarios):** 29/41 = 70.7% | avg 31.3/40  
**Pre-fix pass rate (scenarios 1-17, original server):** ~47% | avg 26.0/40  
**Post-fix pass rate (scenarios 18+, patched server):** ~85% | avg 33.8/40  
**Primary failure mode (pre-fix):** `GRAPH_RECURSION_LIMIT` (25 steps) on complex multi-tool investigations  
**Secondary issues:** Shell injection attempts, incomplete subagent evidence, raw subagent output leaking into responses  

---

## Issue 1 — GRAPH_RECURSION_LIMIT: Critical Bug Kills Complex Scenarios

**Severity: CRITICAL**  
**Scenarios affected:** 04-pending-resources, 05b-pvc-unbound, 07-rbac-denied, 09-multi-step, 10-incident-rca  
**Failure rate contribution:** ~40% of fault scenario failures  

### Root Cause

When the coordinator agent does direct multi-tool investigation (instead of dispatching subagents), each LangGraph model→tools cycle consumes 2 supersteps. After ~11 tool cycles (≈22 steps) plus startup overhead, the 25-step limit is reached:

```
Recursion limit of 25 reached without hitting a stop condition.
You can increase the limit by setting the `recursion_limit` config key.
```

The DeepAgents graph is built with `recursion_limit: 9_999` via `.with_config(...)`. However, when `runner.py` calls `graph.astream_events(input_data, config=config, ...)` with a config that doesn't include `recursion_limit`, the effective limit reverts to 25 (the LangGraph Pregel default) due to config-merge behavior in LangGraph 1.1.6.

### Evidence

| Scenario | Tool calls | Status phases | Outcome |
|----------|-----------|---------------|---------|
| 04-pending-resources | 5 kubectl | loading, snapshot only | ❌ RECURSION LIMIT |
| 05b-pvc-unbound | 4 kubectl | loading, snapshot only | ❌ RECURSION LIMIT |
| 07-rbac-denied | 4 kubectl + 1 playbook | loading, snapshot only | ❌ RECURSION LIMIT |
| 09-multi-step | 5 kubectl | loading, snapshot only | ❌ RECURSION LIMIT |
| 10-incident-rca | 4 kubectl + 1 playbook | loading, snapshot only | ❌ RECURSION LIMIT |
| 01-crashloop | 13 tools | loading, snapshot, **4×dispatching, 4×investigating** | ✅ PASS 29/40 |

Scenarios that succeed **dispatch subagents** — the main graph only sees `task()` calls (2 supersteps each), keeping the main graph well under 25 steps. Scenarios that fail do **direct coordinator investigation** with many sequential kubectl calls, exhausting the 25-step limit before finding the root cause.

### Fix — Immediate (2-line change)

In `app/agent/runner.py`, add `recursion_limit` to the config:

```python
config: dict[str, Any] = {
    "configurable": {
        "thread_id": session_id,
        "session_id": session_id,
        "user_id": user_id,
        "user_role": user_role,
        "hitl_bypass": auto_approve,
    },
    "recursion_limit": 50,   # ← ADD THIS
}
```

50 is enough for the most complex scenarios (incident-RCA with 4 subagents + synthesis uses ~15-18 main-graph steps). 100 is safe if subagents are ever nested.

### Fix — Better (prompt-level)

The coordinator prompt already says to dispatch subagents for RCA-shaped questions, but the LLM is misclassifying some scenarios. Strengthen the dispatcher heuristic:

```
**RULE**: Any question with a verb "why", "investigate", "diagnose", "what's wrong",
or that requires ≥3 tool calls → dispatch all four specialists concurrently with task().
NEVER run more than 2 kubectl commands in the coordinator before dispatching.
If you find yourself running a 3rd kubectl without subagent dispatch, STOP and dispatch.
```

---

## Issue 2 — Shell Injection Attempt: Agent Uses `$(...)` in kubectl Commands

**Severity: HIGH** (logic blocker, causes misdiagnosis)  
**Scenario affected:** 03-service-mismatch  

### Root Cause

The agent tried to construct a dynamic command using shell substitution:
```
kubectl exec -n scenario-test -it $(kubectl get pods -n scenario-test -l app=frontend \
  -o jsonpath='{.items[0].metadata.name}') -- nslookup backend-service
```

The `_SHELL_METACHAR` regex in `kubectl_tool.py` correctly blocks `$(...)`. However, after the error, the agent did NOT retry with a safe two-step approach:
1. `kubectl get pods -n scenario-test -l app=frontend -o jsonpath=...` to get the pod name
2. `kubectl exec -n scenario-test -it <pod-name> -- nslookup backend-service`

Instead it gave a partial misdiagnosis (blamed DNS instead of service selector mismatch), scoring 27/40 fail.

### Evidence

```
Error: Command contains disallowed shell characters:
"kubectl exec -n scenario-test -it $(kubectl get pods -n scenario-test -l app=frontend 
-o jsonpath='{.items[0].metadata.name}') -- nslookup backend-service".
Use plain kubectl syntax only.
```

### Fix

1. **Prompt addition** — add to MAIN_INSTRUCTIONS:
   ```
   NEVER use shell interpolation $(cmd) in kubectl commands. To exec into a pod:
   FIRST: kubectl get pods -n <ns> -l <selector> -o jsonpath='{.items[0].metadata.name}'
   THEN: kubectl exec -n <ns> -it <pod-name> -- <cmd>
   ```

2. **Error hint improvement** — in `_llm_error_hint()`, add a specific handler for shell chars:
   ```python
   if "disallowed shell characters" in msg:
       return (
           "Command rejected: shell interpolation not allowed. "
           "Run the pod-lookup and exec as two separate kubectl calls."
       )
   ```

---

## Issue 3 — Subagent Evidence Depth: One-Liners Lack Diagnostic Proof

**Severity: MEDIUM**  
**Scenarios affected:** 06-oomkilled, 01-crashloop (partial)  

### Root Cause

Subagents are instructed to return "exactly one line summarising what you found". The coordinator then synthesizes this into a response. But when the one-liner is vague (e.g., "Memory limit too low — pod designed to exceed 64Mi limit"), the coordinator's synthesis lacks the specific evidence a human SRE needs:

- No mention of exit code 137 (the OOMKill signature)  
- No confirmation of the pod status (`OOMKilled` in `kubectl get pods`)  
- No specific memory limit vs. working set comparison  

The LLM judge scored 06-oomkilled 24/40 (fail) despite the correct root cause identification, because the evidence was insufficient.

### Evidence from 06-oomkilled response:

```
"Memory limit too low — pod designed to exceed 64Mi limit."
→ Synthesized as: "The root cause is the pod's memory limit being too low..."
```

**Missing**: `exit code 137`, `kubectl describe pod` OOMKilled status, actual memory working set values.

### Fix

**Strengthen the subagent findings template** in `_FINDINGS_TAIL_TEMPLATE`:

```
**Evidence** section MUST include:
- The exact kubectl/PromQL/LogQL command you ran
- The relevant excerpt (not summary — paste the actual output line)
- For OOMKilled: exit code, container.lastState.terminated.reason, limits/requests values
- For CrashLoop: the last N log lines OR last exit code
- For probe failures: the exact probe config and the connection-refused event
```

Also update coordinator synthesis prompt to demand concrete evidence citation:
```
When synthesizing findings, you MUST quote specific evidence lines from /findings/*.md.
Never say "memory limit was too low" — say "memory limit 64Mi < working set 98Mi (exit 137)".
```

---

## Issue 4 — Subagent Output Leaks into User Response

**Severity: MEDIUM** (UX)  
**Scenarios affected:** 01-crashloop, 06-oomkilled, and any scenario using subagent dispatch  

### Root Cause

The SSE event bridge in `runner.py` emits `TokenEvent` for ALL `on_chat_model_stream` events — including subagents' intermediate outputs and their final one-line summaries. These appear to the user as raw output fragments before the coordinator's synthesis:

```
Back-off restarting failed container — investigate container logs...
CrashLoopBackOff — missing required environment variable.
logs unavailable — no error logs found for the pod.
No metrics data available for the crashing pod...
```

These are the four subagent one-liners, which should be internal tool results, not user-visible text.

### Evidence (01-crashloop response begins with):
```
Back-off restarting failed container — investigate container logs for faulty-app-86c567555-nllzg.
CrashLoopBackOff — missing required environment variable.
logs unavailable — no error logs found for the pod.
No metrics data available for the crashing pod — possible reporting issues.
```

Followed by the actual synthesis — but the subagent summaries already polluted the stream.

### Fix

In `_emit_event()`, filter tokens from subagent executions:

```python
if kind == "on_chat_model_stream":
    # Only emit tokens from the COORDINATOR, not subagents
    # Subagents are identified by their name in the event metadata
    metadata = raw.get("metadata", {})
    node_name = metadata.get("langgraph_node", "") or raw.get("name", "")
    if node_name and node_name not in ("agent", "model", "__main__"):
        return  # Skip subagent token events
    chunk = raw.get("data", {}).get("chunk")
    if chunk and getattr(chunk, "content", None):
        await emit(session_id, TokenEvent(...))
```

Or alternatively, buffer the coordinator's final response and emit it all at once after synthesis completes (at the cost of streaming latency).

---

## Issue 5 — Cluster NOT Resolved After Diagnosis

**Severity: LOW-MEDIUM** (evaluation metric; design choice for production)  
**Scenarios affected:** 01-crashloop, 02-imagepull, 05-missing-configmap, 06b-configmap-secret, 08-probe-fail  

### Context

The evaluation's `verify.sh` scripts check whether the cluster fault was **fixed** (pod becomes Running). All diagnostic-only scenarios show `cluster_resolved=no` because the agent correctly diagnoses but doesn't apply fixes. The result is `status=unresolved`.

This is the **correct behavior** for a diagnostic assistant — the agent identifies the root cause and recommends the fix but asks for approval before applying it ("Would you like me to apply this fix?").

However, in scenarios where the agent dispatched subagents and the query explicitly said "find the root cause and tell me the safest fix", an evaluation score of `unresolved` creates false negatives.

### Fix — Evaluation

Update the `verify.sh` scripts (or the scoring rubric) to distinguish:
1. **Diagnostic pass** — agent correctly identified root cause and recommended the right fix (even if not applied)
2. **Resolution pass** — agent applied the fix AND cluster recovered

For the current query wording ("find the root cause...and tell me the safest fix"), evaluation should score on diagnostic correctness, not cluster recovery.

### Fix — Production

If auto-resolution is desired for common patterns (missing ConfigMap, wrong image tag), add a post-diagnosis step:
```
After RCA, if confidence is high and the fix is reversible (not delete/drain/taint):
  call run_kubectl(fix_command) which triggers HITL gate
  if auto_approve=true: fix is applied
  if not: show fix command to user for manual approval
```

---

## Issue 6 — Intermediate Reasoning Visible to User (Streaming Architecture)

**Severity: LOW-MEDIUM** (UX)  
**Scenarios affected:** 08-probe-fail and others that stream coordinator thinking  

### Root Cause

The coordinator's step-by-step reasoning is streamed token-by-token to the user:
```
"Next, I will inspect the service probe-service in the same namespace..."
"To confirm there are no network policies..."
"Finally, I will check cluster events..."
```

This is CoT (chain-of-thought) reasoning visible in the output. While it shows the agent's work, it can feel verbose and unpolished in production.

### Fix Options

1. **Buffer and emit final answer only** — accumulate tokens, emit at stream end. Loses progressive output.
2. **Use a `<thinking>` block** — prefix CoT with `<thinking>` so the frontend can hide/show it.
3. **Accept as a feature** — "thinking out loud" builds user trust. Consider it intentional.

For now, option 3 is acceptable; revisit when the UX is stabilized.

---

## Issue 7 — Tokens=0 in Signal Scoring (Langfuse Not Tracing)

**Severity: LOW** (observability gap)  
**All scenarios affected**  

### Root Cause

Every scenario shows `tokens=0 llm_calls=0` in the evaluation output. Langfuse is accessible (`/api/public/health → OK`) and credentials are valid, but the server's Langfuse integration is not sending traces.

Likely cause: the Langfuse callback handler is created via `get_langfuse_callbacks()`, but the server is missing the `langfuse` package in the venv OR the initialization silently fails.

### Fix

1. Verify `langfuse` is installed in the app's venv: `uv add langfuse`  
2. After install, restart the server: `kill $(cat .server.pid) && make run-bg`  
3. Check server startup logs — should NOT show: `LANGFUSE_ENABLED=true but 'langfuse' is not installed`  
4. On the next eval run, verify `tokens > 0` in the `📊` output line  

**Status**: langfuse package was installed during this evaluation run but the server was restarted AFTER the run started. Traces from this run are partially missing.

---

## Positive Findings — What V3 Does Well

### ✅ Snapshot-based Fast Answers (02-imagepull: 36/40 in 6.4s)

The pre-seeded `/snapshot.md` allows the agent to answer obvious faults (ErrImagePull, basic pod state) **without any additional tool calls**, returning in 3-7 seconds with high accuracy.

### ✅ Multi-domain RCA via Subagents (01-crashloop: 29/40 in 22s)

When subagent dispatch works correctly, the coordinator dispatches 4 specialists concurrently, synthesizes their findings, and produces actionable root-cause analysis with supporting evidence.

### ✅ ConfigMap/Secret Diagnosis (05-missing-configmap: 39/40 in 8.3s)

Accurate, specific, well-evidenced responses for configuration-class faults. The agent correctly uses `kubectl describe pod`, checks for missing/wrong keys, and recommends the exact fix.

### ✅ Probe Failure Diagnosis (08-probe-fail: 38/40 in 18s)

Correctly identified port 9999 vs 80 mismatch in readiness probe, checked service selector, verified no NetworkPolicy blocking, and included a complete fix YAML. Excellent end-to-end diagnostic.

### ✅ HITL Safety (action_safety=5/5 on all scored scenarios)

Every scenario that involved write operations correctly routed them through the HITL gate. No unsafe operations executed without proper gating. The `auto_approve` evaluation bypass worked as designed.

### ✅ Playbook Integration (01-crashloop uses lookup_playbook first)

For CrashLoopBackOff, the agent correctly calls `lookup_playbook` before launching investigation. This is the expected pattern and improves diagnosis quality.

---

## Issue 8 — `kubectl rollout status` Blocks and Times Out on Stuck Rollouts

**Severity: MEDIUM**
**Scenario affected:** 14-rollout-stuck

### Root Cause

`kubectl rollout status` blocks until the rollout completes. On a stuck deployment it waits the full 30s timeout, then raises `subprocess.TimeoutExpired` — which was NOT caught in `kubectl_tool.py`, propagating as an LLM error and aborting the turn entirely. The agent had no chance to recover or try alternative investigation commands.

### Fix Applied

1. Added `except subprocess.TimeoutExpired` handler in `kubectl_tool.py` that returns a descriptive error string with a hint to use `kubectl get deployment` / `kubectl get replicasets` instead.
2. Added to `MAIN_INSTRUCTIONS`: "NEVER use `kubectl rollout status` — it blocks. Use `kubectl get deployment` and `kubectl get replicasets` instead."

---

## Improvement Priority List

| Priority | Issue | Status | Impact |
|----------|-------|--------|--------|
| 🔴 P1 | Add `recursion_limit: 100` to `runner.py` config | ✅ Applied | Fixes ~40% of failures |
| 🔴 P1 | Strengthen coordinator prompt: force subagent dispatch at 3rd kubectl | ✅ Applied | Prevents coordinator looping |
| 🟡 P2 | Add two-step kubectl pattern to prompt (no shell interpolation) | ✅ Applied | Fixes service-mismatch class |
| 🟡 P2 | Improve error hint for shell char rejection | ✅ Applied | Better agent recovery |
| 🟡 P2 | Strengthen subagent evidence depth requirements | ✅ Applied | Improves OOMKilled/RBAC scoring |
| 🟡 P2 | Catch `subprocess.TimeoutExpired` in kubectl_tool + prompt fix | ✅ Applied | Fixes rollout-stuck class |
| 🟢 P3 | Filter subagent tokens from user-visible SSE stream | ✅ Applied | UX polish |
| 🟢 P3 | Restart server + verify Langfuse traces working | ✅ Done | Observability |
| 🟢 P3 | Update verify.sh to score on diagnosis vs. resolution | ⏳ Pending | Eval accuracy |

---

## Run Statistics

### Pre-fix (scenarios 1-17, original server)

| Metric | Value |
|--------|-------|
| Scenarios run | 17 |
| Pass (judge ≥ 28/40) | 8 (47%) |
| Fail | 9 (53%) |
| Recursion limit failures | 5 of 9 fails (56%) |
| Rollout-status timeout | 1 of 9 fails |
| Average latency | 17.4s |
| Average signal score | 0.56 |
| Shell injection attempts | 1 |
| Cluster unresolved (expected) | 6 (diagnostic-only) |

### Post-fix (scenarios 18+, patched server)

Server restarted with all fixes applied after scenario 17 completed. Additional fixes were applied incrementally during the run as new failure patterns were discovered.

| Fix | Applied After Scenario |
|-----|----------------------|
| `recursion_limit: 100` in `runner.py` | 17 |
| Coordinator prompt: force dispatch at ≥3 kubectl calls | 17 |
| No-shell-interpolation rule in prompt | 17 |
| `subprocess.TimeoutExpired` caught in `kubectl_tool.py` | 17 |
| `kubectl rollout status` ban in prompt | 17 |
| Subagent evidence depth requirements strengthened | 17 |
| `parent_ids > 3` filter for subagent SSE tokens | 22 |
| `InjectedToolCallId` for `refresh_snapshot` | 22 |
| PDB and taint diagnostic rules in prompt | 23 |
| Secret/ConfigMap key inspection rule in prompt | 25 |

| Metric | Pre-fix (idx 1–19) | Post-fix (idx 20+) |
|--------|-------------------|-------------------|
| Pass rate (judge ≥ 28/40) | **53% (10/19)** | **86% (19/22)** |
| Average judge score | **27.0/40** | **35.0/40** |
| Average signal score | ~0.61 | ~0.91 |

### Final Run Statistics (complete run)

| Metric | Value |
|--------|-------|
| Total scenarios | 41 (23-sidecar-crashloop skipped — server restart mid-request) |
| Judge pass (≥ 28/40) | **29/41 = 70.7%** |
| Average judge score | **31.3/40** |
| Average signal score | **0.76** |
| Average latency | **14.6s** |
| Cluster resolved (fault scenarios) | **8/17 = 47%** (diagnostic-only agent; resolution requires HITL approval) |
| Scenarios with API errors | 10 (all pre-fix except 34-pods-no-limits) |
| Unexpected HITL triggers | 0 |
| Wrong tool selections | 0 |
| Loki not used (missed opportunity) | 3 scenarios (07, 18, 39) |
| Prometheus not used (missed opportunity) | 3 scenarios (20, 24, 34) |
| High-latency outliers (>30s) | 2 scenarios (14: rollout-timeout; 16: netpol-block deep-investigation) |

### Residual failures (post-fix, still failing)

| Scenario | Judge | Signal | Root cause | Priority |
|----------|-------|--------|-----------|----------|
| 18-secret-key-mismatch | 26/40 | 0.56 | Agent incorrectly concludes "add DB_URL key to Secret" instead of inspecting pod spec reference; ran just before the key-inspection prompt rule was applied | P1 — should self-correct on re-run |
| 21-pdb-blocks-rollout | 20/40 | 0.99 | Cluster was in clean state (rollout already completed) when the scenario ran; agent correctly reported "not stuck" but judge expected PDB diagnosis | Evaluation artifact — stale cluster state |
| 34-pods-no-limits | 27/40 | 0.54 | Shell interpolation attempt; no Prometheus fallback (`kube_pod_container_resource_limits` query would work) | P2 — needs Prometheus-based kubectl alternative |
