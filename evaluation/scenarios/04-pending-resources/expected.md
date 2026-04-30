# Scenario 04 — Expected Behavior

## Ground Truth

- **Fault:** Pod requests 50Gi memory — no standard node has this free
- **Symptom:** Pod stays Pending indefinitely; scheduler events say "Insufficient memory"
- **Fix:** Reduce memory request to a realistic value

## Expected Agent Path

1. Routes to **Logs** or **Infrastructure** agent
2. Tool calls:
   - `describe_pod hungry-app` → events show `0/N nodes are available: N Insufficient memory`
   - `list_nodes` → shows node count and allocatable memory
   - Check allocatable memory < 50Gi on all nodes
3. Eliminates taints (no taints present), affinity (none configured)
4. Conclusion: resource requests exceed cluster capacity
5. Recommends: reduce memory request to realistic value (e.g., 256Mi–2Gi)

## Minimum Passing Criteria

- [ ] Shows scheduler events with "Insufficient memory" message
- [ ] Checks node allocatable capacity
- [ ] Rules out taints/tolerations as cause (with evidence)
- [ ] Identifies 50Gi as the unrealistic request
- [ ] Does NOT suggest adding more nodes as the only fix

## Critical Failures

- Blames taints or affinity without checking
- Does not show scheduler events
- Suggests the pod is Running
- Recommends deleting and redeploying with same requests
