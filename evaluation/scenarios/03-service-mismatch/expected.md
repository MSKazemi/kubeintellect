# Scenario 03 — Expected Behavior

## Ground Truth

- **Fault:** Service `backend-service` selects `app: backend-v2` but the only pod has label `app: backend-v1`
- **Symptom:** Service Endpoints object shows 0 addresses — traffic has nowhere to go
- **Fix:** Update service selector to `app: backend-v1` (or update pod labels to match)

## Expected Agent Path

1. Routes to **Logs** or **ConfigMapsSecrets** agent
2. Tool calls:
   - `describe_service backend-service` → shows selector `app: backend-v2`
   - `get_endpoints backend-service` → shows `<none>` or empty subsets
   - `list_pods -l app=backend-v2` → returns nothing
   - `list_pods -l app=backend-v1` → returns the running pod
3. Conclusion: selector mismatch → 0 endpoints → packets dropped
4. Recommends: fix selector (NOT NetworkPolicy, NOT DNS)

## Minimum Passing Criteria

- [ ] Explicitly fetches the Endpoints object (not just the Service)
- [ ] Reports 0 endpoints
- [ ] Shows both the selector value and the actual pod label
- [ ] Categorizes issue as Service selector (not DNS, not NetworkPolicy)
- [ ] Proposes the correct fix (selector OR label change, with reasoning)

## Critical Failures

- Blames DNS without showing empty endpoints
- Suggests adding NetworkPolicy rules
- Does not check the Endpoints object at all
- Reports endpoints as healthy
