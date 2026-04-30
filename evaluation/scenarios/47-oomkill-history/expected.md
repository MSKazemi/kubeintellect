# Scenario 47 — Expected Behavior

## Ground Truth
- **Task:** Find recent OOMKilled pods, show peak memory, recommend new limits
- **Success:** Agent checks pod events/status for OOMKilled, queries Prometheus for memory usage history

## Expected Agent Path
1. kubectl get pods -A: look for OOMKilled in STATUS or high restart counts
2. describe_pod or get events for OOMKilled confirmation
3. Prometheus query: container_memory_working_set_bytes over last 24h for those pods
4. Recommends new limit = peak_usage × 1.3

## Minimum Passing Criteria
- [ ] Checks pod status/events for OOMKilled (not just guesses)
- [ ] Shows actual memory values (not vague "increase the limit")
- [ ] Applies 30% margin correctly
- [ ] If no OOMKilled pods found, correctly reports none

## Critical Failures
- Recommends limits without querying actual memory usage
- Reports OOMKilled for pods that are healthy
