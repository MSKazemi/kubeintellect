# Scenario 34 — Expected Behavior

## Ground Truth

- **Query type:** Observability — resource governance audit
- **No fault injected.**
- **Correct answer:** List of containers/pods that have no CPU or memory limits set.

## Expected Agent Path

1. Calls `run_kubectl get pods -A -o json` (or yaml) and inspects `spec.containers[].resources.limits`.
2. Identifies containers where `limits` is absent or empty for cpu/memory.
3. Reports: pod name, namespace, container name, and which limits are missing.
4. If all pods have limits: reports "all pods have resource limits set."

## Minimum Passing Criteria

- [ ] Checks all namespaces
- [ ] Distinguishes between missing CPU limits and missing memory limits
- [ ] Names the specific container, not just the pod
- [ ] Includes system namespaces (kube-system pods often lack limits — this is expected, but should be noted)
- [ ] If all pods have limits: says so clearly

## Critical Failures

- Only checks one namespace
- Checks requests instead of limits (different field)
- Reports pods with limits=0 as "having limits" (0 is not a set limit)
- Hallucinates missing limits for pods that have them
