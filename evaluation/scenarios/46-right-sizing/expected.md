# Scenario 46 — Expected Behavior

## Ground Truth
- **Task:** Identify over-provisioned pods (CPU request > 3× actual) and suggest right-sized values
- **Success:** Agent queries both kubectl (for requests) and Prometheus (for actual usage), names specific pods with numbers

## Expected Agent Path
1. Gets resource requests: kubectl get pods -n kubeintellect -o json (or describe)
2. Queries Prometheus: rate(container_cpu_usage_seconds_total{namespace="kubeintellect"}[1h])
3. Computes ratio for each container, filters >3×
4. Suggests specific new request values with reasoning

## Minimum Passing Criteria
- [ ] Uses Prometheus metrics (not just kubectl top, which lacks history)
- [ ] Shows actual pod/container names from the live cluster
- [ ] Computes or estimates the ratio (not vague "seems high")
- [ ] Suggests specific numeric values for updated requests

## Critical Failures
- Reports no data without trying Prometheus
- Gives generic advice without querying actual metrics
- Hallucinates pod names not in the cluster
