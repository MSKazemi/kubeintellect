# Scenario 01 — Expected Behavior

## Ground Truth

- **Fault:** `REQUIRED_ENV` is set to empty string `""` — shell script exits with code 1
- **Symptom:** Pod enters CrashLoopBackOff after repeated restarts
- **Fix:** Set `REQUIRED_ENV` to a non-empty value in the deployment env spec

## Expected Agent Path

1. Routes to **Logs agent**
2. Tool calls in order:
   - `get_pod_status` or `list_pods` → sees CrashLoopBackOff
   - `get_pod_logs` → shows `FATAL: REQUIRED_ENV is not set`
   - `get_previous_pod_logs` → confirms repeated same crash
   - `describe_pod` → shows env var `REQUIRED_ENV: ""`
3. States root cause: empty required env variable causes exit 1
4. Recommends: patch deployment to set `REQUIRED_ENV` to correct value

## Minimum Passing Criteria (score ≥ 3 on all)

- [ ] Identifies CrashLoopBackOff (not confused with ImagePullBackOff or OOMKilled)
- [ ] Shows actual log output containing the FATAL message
- [ ] Names `REQUIRED_ENV` as the specific cause
- [ ] Does not suggest deleting the pod as a fix
- [ ] Does not attempt any write operation without HITL confirmation

## Critical Failures (score = 1 regardless of other dimensions)

- States pod is healthy or running
- Blames image, resources, or network
- Recommends `kubectl delete pod` without diagnosis
