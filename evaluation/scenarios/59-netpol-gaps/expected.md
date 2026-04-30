# Scenario 59 — Expected Behavior

## Ground Truth
- **Task:** Find namespaces without NetworkPolicy, list their exposed workloads
- **Success:** Compares NetworkPolicy namespace coverage against all namespaces, lists running workloads

## Expected Agent Path
1. kubectl get networkpolicies -A → get namespaces that have at least one policy
2. kubectl get namespaces → all namespaces
3. Difference = unprotected namespaces
4. kubectl get pods/deployments in each unprotected namespace → list workloads

## Minimum Passing Criteria
- [ ] Compares NetworkPolicy namespaces against all namespaces (not just guessing)
- [ ] Shows workloads in unprotected namespaces with actual names
- [ ] Explains "lateral movement" risk correctly
- [ ] If all namespaces have policies, reports clearly

## Critical Failures
- Reports namespaces without querying NetworkPolicy list
- Confuses pod-level and namespace-level policies
