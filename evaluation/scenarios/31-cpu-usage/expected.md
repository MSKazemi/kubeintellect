# Scenario 31 — Expected Behavior

## Ground Truth

- **Query type:** Observability — resource metrics
- **No fault injected.**
- **Correct answer:** Top CPU-consuming pods with current millicores usage, sorted descending.

## Expected Agent Path

1. Prefers `query_prometheus` with a PromQL like `topk(10, sum by (pod, namespace) (rate(container_cpu_usage_seconds_total[5m])))` for accurate data.
2. Falls back to `run_kubectl top pods -A --sort-by=cpu` if Prometheus is unavailable.
3. Lists top consumers: pod name, namespace, CPU usage (millicores or percentage).
4. Handles gracefully if metrics-server or Prometheus is unavailable.

## Minimum Passing Criteria

- [ ] Uses Prometheus or `kubectl top` — not `kubectl describe` (which has no live metrics)
- [ ] Covers all namespaces
- [ ] Shows CPU values with units (m for millicores, or %)
- [ ] Lists at least top 5 consumers (or all pods if fewer than 5)
- [ ] Handles "no metrics available" gracefully if metrics-server is down

## Critical Failures

- Uses `kubectl describe pod` to report CPU (shows requests/limits, not actual usage)
- Hallucinates CPU values
- Returns only one namespace
