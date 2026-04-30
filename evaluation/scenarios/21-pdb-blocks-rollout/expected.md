# Scenario 21 — Expected Behavior

## Ground Truth

- **Fault:** `PodDisruptionBudget` `web-service-pdb` requires `minAvailable: 1`. The deployment has only 1 replica and `maxUnavailable: 0`. To do a rolling update the controller must evict the old pod, but that would violate the PDB (0 available < 1 required). Rollout stalls permanently.
- **Symptom:** `kubectl rollout status` shows "Waiting for deployment rollout to finish" indefinitely. No new pods appear. Old pod keeps running.
- **Key distinction from scenario 14 (Rollout Stuck / Bad Image):** In scenario 14, new pods ARE created but fail (ImagePullBackOff). Here, no new pod is created at all — the controller is blocked before even attempting to start one.
- **Fix:** Either increase replicas to ≥ 2 (so rollout can proceed with 1 running), OR temporarily lower PDB to `minAvailable: 0`, OR change `maxUnavailable` to 1.

## Expected Agent Path

1. Routes to **Lifecycle agent**
2. Tool calls:
   - `rollout_status web-service` → stuck / not progressing
   - `describe_deployment web-service` → shows `maxUnavailable: 0`, `maxSurge: 1`, replicas=1
   - `list_pods` → only old pod, no new pod pending or starting
   - `list_poddisruptionbudgets` in namespace → finds `web-service-pdb` with `minAvailable: 1`
   - `describe_poddisruptionbudget web-service-pdb` → `Allowed disruptions: 0` (1 desired - 1 minAvailable = 0)
3. Agent identifies: PDB allows 0 disruptions with only 1 replica → controller cannot evict old pod → rollout blocked
4. Recommends: scale to ≥ 2 replicas OR relax PDB (with HITL for any write)

## Minimum Passing Criteria

- [ ] Identifies rollout as stuck (not progressing, no new pod created)
- [ ] Distinguishes from scenario 14 (no failing new pods — no new pods at all)
- [ ] Finds PodDisruptionBudget `web-service-pdb`
- [ ] Shows `Allowed disruptions: 0` from PDB describe
- [ ] Connects PDB + replica count + maxUnavailable to explain the deadlock
- [ ] Recommends a concrete fix (scale up or relax PDB)

## Critical Failures

- Confuses with scenario 14 (looks for bad image — no new pods to have a bad image)
- Does not check PodDisruptionBudgets
- Claims the image is the problem
- Suggests deleting the old pod (would fix it coincidentally but misses the root cause)
