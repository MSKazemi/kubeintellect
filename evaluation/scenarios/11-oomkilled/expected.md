# Scenario 11 — Expected Behavior

## Ground Truth

- **Fault:** Container allocates ~128MB via `dd` into /dev/shm, exceeding the 64Mi memory limit
- **Symptom:** Container is OOMKilled by the kernel → `OOMKilled` exit code 137 → CrashLoopBackOff
- **Fix:** Increase `resources.limits.memory` to accommodate actual usage OR reduce allocation in the app

## Expected Agent Path

1. Routes to **Logs agent**
2. Tool calls:
   - `list_pods` → sees CrashLoopBackOff or OOMKilled status
   - `describe_pod` → shows `OOMKilled` in Last State + exit code 137
   - `get_pod_logs` or `get_previous_pod_logs` → shows the dd command running before OOM kill
3. Confirms: exit code 137 = OOM kill, memory limit 64Mi exceeded
4. Recommends: increase memory limit (not delete pod)

## Minimum Passing Criteria

- [ ] Identifies OOMKilled (not confused with CrashLoopBackOff or SIGTERM)
- [ ] Shows exit code 137 or Last State: OOMKilled from describe output
- [ ] Names the 64Mi memory limit as the constraint
- [ ] Does NOT suggest deleting the pod as a fix
- [ ] Distinguishes from CrashLoopBackOff caused by app error (different root cause)

## Critical Failures

- Reports pod as healthy
- Blames image pull or RBAC
- Cannot distinguish OOMKilled from regular CrashLoopBackOff
- Suggests increasing CPU instead of memory
