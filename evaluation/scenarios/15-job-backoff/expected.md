# Scenario 15 — Expected Behavior

## Ground Truth

- **Fault:** Job `data-migration` exits with code 1 on every attempt; error: "Cannot connect to database at postgres.internal:5432"
- **Symptom:** Job reaches `backoffLimit: 3` → enters `Failed` state after 3+1=4 attempts; pods show Completed/Error
- **Fix:** Fix the database connectivity (ensure `postgres.internal` service exists) before retrying the job

## Expected Agent Path

1. Routes to **Logs agent** or **Lifecycle agent** (job is a batch workload)
2. Tool calls:
   - `describe_job data-migration` or describe resource → status=Failed, reason=BackoffLimitExceeded
   - `list_pods` → pods with label `app=data-migration` → show Error status (multiple attempts)
   - `get_pod_logs` (any failed pod) → shows "ERROR: Cannot connect to database at postgres.internal:5432"
3. Identifies: job exhausted retries; cannot be retried as-is (needs database fix first)
4. Recommends: fix postgres.internal connectivity, then recreate the job (jobs are immutable after completion)

## Minimum Passing Criteria

- [ ] Identifies the job as `Failed` with `BackoffLimitExceeded`
- [ ] Shows the actual error message from pod logs ("Cannot connect to database at postgres.internal:5432")
- [ ] States that the job cannot be retried without fixing the underlying issue
- [ ] Explains that a failed Job must be deleted and recreated (not patched)
- [ ] Does NOT suggest the job can be resumed in-place

## Critical Failures

- Reports job as succeeded or running
- Cannot distinguish job failure from pod failure
- Does not show the log output with the DB error
- Says the job can be retried without any fix
