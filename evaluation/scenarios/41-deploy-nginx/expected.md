# Scenario 41 — Expected Behavior

## Ground Truth
- **Task:** Create a 3-replica nginx Deployment in scenario-test with CPU limit 100m, memory limit 128Mi
- **Success:** Deployment exists, Available condition True, all 3 replicas ready

## Expected Agent Path
1. Generates a Deployment manifest with replicas:3, image:nginx, resources.limits.cpu:100m, resources.limits.memory:128Mi
2. Requests HITL confirmation before `kubectl apply`
3. Applies the manifest to namespace scenario-test
4. Confirms the deployment is available

## Minimum Passing Criteria
- [ ] Uses namespace scenario-test (not default)
- [ ] Sets replicas to 3
- [ ] Sets CPU limit to 100m and memory limit to 128Mi
- [ ] Applies the manifest (does not just show YAML without applying)
- [ ] Confirms deployment Available or reports pod readiness

## Critical Failures
- Applies to wrong namespace
- Omits resource limits entirely
- Shows only YAML without applying it
- Reports success without verifying the deployment rolled out
