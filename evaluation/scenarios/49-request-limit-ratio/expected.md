# Scenario 49 — Expected Behavior

## Ground Truth
- **Task:** Find pods with CPU request/limit ratio < 0.2, explain throttling risk
- **Success:** Gets actual resource values from cluster, computes ratios, explains CFS throttling mechanism

## Expected Agent Path
1. kubectl get pods -A -o json: extract resources.requests.cpu and resources.limits.cpu per container
2. Compute ratio = request / limit, filter < 0.2
3. Explain: Linux CFS quota — when limit is hit, container is throttled even if overall CPU is free

## Minimum Passing Criteria
- [ ] Retrieves actual resource values from cluster (not placeholders)
- [ ] Computes ratio correctly (request ÷ limit)
- [ ] Explains CFS (Completely Fair Scheduler) throttling mechanism
- [ ] Shows actual pod names and namespaces

## Critical Failures
- Gives generic advice without querying actual resources
- Confuses request/limit with throttling incorrectly
