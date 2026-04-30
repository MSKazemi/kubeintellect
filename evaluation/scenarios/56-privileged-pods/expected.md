# Scenario 56 — Expected Behavior

## Ground Truth
- **Task:** Audit pods for root/privileged security contexts
- **Success:** Agent reads pod specs from the cluster, identifies security context issues

## Expected Agent Path
1. kubectl get pods -A -o json → parse securityContext at pod and container level
2. Check: runAsUser==0 OR privileged==true OR (no runAsNonRoot:true and no runAsUser set)
3. Lists findings: pod name, namespace, container, issue type

## Minimum Passing Criteria
- [ ] Checks both pod-level and container-level securityContext
- [ ] Shows actual pod names from the live cluster
- [ ] Distinguishes between privileged and root issues
- [ ] If none found, reports clearly with what was checked

## Critical Failures
- Reports findings without kubectl call (hallucinated pod names)
- Checks only pod-level securityContext (misses container-level)
