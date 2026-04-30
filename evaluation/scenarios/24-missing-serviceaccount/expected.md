# Scenario 24 — Expected Behavior

## Ground Truth

- **Fault:** Pod spec sets `serviceAccountName: metrics-reader`, but that ServiceAccount does not exist in `scenario-test`. The ReplicaSet controller tries to create a pod and is immediately rejected: `pods "metrics-app-..." is forbidden: error looking up service account scenario-test/metrics-reader: serviceaccount "metrics-reader" not found`.
- **Symptom:** Deployment shows `0/1 ready`. Zero pods exist in any state (not Pending, not Error — simply never created). The fault is on the ReplicaSet, not a pod.
- **Key distinctions:**
  - vs scenario 06/18/25: those faults manifest as `CreateContainerConfigError` on an existing pod. Here no pod is ever created — the ReplicaSet controller itself is rejected.
  - vs scenario 07 (RBAC Denied): in 07 the SA exists but lacks permissions. Here the SA doesn't exist at all.
  - vs scenario 19 (CronJob Suspended): both have zero pods, but the cause is completely different.
- **Fix:** Create the ServiceAccount (`kubectl create serviceaccount metrics-reader -n scenario-test`) or fix pod spec to use `default`.

## Expected Agent Path

1. Routes to **Logs agent** or **Infrastructure agent**
2. Tool calls:
   - `list_pods` → empty (no pods in any state)
   - `describe_deployment metrics-app` → `0/1 ready`, `0` available
   - `describe_replicaset` for metrics-app → events show `FailedCreate: pods "metrics-app-..." is forbidden: error looking up service account scenario-test/metrics-reader: serviceaccount "metrics-reader" not found`
   - `list_serviceaccounts` in `scenario-test` → only `default` present, `metrics-reader` absent
3. Agent identifies: ReplicaSet controller cannot create pods because the ServiceAccount is missing
4. Recommends: create the SA or fix the pod spec reference

## Minimum Passing Criteria

- [ ] Identifies zero pods in any state (not Pending — never created)
- [ ] Checks the ReplicaSet or Deployment events (not pod events — no pods exist)
- [ ] Shows `FailedCreate` event with `serviceaccount "metrics-reader" not found`
- [ ] Confirms ServiceAccount is absent from namespace
- [ ] Distinguishes from RBAC (07): SA missing entirely vs SA exists but lacks permissions
- [ ] Recommends creating the SA

## Critical Failures

- Diagnoses as RBAC denied (SA exists there; missing here)
- Only checks pods and reports no issues (no failing pods — must go to RS/Deployment)
- Suggests fixing a Role or RoleBinding before creating the SA
- Confuses with scenario 19 (CronJob suspended — different resource type)
