# Scenario 17 — Expected Behavior

## Ground Truth

- **Fault:** Liveness probe checks `GET /health` on port 80, but nginx has no `/health` route → returns HTTP 404. After 2 consecutive failures (`failureThreshold: 2`, every 5 s), kubelet sends SIGKILL to the container (exit code 137) and restarts it.
- **Symptom:** Pod is in CrashLoopBackOff with rising restart count, but app logs show normal nginx access log lines (the app never crashes itself — Kubernetes is doing the killing).
- **Key distinction from scenario 01 (CrashLoop):** The application process does not exit on its own. Exit code 137 appears in `Last State` because kubelet killed it.
- **Key distinction from scenario 08 (Readiness Probe):** A failing readiness probe marks the pod NotReady and removes it from endpoints — it does NOT restart the pod. A failing liveness probe triggers a container kill and restart. This scenario tests whether the agent understands that distinction.
- **Fix:** Correct the liveness probe path from `/health` to `/healthz`, or add a `/health` route to the application.

## Expected Agent Path

1. Routes to **Logs agent**
2. Tool calls:
   - `list_pods` → pod shows `CrashLoopBackOff`, restart count climbing
   - `describe_pod app-server` → reveals:
     - `Last State: Terminated` with reason `Error`, exit code `137`
     - Liveness probe config: `httpGet path: /health port: 80`
     - Events: `Liveness probe failed: HTTP probe failed with statuscode: 404`
     - Events: `Container app-server failed liveness probe, will be restarted`
   - `get_pod_logs` → nginx access logs show `GET /health HTTP/1.1" 404` entries — the app is responding, just returning 404 to the probe
3. Agent identifies: kubelet is killing the container (exit 137 via liveness failure), not the app itself crashing
4. Recommends: fix the liveness probe path to match the actual health endpoint

## Minimum Passing Criteria

- [ ] Identifies restart cause as liveness probe failure, not an application crash
- [ ] Shows liveness probe configuration (wrong path `/health`) from describe output
- [ ] Shows the 404 response in nginx access logs or probe failure event
- [ ] Explains exit code 137 as kubelet SIGKILL (not OOMKilled)
- [ ] Distinguishes liveness from readiness probe behavior (killing vs. endpoint removal)
- [ ] Recommends fixing the probe path rather than the application

## Critical Failures

- Diagnoses as OOMKilled (exit code is the same 137 but reason differs — events and logs must be checked)
- Diagnoses as application crash (app never exits on its own)
- Suggests increasing memory limits (irrelevant)
- Confuses with readiness probe failure (readiness does not restart pods)
- Does not read pod events or liveness probe config from describe output
