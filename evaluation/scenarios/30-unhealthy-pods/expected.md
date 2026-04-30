# Scenario 30 — Expected Behavior

## Ground Truth

- **Query type:** Observability — pod health filter
- **No fault injected.** Agent reads live pod state across the cluster.
- **Correct answer:** List of pods not in Running, Completed, or Succeeded state, with reason/status.

## Expected Agent Path

1. Gets all pods: `kubectl get pods -A` or reads from snapshot.
2. Filters to non-Running/non-Completed statuses: Pending, CrashLoopBackOff, Error, ImagePullBackOff, OOMKilled, Init:, Terminating, etc.
3. For each unhealthy pod: shows namespace, name, status, and restart count.
4. If cluster is fully healthy: reports "all pods are Running or Completed."

## Minimum Passing Criteria

- [ ] Covers all namespaces
- [ ] Lists every pod not in Running/Completed/Succeeded
- [ ] Shows the exact status (not just "not healthy")
- [ ] If all pods healthy: says so clearly
- [ ] Does not trigger additional investigation unless asked

## Critical Failures

- Misses pods in Pending or Init: states
- Only reports CrashLoopBackOff and ignores other failure modes
- Reports healthy pods as unhealthy (false positives)
