# Scenario 18 — Expected Behavior

## Ground Truth

- **Fault:** Pod references `secretKeyRef.key: DB_URL`, but the Secret `app-secrets` has key `DATABASE_URL` (not `DB_URL`). The Secret itself exists and is correctly formed — only the key name in the pod spec is wrong.
- **Symptom:** Pod stuck in `CreateContainerConfigError`. No container ever starts. Restart count stays at 0 (this is a pre-start failure, not a crash).
- **Error message in events:** `couldn't find key DB_URL in Secret scenario-test/app-secrets`
- **Key distinction from scenario 06 (Missing ConfigMap):** The referenced resource (`app-secrets`) EXISTS. The agent must go one level deeper and inspect the Secret's actual data keys — existence check alone is not enough.
- **Key distinction from scenario 01 (Bad Env Var):** In scenario 01 the container starts and then crashes because a required env var is empty at runtime. Here the container never starts at all — Kubernetes rejects the pod spec during container creation because the key cannot be resolved.
- **Fix:** Change `secretKeyRef.key` from `DB_URL` to `DATABASE_URL` in the Deployment spec.

## Expected Agent Path

1. Routes to **Logs agent** or **ConfigMapsSecrets agent**
2. Tool calls:
   - `list_pods` → pod in `CreateContainerConfigError`, restart count 0
   - `describe_pod api-app` → event: `Error: couldn't find key DB_URL in Secret scenario-test/app-secrets`
   - `get_secret app-secrets` (or `describe_secret`) → data keys are `DATABASE_URL` and `LOG_LEVEL` — no `DB_URL`
3. Agent identifies: Secret exists but contains `DATABASE_URL`, not `DB_URL` → pod spec has wrong key name
4. Recommends: patch the Deployment's `secretKeyRef.key` from `DB_URL` to `DATABASE_URL`

## Minimum Passing Criteria

- [ ] Identifies `CreateContainerConfigError` as the pod state (not CrashLoopBackOff)
- [ ] Shows the describe event with the exact missing key name (`DB_URL`)
- [ ] Inspects the Secret `app-secrets` and lists its actual keys
- [ ] Identifies the key name mismatch (`DB_URL` vs `DATABASE_URL`)
- [ ] Does NOT say the Secret is missing — it exists
- [ ] Recommends fixing the pod spec (not the Secret)

## Critical Failures

- Reports the Secret as missing (it exists — only the key name is wrong)
- Does not inspect the Secret's data keys (stops after confirming Secret exists)
- Confuses with scenario 06 (missing resource vs wrong key in existing resource)
- Confuses with scenario 01 (runtime env var empty vs pre-start config resolution failure)
- Suggests recreating the Secret instead of fixing the pod spec key reference
