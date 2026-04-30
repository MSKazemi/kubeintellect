# Scenario 16 — Expected Behavior

## Ground Truth

- **Fault:** NetworkPolicy `backend-allow-frontend-only` allows ingress to `backend-api` only from pods with `role=frontend`. `client-app` does NOT have this label → traffic blocked.
- **Symptom:** Both pods Running, service has endpoints, but client cannot reach backend. Pure network policy block.
- **Fix:** Add label `role: frontend` to `client-app` pods OR update the NetworkPolicy to allow `client-app`

## Expected Agent Path

1. Routes to **Infrastructure agent** (networking/service query)
2. Tool calls:
   - `check_service_endpoints backend-api-svc` → endpoints exist (backend pod IS running and ready)
   - `list_network_policies` in namespace → finds `backend-allow-frontend-only`
   - `describe_network_policy backend-allow-frontend-only` → shows `podSelector: role=frontend` on ingress rule
   - `list_pods` or `describe_pod client-app` → shows labels `app: client-app` (no `role: frontend`)
3. Confirms: NetworkPolicy is the cause. client-app missing `role=frontend` label.
4. Recommends: add `role: frontend` label to client-app OR expand NetworkPolicy

## Minimum Passing Criteria

- [ ] Confirms endpoints are NOT empty (service is correctly configured — eliminates selector mismatch)
- [ ] Finds the NetworkPolicy `backend-allow-frontend-only`
- [ ] Shows the ingress rule requiring `role: frontend`
- [ ] Shows that `client-app` pods lack this label
- [ ] Correctly categorizes as NetworkPolicy issue (not DNS, not service selector)

## Critical Failures

- Says service has no endpoints (it does — this is a different fault than scenario 03)
- Does not check NetworkPolicies
- Blames service selector (selectors are correct — pods are in endpoints)
- Cannot explain which label is missing
