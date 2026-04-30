# Scenario 20 — Expected Behavior

## Ground Truth

- **Pre-condition:** Worker node has taint `dedicated=gpu:NoSchedule` applied manually before the scenario.
- **Fault:** Deployment `gpu-workload` has no `tolerations` block → scheduler cannot place the pod on any tainted node.
- **Symptom:** Pod stays `Pending`. Scheduler events: `0/2 nodes are available: 1 node(s) had untolerated taint {dedicated: gpu}: 1 node(s) were unschedulable.`
- **Key distinction from scenario 04 (Insufficient Resources):** Resources are fine — the blocking reason is a taint, not CPU/memory. The agent must read scheduler events carefully and not jump to resource conclusions.
- **Fix:** Add a matching toleration to the pod spec, OR remove the taint from the node.

## Expected Agent Path

1. Routes to **Logs agent** or **Infrastructure agent**
2. Tool calls:
   - `list_pods` → pod `Pending`, restart count 0
   - `describe_pod gpu-workload-*` → events show `0/N nodes available: N node(s) had untolerated taint {dedicated: gpu}`
   - `describe_nodes` or `get_nodes` → confirms node has taint `dedicated=gpu:NoSchedule`
   - Checks allocatable resources → sufficient (rules out resource constraint)
3. Agent identifies: taint/toleration mismatch, not resource shortage
4. Recommends: add `tolerations: [{key: dedicated, operator: Equal, value: gpu, effect: NoSchedule}]` to pod spec

## Minimum Passing Criteria

- [ ] Identifies pod as `Pending`
- [ ] Shows scheduler event mentioning "untolerated taint"
- [ ] Checks node taints and confirms `dedicated=gpu:NoSchedule`
- [ ] Confirms resources are NOT the issue (eliminates scenario 04 hypothesis)
- [ ] Correctly categorizes as taint/toleration problem
- [ ] Recommends adding a toleration (not reducing resource requests)

## Critical Failures

- Diagnoses as insufficient resources (same Pending symptom as scenario 04 but different cause)
- Does not check node taints
- Recommends deleting and recreating the pod with same spec
- Does not distinguish taint from affinity/nodeSelector constraints
