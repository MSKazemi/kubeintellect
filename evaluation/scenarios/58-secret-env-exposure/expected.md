# Scenario 58 — Expected Behavior

## Ground Truth
- **Task:** Identify pods using secretKeyRef env vars (less secure than volume mounts)
- **Success:** Parses pod specs from cluster, correctly identifies secretKeyRef usage

## Expected Agent Path
1. kubectl get pods -A -o json → parse spec.containers[*].env[*].valueFrom.secretKeyRef
2. For each match: pod name, namespace, container name, secret name and key
3. Explains why volume mounts are preferred (not in process environment)

## Minimum Passing Criteria
- [ ] Searches all namespaces
- [ ] Correctly parses env[].valueFrom.secretKeyRef (not envFrom.secretRef)
- [ ] Shows actual secret names being referenced
- [ ] If none found, explains what was checked

## Critical Failures
- Confuses envFrom.secretRef (bulk) with env[].valueFrom.secretKeyRef (per-key)
- Hallucinate results without querying the cluster
