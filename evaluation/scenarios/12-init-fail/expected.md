# Scenario 12 — Expected Behavior

## Ground Truth

- **Fault:** Init container `db-check` tries to TCP-connect to `db.internal.svc.cluster.local:5432`, which does not exist → exits with code 1
- **Symptom:** Pod stuck in `Init:CrashLoopBackOff` or `Init:0/1` — the main app container (nginx) never starts
- **Fix:** Ensure `db.internal.svc.cluster.local` exists and is reachable, OR remove/fix the init container check

## Expected Agent Path

1. Routes to **Logs agent**
2. Tool calls:
   - `list_pods` → status shows `Init:CrashLoopBackOff` or `Init:0/1`
   - `describe_pod` → shows init container `db-check` in failed state, exit code 1
   - `get_pod_logs` with container=`db-check` → shows "nc: bad address" or connection timeout
3. Identifies: init container is the blocking component, not the app container
4. Recommends: fix the database service reference or remove the prereq check

## Minimum Passing Criteria

- [ ] Identifies the pod is stuck in init container phase (not Running or CrashLoopBackOff of main container)
- [ ] Names `db-check` as the failing init container
- [ ] Shows logs from the init container (not the main app)
- [ ] Identifies `db.internal.svc.cluster.local:5432` as the unreachable target
- [ ] Does NOT suggest the nginx app itself is broken

## Critical Failures

- Reports main app container as the failure (nginx is never started)
- Cannot distinguish init container failure from main container failure
- Misidentifies as ImagePullBackOff
- Suggests the nginx image is wrong
