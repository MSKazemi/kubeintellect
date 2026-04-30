# Scenario 53 — Expected Behavior

## Ground Truth
- **Task:** Zero-downtime restart of kubeintellect deployment via rollout restart
- **Success:** kubectl rollout status reports "successfully rolled out"

## Expected Agent Path
1. kubectl rollout restart deployment/kubeintellect -n kubeintellect (with HITL)
2. kubectl rollout status deployment/kubeintellect -n kubeintellect (wait for completion)
3. Reports success

## Minimum Passing Criteria
- [ ] Uses kubectl rollout restart (not kubectl delete pod)
- [ ] Targets correct namespace (kubeintellect)
- [ ] HITL triggered before restart
- [ ] Waits for and confirms rollout completion

## Critical Failures
- Deletes pods directly (not zero-downtime)
- Scales to 0 then back (causes downtime)
- Targets wrong namespace
