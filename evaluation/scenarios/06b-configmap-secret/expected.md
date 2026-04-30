# Scenario 06 — Expected Behavior

## Ground Truth

- **Fault:** Deployment references `configMapRef: nonexistent-config` which does not exist
- **Symptom:** Pod in `CreateContainerConfigError` — kubelet cannot create container
- **Fix:** Create the ConfigMap or remove the reference

## Expected Agent Path

1. Routes to **Logs** or **ConfigMapsSecrets** agent
2. Tool calls:
   - `list_pods` → pod in `CreateContainerConfigError` state
   - `describe_pod config-app` → events: `configmap "nonexistent-config" not found`
   - `list_configmaps -n scenario-test` → confirms `nonexistent-config` absent
3. Conclusion: pod references ConfigMap that doesn't exist in the namespace
4. Recommends: create the ConfigMap with required keys or fix the reference

## Minimum Passing Criteria

- [ ] Identifies `CreateContainerConfigError` (not CrashLoop or ImagePull)
- [ ] Shows the event message naming `nonexistent-config`
- [ ] Confirms ConfigMap does not exist in the namespace
- [ ] Proposes creating the ConfigMap (not deleting the pod)

## Critical Failures

- Blames image or resource limits
- Does not check pod events
- Claims the ConfigMap exists
