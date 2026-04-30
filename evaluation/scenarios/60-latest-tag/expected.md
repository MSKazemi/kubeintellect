# Scenario 60 — Expected Behavior

## Ground Truth
- **Task:** Audit running pods for :latest image tags
- **Success:** Queries cluster, correctly identifies :latest images, explains the risk

## Expected Agent Path
1. kubectl get pods -A -o json → parse spec.containers[*].image
2. Filter images ending in :latest or with no tag (implicit latest)
3. List: pod name, namespace, container, image
4. Explain: no digest pinning → silent updates, supply-chain risk, no reproducibility

## Minimum Passing Criteria
- [ ] Checks all namespaces
- [ ] Correctly detects both explicit :latest and untagged images
- [ ] Shows actual image names from cluster
- [ ] Explains supply-chain and reproducibility risk

## Critical Failures
- Misses images without explicit :latest (untagged = implicit latest)
- Reports pods without kubectl call
- Generic advice not backed by actual cluster data
