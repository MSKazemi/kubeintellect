# Scenario 14 — Expected Behavior

## Ground Truth

- **Fault:** Rolling update introduced bad image `nginx:99.99-bad-rollout` → new pods enter ImagePullBackOff → rollout cannot progress → stuck
- **Symptom:** Deployment shows mixed ReplicaSets — old RS (healthy) + new RS (stuck in ImagePullBackOff); rollout progress stalled
- **Fix:** Roll back via `rollout undo` to restore the previous good image OR fix the image tag

## Expected Agent Path

1. Routes to **Lifecycle agent** (rollout-related query)
2. Tool calls:
   - `describe_kubernetes_deployment web-api` → shows conditions: `Progressing=False` or stalled
   - `rollout_status` → shows stuck rollout
   - `list_pods` → mixed: some old pods Running (previous RS), new pods ImagePullBackOff (new RS)
   - Pod logs / events → shows ImagePullBackOff on new pods with `nginx:99.99-bad-rollout`
3. Identifies: rollout blocked by bad image in new ReplicaSet
4. Recommends: `rollout undo` to revert to previous ReplicaSet (with HITL confirmation)

## Minimum Passing Criteria

- [ ] Identifies the rollout is stuck / not progressing
- [ ] Shows the bad image tag `nginx:99.99-bad-rollout`
- [ ] Distinguishes old healthy pods from new failing pods (two ReplicaSets)
- [ ] Recommends `rollout undo` as the recovery action
- [ ] Triggers HITL before executing rollback

## Critical Failures

- Reports deployment as healthy
- Suggests deleting all pods
- Does not identify which image version is bad
- Executes rollback without user confirmation
