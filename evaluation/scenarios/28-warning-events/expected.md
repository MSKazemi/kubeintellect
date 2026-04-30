# Scenario 28 — Expected Behavior

## Ground Truth

- **Query type:** Observability — event stream
- **No fault injected.** Agent reads live events.
- **Correct answer:** List of Warning-type events from the last hour with reason, object, message, and time.

## Expected Agent Path

1. Calls `run_kubectl get events -A --field-selector type=Warning` or equivalent.
2. Optionally filters by `--sort-by=.lastTimestamp` to show most recent first.
3. Presents events with: namespace, reason, object (kind/name), message, count, and last seen time.
4. If no warnings exist, reports "no warning events in the last hour" clearly.

## Minimum Passing Criteria

- [ ] Uses `--field-selector type=Warning` or otherwise filters to Warning events only
- [ ] Shows event reason and the affected object
- [ ] Shows event message (not just counts)
- [ ] Covers all namespaces (not just one)
- [ ] If no events, says so explicitly rather than returning empty output

## Critical Failures

- Lists Normal events alongside Warning events without distinguishing
- Only checks one namespace
- Returns raw kubectl output without any interpretation
- Queries Prometheus for events (wrong tool)
