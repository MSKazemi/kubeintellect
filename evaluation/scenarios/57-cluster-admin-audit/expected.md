# Scenario 57 — Expected Behavior

## Ground Truth
- **Task:** Audit cluster-admin ClusterRoleBindings
- **Success:** Queries live cluster, lists all CRBs referencing cluster-admin with subjects

## Expected Agent Path
1. kubectl get clusterrolebindings -o json → filter where roleRef.name == "cluster-admin"
2. For each: extract metadata.name, subjects[*].{name,kind,namespace}
3. Presents as clear table or list

## Minimum Passing Criteria
- [ ] Queries ClusterRoleBindings (not RoleBindings — different scope)
- [ ] Filters by roleRef.name == cluster-admin
- [ ] Shows subject kind (ServiceAccount/User/Group) for each
- [ ] Uses actual kubectl call, not hardcoded output

## Critical Failures
- Queries RoleBindings instead of ClusterRoleBindings
- Hallucinate subjects without kubectl call
