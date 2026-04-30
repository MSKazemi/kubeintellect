# Scenario 44 — Expected Behavior

## Ground Truth
- **Task:** ConfigMap app-config with DB_HOST=postgres.svc; Deployment busybox-config using it as envFrom
- **Success:** Pod running, DB_HOST env var present inside the container

## Expected Agent Path
1. Creates ConfigMap with data: DB_HOST: postgres.svc
2. Creates Deployment referencing the ConfigMap via envFrom or env[].valueFrom.configMapKeyRef
3. Requests HITL, applies both resources
4. Verifies pod is Running

## Minimum Passing Criteria
- [ ] ConfigMap has exactly DB_HOST=postgres.svc
- [ ] Deployment references the ConfigMap via env (not volume mount)
- [ ] Pod label app=busybox-config (needed for verify.sh)
- [ ] Both resources created in scenario-test

## Critical Failures
- Mounts ConfigMap as volume instead of env vars
- Uses wrong key name (not DB_HOST)
- Pod label mismatch with app=busybox-config
