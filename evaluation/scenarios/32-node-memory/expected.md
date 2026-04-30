# Scenario 32 — Expected Behavior

## Ground Truth

- **Query type:** Observability — node memory state
- **No fault injected.**
- **Correct answer:** Node memory utilization and whether any node is under memory pressure.

## Expected Agent Path

1. Checks node conditions: `kubectl describe nodes` or `kubectl get nodes -o wide` — look for `MemoryPressure=True`.
2. Gets utilization: `kubectl top nodes` or Prometheus `node_memory_MemAvailable_bytes`.
3. Reports: each node's memory usage (used/total or %), and MemoryPressure condition.
4. If no memory pressure: states so clearly.

## Minimum Passing Criteria

- [ ] Checks `MemoryPressure` condition on each node (not just general pod health)
- [ ] Reports actual memory utilization numbers (not just "looks fine")
- [ ] Covers all nodes, not just one
- [ ] Handles unavailable metrics gracefully

## Critical Failures

- Checks pod memory limits instead of node memory utilization
- Only reports `MemoryPressure=False` without utilization numbers
- Confuses node memory with pod memory limits
