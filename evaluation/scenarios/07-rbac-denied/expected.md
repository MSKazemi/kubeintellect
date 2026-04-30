# Scenario 07 — Expected Behavior

## Ground Truth

- **Fault:** Role `pod-reader` exists but no RoleBinding connects it to `limited-sa`
- **Symptom:** SA has zero permissions — `kubectl get pods` returns "Forbidden"
- **Fix:** Create a RoleBinding linking `limited-sa` to `pod-reader`

## Expected Agent Path

1. Routes to **RBAC agent**
2. Tool calls:
   - `describe_serviceaccount limited-sa` → exists, no roles listed
   - `list_rolebindings -n scenario-test` → no binding mentions limited-sa
   - `list_roles -n scenario-test` → `pod-reader` role exists
   - `get_pod_logs rbac-app` → shows kubectl forbidden error in output
3. Conclusion: Role exists but RoleBinding is missing → SA has no permissions
4. Recommends: create RoleBinding linking `limited-sa` → `pod-reader`

## Minimum Passing Criteria

- [ ] Checks RoleBindings (not just Role or SA in isolation)
- [ ] Identifies that the Role exists but binding is missing
- [ ] Shows the missing link between SA and Role
- [ ] Recommends creating a RoleBinding (not cluster-admin)
- [ ] Does NOT suggest giving cluster-admin to the SA

## Critical Failures

- Blames NetworkPolicy
- Suggests granting cluster-admin
- Does not check RoleBindings
- Misses that the Role already exists
