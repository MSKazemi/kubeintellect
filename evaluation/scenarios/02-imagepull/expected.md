# Scenario 02 — Expected Behavior

## Ground Truth

- **Fault:** Image `nginx:99.99.99-does-not-exist` does not exist on Docker Hub
- **Symptom:** Pod enters `ImagePullBackOff` / `ErrImagePull`
- **Fix:** Correct the image tag (e.g., `nginx:1.25`)

## Expected Agent Path

1. Routes to **Logs agent**
2. Tool calls:
   - `list_pods` or `get_pod_status` → sees `ImagePullBackOff`
   - `describe_pod` → events show `Failed to pull image "nginx:99.99.99-does-not-exist"`
3. Identifies: wrong/nonexistent image tag (not registry auth, not rate limit)
4. Recommends: fix image tag in deployment spec

## Minimum Passing Criteria

- [ ] Identifies `ImagePullBackOff` (not confused with CrashLoop)
- [ ] Shows the exact failing image name
- [ ] Categorizes as tag error (not registry credential problem)
- [ ] Does not suggest deleting and redeploying with same image

## Critical Failures

- Identifies pod as Running or healthy
- Blames registry credentials without evidence
- Suggests adding `imagePullSecrets` when that is not the issue
