# Scenario 25 — Expected Behavior

## Ground Truth

- **Fault:** ConfigMap `app-config` exists and has key `app.properties`, but the volume definition requests key `app.conf` (which doesn't exist). Kubernetes cannot project the volume → pod stays in `CreateContainerConfigError` with event: `couldn't find key app.conf in ConfigMap scenario-test/app-config`.
- **Symptom:** Pod stuck in `CreateContainerConfigError`, restart count 0.
- **Key distinctions:**
  - vs scenario 06 (Missing ConfigMap): the ConfigMap EXISTS — only the key name within it is wrong.
  - vs scenario 18 (Secret Key Mismatch): same pattern but for a volume mount, not an `env.valueFrom`. The probe path is through `volumes[].configMap.items[].key`, not `env[].valueFrom.configMapKeyRef.key`.
- **Fix:** Change the volume key from `app.conf` to `app.properties` in the Deployment's `volumes` spec.

## Expected Agent Path

1. Routes to **Logs agent** or **ConfigMapsSecrets agent**
2. Tool calls:
   - `list_pods` → pod `CreateContainerConfigError`, restart count 0
   - `describe_pod config-reader-*` → event: `couldn't find key app.conf in ConfigMap scenario-test/app-config`
   - `get_configmap app-config` → data keys are `app.properties` and `feature-flags.yaml` (no `app.conf`)
3. Agent identifies: volume requests key `app.conf`, ConfigMap has `app.properties` → fix the volume spec
4. Recommends: update `volumes[].configMap.items[].key` from `app.conf` to `app.properties`

## Minimum Passing Criteria

- [ ] Identifies `CreateContainerConfigError` (not CrashLoopBackOff)
- [ ] Shows event with exact missing key (`app.conf`)
- [ ] Reads ConfigMap data and lists actual keys (`app.properties`, `feature-flags.yaml`)
- [ ] Identifies this as a volume key mismatch (not an env var issue)
- [ ] Does NOT say ConfigMap is missing — it exists
- [ ] Recommends fixing the volume key reference in the Deployment, not the ConfigMap

## Critical Failures

- Says ConfigMap is missing (it exists)
- Confuses with scenario 06 (missing CM) or scenario 18 (secret env key)
- Does not inspect the ConfigMap's actual keys
- Recommends adding `app.conf` key to the ConfigMap (wrong — fix the reference, not the CM)
