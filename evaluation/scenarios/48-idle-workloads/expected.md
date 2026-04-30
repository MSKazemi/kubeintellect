# Scenario 48 — Expected Behavior

## Ground Truth
- **Task:** Identify idle deployments (CPU < 1m) for potential scale-to-zero
- **Success:** Queries Prometheus for CPU usage, lists specific deployments with names and replica counts

## Expected Agent Path
1. Prometheus: rate(container_cpu_usage_seconds_total[1h]) grouped by deployment/namespace
2. kubectl get deployments -A: cross-reference current replica counts
3. Filters to CPU < 1m (0.001 core), lists candidates with namespace + name + replicas

## Minimum Passing Criteria
- [ ] Uses Prometheus (not kubectl top — lacks 1h window)
- [ ] Shows deployment names and namespaces (not just pod names)
- [ ] Shows current replica count for each candidate
- [ ] If no idle workloads, correctly reports none found

## Critical Failures
- Recommends scaling system/kube-system workloads
- Reports near-zero usage without Prometheus query
