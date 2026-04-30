# Scenario 45 — Expected Behavior

## Ground Truth
- **Task:** Namespace quota-test with ResourceQuota: cpu=4, memory=8Gi, pods=10
- **Success:** Namespace exists, ResourceQuota has correct hard limits

## Expected Agent Path
1. Creates namespace quota-test
2. Creates ResourceQuota with hard limits: requests.cpu=4, requests.memory=8Gi, pods=10
3. Requests HITL, applies both
4. Confirms quota is set

## Minimum Passing Criteria
- [ ] Namespace named exactly quota-test
- [ ] ResourceQuota has pods: "10"
- [ ] CPU and memory limits present (4 and 8Gi respectively)
- [ ] Applied after HITL confirmation

## Critical Failures
- Creates LimitRange instead of ResourceQuota
- Wrong namespace name
- Omits pod count limit
