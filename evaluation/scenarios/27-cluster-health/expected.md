# Scenario 27 — Expected Behavior

## Ground Truth

- **Query type:** Observability — cluster-wide health summary
- **No fault injected.** Agent assesses current cluster state.
- **Correct answer:** Health status covering nodes, namespaces, pod health, and any active problems.

## Expected Agent Path

1. Reads cluster snapshot (nodes + pods + events) — may be sufficient.
2. If more detail needed: `kubectl get nodes`, `kubectl get pods -A`, `kubectl get events -A --field-selector type=Warning`.
3. Reports: node count + ready status, namespace count, unhealthy pod count, recent warnings.
4. Gives a clear "healthy / degraded / critical" verdict.

## Minimum Passing Criteria

- [ ] Reports node count and whether all nodes are Ready
- [ ] Reports any pods not in Running/Completed state
- [ ] Mentions any active Warning events if present
- [ ] Gives an overall health verdict (healthy or degraded)
- [ ] Does not over-call tools when snapshot is sufficient

## Critical Failures

- Reports cluster healthy when nodes are NotReady
- Ignores pods in CrashLoopBackOff or Pending
- Makes no tool calls and gives only generic advice
