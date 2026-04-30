# Scenario 40 — Expected Behavior

## Ground Truth

- **Query type:** Observability — Prometheus latency percentiles
- **No fault injected.**
- **Correct answer:** P50, P95, and P99 request latency for the kubeintellect API over the last hour.

## Expected Agent Path

1. Calls `query_prometheus` three times (or one query with multiple series) using `histogram_quantile`:
   - `histogram_quantile(0.50, rate(http_request_duration_seconds_bucket[1h]))`
   - `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[1h]))`
   - `histogram_quantile(0.99, rate(http_request_duration_seconds_bucket[1h]))`
2. Reports values in ms (converting from seconds if needed).
3. If no histogram metric found: reports "no http_request_duration_seconds_bucket metric found" — does not fabricate values.
4. Notes if P99 > 1s as potentially problematic.

## Minimum Passing Criteria

- [ ] Uses `query_prometheus` — not kubectl or logs
- [ ] Queries all three percentiles: P50, P95, P99
- [ ] Uses `histogram_quantile` or equivalent PromQL pattern
- [ ] States the 1-hour time window
- [ ] Reports values with units (ms or s)
- [ ] If no data: says so explicitly rather than returning zeros or silence

## Critical Failures

- Reports latency percentiles without querying Prometheus (hallucination)
- Only reports one percentile when asked for three
- Returns raw PromQL output without interpreting the values
- Queries logs or kubectl for latency data
