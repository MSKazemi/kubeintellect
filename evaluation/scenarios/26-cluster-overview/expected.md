# Scenario 26 — Expected Behavior

## Ground Truth

- **Query type:** Observability — read-only cluster state
- **No fault injected.** Agent reads live cluster state.
- **Correct answer:** List of all pods across all namespaces with name, namespace, status, ready count, restarts, and node.

## Expected Agent Path

1. May answer directly from the cluster snapshot (which already includes pod list).
2. If snapshot is insufficient, calls `run_kubectl get pods -A` or `run_kubectl get pods --all-namespaces`.
3. Presents results grouped by namespace or as a sorted table.
4. Does **not** make unnecessary additional tool calls.

## Minimum Passing Criteria

- [ ] Covers **all** namespaces (not just one)
- [ ] Shows pod name, namespace, and status for each pod
- [ ] Includes ready count (e.g. `1/1`) or restart count
- [ ] Does not confuse namespaces or omit system namespaces (kube-system, monitoring, etc.)
- [ ] Response is readable — not raw JSON dump

## Critical Failures

- Only lists pods from one namespace
- Returns empty list when pods exist
- Hallucinates pod names not present in the cluster
- Makes write operations to answer a read query
