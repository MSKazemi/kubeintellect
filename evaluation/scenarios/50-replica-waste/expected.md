# Scenario 50 — Expected Behavior

## Ground Truth
- **Task:** Find over-replicated deployments (>2 replicas, <5m per replica CPU)
- **Success:** Combines kubectl (replica count) with Prometheus (CPU usage), shows correct math

## Expected Agent Path
1. kubectl get deployments -A: filter deployments with replicas > 2
2. Prometheus: sum(rate(container_cpu_usage_seconds_total[1h])) by deployment
3. per_replica_cpu = total_cpu / replicas; filter < 5m
4. Shows math: total / replicas = per_replica

## Minimum Passing Criteria
- [ ] Correctly gets replica count from kubectl (not Prometheus)
- [ ] Uses Prometheus for CPU over 1h window
- [ ] Shows the division: total_cpu ÷ replicas = per_replica_cpu
- [ ] Units consistent (millicpu)

## Critical Failures
- Uses kubectl top (no 1h window)
- Shows total CPU without per-replica breakdown
