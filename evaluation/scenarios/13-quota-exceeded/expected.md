# Scenario 13 — Expected Behavior

## Ground Truth

- **Fault:** ResourceQuota `tight-quota` limits namespace to 200m CPU requests and 2 pods. The deployment requests 3×150m = 450m CPU and 3 pods — exceeding both the CPU quota and pod count quota.
- **Symptom:** ReplicaSet cannot create all pods — some pods fail to create with `exceeded quota` event
- **Fix:** Either increase the quota OR reduce `replicas` to 1 (150m ≤ 200m) and accept the pod limit

## Expected Agent Path

1. Routes to **Logs agent** or **Infrastructure agent**
2. Tool calls:
   - `describe_deployment quota-buster` → shows desired=3, available < 3
   - `list_pods` → sees only 1-2 pods created, rest blocked
   - Events on ReplicaSet or namespace → "exceeded quota: tight-quota, requested: pods=1, used: pods=2, limited: pods=2" or CPU quota message
   - `describe_quota` or `list_resource_quotas` → shows tight-quota with used vs hard limits
3. Identifies: ResourceQuota is blocking pod creation (not scheduling — quota is enforced at admission)
4. Recommends: increase quota OR reduce replicas

## Minimum Passing Criteria

- [ ] Identifies that the deployment has fewer pods than desired (not all replicas running)
- [ ] Finds the ResourceQuota `tight-quota`
- [ ] Shows quota used vs hard limits (CPU or pod count exceeded)
- [ ] Distinguishes from scheduler Pending (quota is enforced at admission, not scheduling)
- [ ] Does NOT blame node resources or taints

## Critical Failures

- Reports all 3 replicas as running
- Blames node capacity instead of namespace quota
- Cannot find the ResourceQuota object
- Confuses with scenario 04 (node-level vs namespace-level constraints)
