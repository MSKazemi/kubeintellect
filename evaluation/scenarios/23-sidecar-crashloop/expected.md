# Scenario 23 — Expected Behavior

## Ground Truth

- **Fault:** Sidecar container `log-shipper` exits immediately with code 1 (cannot reach `log-aggregator.internal:5170`). Kubernetes restarts the entire pod when any container in it fails — so the healthy `app` (nginx) also gets restarted even though it never crashes.
- **Symptom:** Pod in `CrashLoopBackOff`, restart count rising. Main app logs look normal between restarts.
- **Key distinction from scenario 01 (CrashLoop / Bad Env Var):** In scenario 01, the main application container crashes. Here the main container (`app`) is completely healthy — the culprit is the sidecar. The agent must check container statuses individually, not just the pod.
- **Key distinction from scenario 17 (Liveness Probe):** Pod is killed by a container exit, not by kubelet liveness action. Exit code is 1 (not 137).
- **Fix:** Fix or remove the `log-shipper` sidecar (ensure `log-aggregator.internal` is reachable, or correct the endpoint).

## Expected Agent Path

1. Routes to **Logs agent**
2. Tool calls:
   - `list_pods` → pod `CrashLoopBackOff`, restart count rising
   - `describe_pod web-app-*` → container statuses show `app: Running` but `log-shipper: Error / CrashLoopBackOff`; last state of `log-shipper` shows exit code 1
   - `get_pod_logs` with `container=log-shipper` → "ERROR: cannot connect to log-aggregator.internal:5170"
   - `get_pod_logs` with `container=app` → normal nginx access logs (app is healthy)
3. Agent identifies: `log-shipper` sidecar is the crashing container; main app is fine
4. Recommends: fix the sidecar endpoint or remove it

## Minimum Passing Criteria

- [ ] Identifies the pod as CrashLoopBackOff
- [ ] Checks container statuses individually (not just pod-level status)
- [ ] Names `log-shipper` as the crashing container (not `app`)
- [ ] Shows logs from `log-shipper` with the error message
- [ ] Confirms `app` (nginx) logs are normal — main app is healthy
- [ ] Does NOT suggest the nginx image or config is broken

## Critical Failures

- Blames the main `app` container (nginx) for the crash
- Does not fetch per-container logs (only checks pod-level logs, which default to `app`)
- Does not read individual container statuses from describe output
- Confuses with scenario 01 (main app crash vs sidecar crash)
