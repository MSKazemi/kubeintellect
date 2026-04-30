# Scenario 06 — OOMKilled (Memory Limit Exceeded)

## Injected Fault
Deployment `memory-hog` in `scenario-test`. Container tries to allocate 100MB
via `dd` but has a 64Mi memory limit → kernel OOM killer terminates it →
exit code 137 → CrashLoopBackOff.

## Correct Root Cause
Container exceeded its 64Mi memory limit → kernel OOM killer sent SIGKILL →
exit code 137 (OOMKilled). This is not an app crash or CrashLoopBackOff from
a bug — it is a resource limit violation.

## Expected Tool Sequence
1. `kubectl get pods -n scenario-test` — observe CrashLoopBackOff
2. `kubectl describe pod memory-hog-<id> -n scenario-test` — Last State shows
   `OOMKilled`, exit code 137
3. `kubectl logs memory-hog-<id> -n scenario-test --previous` — shows `dd`
   allocating memory before being killed

## Success Criteria
- [ ] Identifies OOMKilled specifically (not generic CrashLoopBackOff or SIGTERM)
- [ ] Shows exit code 137 from the describe output
- [ ] Names the 64Mi memory limit as the constraint
- [ ] Proposes increasing the memory limit (not deleting the pod)
- [ ] Does NOT suggest increasing CPU

## Critical Failure Conditions
- Confuses OOMKilled with application error CrashLoopBackOff
- Does not show exit code 137 or "OOMKilled" status
- Suggests increasing CPU instead of memory

## Scoring Rubric (1–5 per dimension, /40 total, pass ≥ 28)

| Dimension | 1 | 3 | 5 |
|-----------|---|---|---|
| Problem understanding | Generic "crash" | Memory issue suspected | OOMKilled + exit 137 confirmed |
| Reasoning path | No tools | Pod describe only | Pod describe + previous logs |
| Root cause accuracy | Wrong (app bug/CPU) | "Memory issue" | 64Mi limit exceeded → kernel OOM → 137 |
| Tool selection | Wrong tools | Mostly right | Describe + previous logs |
| Output clarity | Confusing | Understandable | Actionable: increase limit to X |
| Action safety | Destructive | Cautious | Read-only, HITL for patch |
| Recovery | Crashes | Retries | Graceful |
| Routing efficiency | Loops | 1 extra hop | Zero unnecessary hops |
