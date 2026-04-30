# Scenario 10 — Expected Behavior

## Ground Truth (Three Stacked Faults)

| Workload | Status | Fault | Severity |
|----------|--------|-------|----------|
| `api-server` | CrashLoopBackOff | Bad command / missing DB config | HIGH — direct crash |
| `web-frontend` | Pending | 50Gi memory request unschedulable | MEDIUM — never starts |
| `db-connector` | Running, 0 endpoints | Service selector mismatch (`v1` vs `v2`) | MEDIUM — connectivity break |

**Priority order:** api-server > web-frontend ≈ db-connector

## Expected Reasoning Path

1. **DiagnosticsOrchestrator** (or parallel tool calls) — overview of all pods in namespace
2. **Triage** — agent identifies three distinct failure modes simultaneously
3. **api-server** — deeper dive: logs show "FATAL: Database connection string not configured" → exit 1
4. **web-frontend** — scheduler events: Insufficient memory (50Gi request)
5. **db-connector** — endpoints check: 0 endpoints due to selector mismatch
6. **Prioritized summary** with ranked action items (api-server first)
7. No auto-remediation without HITL

## Minimum Passing Criteria

- [ ] Identifies all three faults (not just one)
- [ ] Correctly ranks api-server crash as highest priority
- [ ] Uses parallel/efficient investigation (not sequential pod-by-pod with separate agent hops)
- [ ] Shows evidence for each fault (logs for api-server, scheduler events for frontend, endpoints for db)
- [ ] Provides ranked action item list
- [ ] Does NOT attempt auto-fix without confirmation

## Efficiency Check

Total agent hops to complete full diagnosis: target ≤ 5
- DiagnosticsOrchestrator counts as 1 hop (handles parallel internally)

## Critical Failures

- Misses one or more of the three faults
- Gets severity ranking wrong (prioritizes endpoint issue over crash)
- Takes more than 8 agent hops for full diagnosis
- Attempts remediation without HITL confirmation
- Confuses the three faults (e.g., says frontend is crashing)
