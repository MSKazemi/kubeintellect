# Scenario 08 — Expected Behavior

## Ground Truth

- **Fault:** Readiness probe checks port 9999 but nginx listens on port 80
- **Symptom:** Pod is Running but never becomes Ready → 0/1 Ready → service endpoints empty
- **Fix:** Change readinessProbe port from 9999 to 80

## Expected Agent Path

1. Routes to **Logs agent**
2. Tool calls:
   - `list_pods` → pod shows `0/1 Ready`, status Running
   - `describe_pod probe-app` → shows readinessProbe on port 9999 + events: "Readiness probe failed: dial tcp ... connection refused"
   - `get_endpoints probe-service` → 0 endpoints (because pod not Ready)
3. Identifies: probe port (9999) ≠ container port (80)
4. Conclusion: probe failure → pod never Ready → service has no backends
5. Recommends: fix probe port to 80

## Minimum Passing Criteria

- [ ] Distinguishes "Running but not Ready" from CrashLoopBackOff
- [ ] Shows readiness probe configuration with port 9999
- [ ] Shows probe failure events
- [ ] Fetches endpoints and confirms empty
- [ ] Identifies port mismatch (9999 vs 80) as root cause
- [ ] Does NOT confuse with liveness probe (different behavior)

## Critical Failures

- Reports pod as Ready
- Blames CrashLoop instead of probe failure
- Does not check endpoints
- Does not identify the port mismatch
