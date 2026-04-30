# Scenario 35 — Expected Behavior

## Ground Truth

- **Query type:** Observability — namespace-scoped event stream
- **No fault injected.**
- **Correct answer:** All events (Normal and Warning) in the `kubeintellect` namespace from the last 30 minutes.

## Expected Agent Path

1. Calls `run_kubectl get events -n kubeintellect --sort-by=.lastTimestamp` or queries Loki for events.
2. Presents: event type (Normal/Warning), reason, object, message, count, last seen.
3. Filters to recent events (last 30 min) where possible.
4. If no events in window: reports "no events found in the last 30 minutes in the kubeintellect namespace."

## Minimum Passing Criteria

- [ ] Targets the `kubeintellect` namespace specifically (not all namespaces)
- [ ] Shows both Normal and Warning events (query does not say "warning only")
- [ ] Includes event reason and affected object
- [ ] Most recent events shown first (sorted)
- [ ] If no events: says so rather than returning empty output

## Critical Failures

- Queries all namespaces instead of kubeintellect
- Omits Normal events (query asks for all events)
- Returns raw JSON without formatting
