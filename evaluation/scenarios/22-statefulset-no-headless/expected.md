# Scenario 22 — Expected Behavior

## Ground Truth

- **Fault:** StatefulSet `db-cluster` has `serviceName: db-headless` but no Service named `db-headless` exists in the namespace. The pod itself runs fine (Kubernetes ≥ 1.26 no longer blocks pod creation). However, stable DNS names (`db-cluster-0.db-headless.scenario-test.svc.cluster.local`) are unresolvable because the headless Service — which backs that DNS entry — is absent.
- **Symptom:** Pod `db-cluster-0` is Running and Ready, but DNS lookup for the stable hostname returns NXDOMAIN.
- **Key distinction from scenario 03 (Service Selector Mismatch):** In scenario 03 the Service exists with wrong selectors (empty endpoints). Here the Service does not exist at all, and the pod is healthy — the breakage is purely in DNS.
- **Fix:** Create the headless Service `db-headless` with `clusterIP: None` and selector `app: db-cluster`.

## Expected Agent Path

1. Routes to **Infrastructure agent**
2. Tool calls:
   - `list_pods` → `db-cluster-0` Running (pod is healthy)
   - `describe_statefulset db-cluster` → `serviceName: db-headless`
   - `list_services` in namespace → `db-headless` absent
   - Agent recognises: headless Service is the DNS backing for stable StatefulSet hostnames; its absence breaks `<pod>.<svc>.<ns>.svc.cluster.local` resolution
3. Recommends: create headless Service with `clusterIP: None`, selector `app: db-cluster`

## Minimum Passing Criteria

- [ ] Identifies StatefulSet resource type (not Deployment)
- [ ] Notes pod IS running (does not claim pod is broken)
- [ ] Reads `serviceName: db-headless` from StatefulSet spec
- [ ] Confirms Service `db-headless` is absent from the namespace
- [ ] Explains that headless Services back stable DNS names for StatefulSet pods
- [ ] Recommends creating headless Service (`clusterIP: None`) — not a regular ClusterIP

## Critical Failures

- Claims the pod is broken or not running (pod is healthy)
- Confuses with scenario 03 (service exists there with wrong selectors)
- Suggests creating a regular ClusterIP Service (does not provide stable DNS names)
- Does not check the StatefulSet `serviceName` field
