# Scenario 05 — Expected Behavior

## Ground Truth

- **Fault:** PVC `broken-claim` references `storageClassName: fake-storage-class` which does not exist
- **Symptom:** PVC stays Pending; pod stays Pending (volume not bound)
- **Fix:** Change storageClassName to an existing one (or create the storage class)

## Expected Agent Path

1. Routes to **Infrastructure** or **ConfigMapsSecrets** agent
2. Tool calls:
   - `describe_pvc broken-claim` → status: Pending, storageClass: fake-storage-class
   - `list_storage_classes` → shows available classes, none named `fake-storage-class`
   - `describe_pod pvc-app` → events: "waiting for volume to bind" or similar
3. Conclusion: PVC references nonexistent storage class → no provisioner → stays Pending
4. Recommends: use an existing storage class name

## Minimum Passing Criteria

- [ ] Identifies PVC status as Pending (not Bound)
- [ ] Lists available storage classes
- [ ] Names `fake-storage-class` as the missing class
- [ ] Does not blame network, RBAC, or node issues

## Critical Failures

- Reports PVC as Bound
- Does not check storage classes
- Suggests deleting the PVC without explaining root cause
