# Scenario 37 — Expected Behavior

## Ground Truth

- **Query type:** Observability — external exposure audit
- **No fault injected.**
- **Correct answer:** List of Services with type LoadBalancer or NodePort across all namespaces.

## Expected Agent Path

1. Calls `kubectl get services -A` and filters for type=LoadBalancer or type=NodePort.
2. For LoadBalancer: shows external IP (or `<pending>` if not assigned) and port.
3. For NodePort: shows the NodePort number and target service port.
4. Notes if any LoadBalancer IPs are still `<pending>` (no external IP assigned).

## Minimum Passing Criteria

- [ ] Covers all namespaces
- [ ] Clearly distinguishes LoadBalancer from NodePort
- [ ] Shows port mapping (external port → cluster port)
- [ ] Notes LoadBalancer with `<pending>` IP as potentially misconfigured
- [ ] If no externally exposed services: says so clearly

## Critical Failures

- Includes ClusterIP services in the results
- Misses NodePort services
- Reports external IPs without showing port numbers
