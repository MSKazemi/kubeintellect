# Scenario 38 — Expected Behavior

## Ground Truth

- **Query type:** Observability — application log analysis
- **No fault injected.** Query targets KubeIntellect app logs specifically.
- **Correct answer:** ERROR-level log lines from the kubeintellect application in the last 15 minutes.

## Expected Agent Path

1. **Prefers `query_loki`** with label filter `{namespace="kubeintellect"}` and text filter `|= "ERROR"` and time range of last 15 minutes.
2. Falls back to `kubectl logs -n kubeintellect deployments/kubeintellect --since=15m` only if Loki is unavailable.
3. Presents each ERROR line with timestamp and message.
4. If no errors found: reports "no ERROR-level log lines in the last 15 minutes."

## Minimum Passing Criteria

- [ ] Queries Loki (not just kubectl logs) — Loki provides time-range filtering and multi-pod aggregation
- [ ] Filters to ERROR level only (not WARNING, INFO, DEBUG)
- [ ] Respects 15-minute time window
- [ ] Targets `kubeintellect` namespace / app specifically
- [ ] If no errors: reports clearly rather than returning empty output

## Critical Failures

- Uses only `kubectl logs` without `--since` (returns all logs, not time-filtered)
- Returns INFO/DEBUG lines alongside ERROR lines
- Queries the wrong namespace or application
- Does not use Loki even though it is available
