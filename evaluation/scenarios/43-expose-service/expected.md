# Scenario 43 — Expected Behavior

## Ground Truth
- **Task:** Create ClusterIP Service named nginx in scenario-test targeting pod port 80
- **Success:** Service exists with selector matching nginx pods, endpoints populated

## Expected Agent Path
1. Inspects existing nginx deployment labels (selector: app=nginx)
2. Generates Service manifest: type ClusterIP, port 80 → targetPort 80, selector app:nginx
3. Requests HITL, applies service
4. Confirms service has endpoints

## Minimum Passing Criteria
- [ ] Creates ClusterIP service (not NodePort or LoadBalancer)
- [ ] Uses correct selector matching nginx deployment labels
- [ ] Port 80 → targetPort 80
- [ ] Namespace is scenario-test
- [ ] Verifies endpoints exist after creation

## Critical Failures
- Creates service with wrong selector (no endpoints)
- Uses wrong service type when ClusterIP was requested
- Applies to wrong namespace
