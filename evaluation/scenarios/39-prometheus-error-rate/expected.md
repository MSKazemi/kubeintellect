# Scenario 39 — Expected Behavior

## Ground Truth

- **Query type:** Observability — Prometheus metrics
- **No fault injected.**
- **Correct answer:** HTTP error rate (4xx + 5xx responses) for the kubeintellect service over the last 30 minutes.

## Expected Agent Path

1. Calls `query_prometheus` with PromQL such as:
   - `rate(http_requests_total{status=~"[45].."}[5m])` or
   - `sum(rate(http_requests_total{status=~"[45].."}[5m])) / sum(rate(http_requests_total[5m]))`
2. Reports the error rate as a percentage or requests/second with time window.
3. If no HTTP metrics are instrumented: reports "no http_requests_total metric found — application may not expose Prometheus metrics."
4. Does not fabricate numbers if Prometheus returns no data.

## Minimum Passing Criteria

- [ ] Uses `query_prometheus` — not kubectl or logs
- [ ] Constructs a meaningful PromQL involving error status codes (4xx/5xx)
- [ ] States the 30-minute time window
- [ ] Reports actual values or explicitly states no data was found
- [ ] Distinguishes 4xx (client errors) from 5xx (server errors) if data allows

## Critical Failures

- Reports an error rate without querying Prometheus (hallucination)
- Queries Loki for metrics (wrong tool)
- Returns a number without stating what time window or metric it came from
