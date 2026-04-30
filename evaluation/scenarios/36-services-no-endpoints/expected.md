# Scenario 36 — Expected Behavior

## Ground Truth

- **Query type:** Observability — service connectivity audit
- **No fault injected.**
- **Correct answer:** List of Services that exist but have no matching pod endpoints (Endpoints object has no addresses).

## Expected Agent Path

1. Gets all endpoints: `kubectl get endpoints -A` — look for endpoints with no addresses (empty `subsets`).
2. Cross-references with Services to identify the names.
3. For each service with no endpoints: reports name, namespace, and selector.
4. Notes the likely cause: selector mismatch, no matching pods, or pods not Ready.
5. Excludes headless services with no selector (`spec.selector: {}`) if appropriate.

## Minimum Passing Criteria

- [ ] Covers all namespaces
- [ ] Distinguishes between "no endpoints at all" and "service has no selector" (e.g. ExternalName)
- [ ] For each empty-endpoint service: shows the selector and explains why endpoints may be missing
- [ ] If all services have healthy endpoints: says so clearly
- [ ] Does not confuse ClusterIP: None (headless) with broken services

## Critical Failures

- Reports healthy services as having no endpoints
- Only checks one namespace
- Does not show the selector causing the mismatch
