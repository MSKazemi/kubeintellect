# Scenario 05 — Missing ConfigMap Reference

## Injected Fault
Deployment `config-app` in `scenario-test`. Pod references ConfigMap
`nonexistent-config` via `envFrom`. The ConfigMap does not exist.

## Correct Root Cause
Pod references a ConfigMap `nonexistent-config` that does not exist →
`CreateContainerConfigError` or `RunContainerError` → pod never starts.

## Expected Tool Sequence
1. `kubectl get pods -n scenario-test` — observe CreateContainerConfigError
2. `kubectl describe pod config-app-<id> -n scenario-test` — events show
   "configmap nonexistent-config not found"
3. `kubectl get configmaps -n scenario-test` — confirm it does not exist

## Success Criteria
- [ ] Identifies CreateContainerConfigError (not CrashLoopBackOff)
- [ ] Shows pod events mentioning `nonexistent-config not found`
- [ ] Lists ConfigMaps in namespace and confirms absence
- [ ] Suggests creating the ConfigMap or removing the reference

## Critical Failure Conditions
- Blames image or resource limits
- Does not check pod events
- Claims ConfigMap exists

## Scoring Rubric (1–5 per dimension, /40 total, pass ≥ 28)

| Dimension | 1 | 3 | 5 |
|-----------|---|---|---|
| Problem understanding | Wrong fault type | Pod won't start, unknown cause | CreateContainerConfigError + missing CM |
| Reasoning path | No tools | Pod only | Pod describe → configmap list |
| Root cause accuracy | Wrong (image/resources) | "Config issue" | Exact: nonexistent-config not found |
| Tool selection | Wrong tools | Mostly right | Pod describe + configmap list |
| Output clarity | Confusing | Understandable | Actionable fix suggestion |
| Action safety | Destructive | Cautious | Read-only diagnosis |
| Recovery | Crashes | Retries | Graceful |
| Routing efficiency | Loops | 1 extra hop | Zero unnecessary hops |
