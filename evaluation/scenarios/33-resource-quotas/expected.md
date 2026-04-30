# Scenario 33 — Expected Behavior

## Ground Truth

- **Query type:** Observability — quota utilization
- **No fault injected.**
- **Correct answer:** Resource quotas across all namespaces with used vs. hard limits, flagging any namespace above ~80% utilization.

## Expected Agent Path

1. Calls `run_kubectl get resourcequota -A` or `kubectl describe resourcequota -A`.
2. For each quota: shows namespace, resource type (cpu, memory, pods), used vs. hard limit, and utilization %.
3. Flags namespaces approaching limits (>80% used on any resource).
4. If no quotas defined: reports "no ResourceQuota objects found."

## Minimum Passing Criteria

- [ ] Covers all namespaces (not just one)
- [ ] Shows used AND hard limit for each resource (not just one or the other)
- [ ] Calculates or estimates utilization percentage
- [ ] Flags any namespace at risk (>80%) — or says "none approaching limits"
- [ ] Does not confuse LimitRange with ResourceQuota

## Critical Failures

- Only reports quotas for one namespace
- Reports limits but not actual usage (cannot assess risk)
- Confuses LimitRange with ResourceQuota
