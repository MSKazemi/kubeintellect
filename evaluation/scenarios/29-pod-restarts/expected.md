# Scenario 29 — Expected Behavior

## Ground Truth

- **Query type:** Observability + light diagnosis
- **No fault injected.** Agent reads live pod state.
- **Correct answer:** Identify pods with elevated restart counts, state the count, and diagnose likely cause from events/logs.

## Expected Agent Path

1. Gets pod list with restart counts: `kubectl get pods -A` — look for RESTARTS > 3.
2. For pods with high restarts, checks events and/or logs to identify cause.
3. If no pods have high restarts, reports clearly: "No pods with elevated restart counts found."

## Minimum Passing Criteria

- [ ] Checks all namespaces for restart counts
- [ ] Identifies what "high" means (context-dependent, typically >3–5)
- [ ] For any high-restart pod: names the pod, count, namespace, and suspected cause
- [ ] If no high-restart pods: says so clearly (not silence or "I cannot check")
- [ ] Does not recommend restarting pods without identifying cause

## Critical Failures

- Only checks one namespace
- Reports restart counts but makes no attempt to diagnose cause
- Misidentifies restart count field (confuses READY with RESTARTS)
