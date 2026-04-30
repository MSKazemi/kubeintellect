# Scenario 19 — Expected Behavior

## Ground Truth

- **Fault:** `spec.suspend: true` is set on the CronJob `db-backup`. When suspended, the Kubernetes CronJob controller stops scheduling new Jobs entirely — no Jobs are created, no pods exist.
- **Symptom:** Zero pods, zero Jobs in namespace. CronJob shows no `lastScheduleTime` and `SUSPEND` column is `True` in `kubectl get cronjob`.
- **Key distinction from scenario 15 (Job Backoff):** In scenario 15 the Job runs but fails. Here the Job is never created at all — there is nothing to look at except the CronJob spec itself.
- **Fix:** Patch `spec.suspend` to `false` (`kubectl patch cronjob db-backup -n scenario-test -p '{"spec":{"suspend":false}}'`).

## Expected Agent Path

1. Routes to **Logs agent** or **Lifecycle agent**
2. Tool calls:
   - `list_pods` in `scenario-test` → empty (no pods at all)
   - `list_jobs` in `scenario-test` → empty (no Jobs created)
   - `list_cronjobs` in `scenario-test` → finds `db-backup` with `SUSPEND: True`, no `LAST SCHEDULE`
   - `describe_cronjob db-backup` → confirms `Suspend: true`, schedule `* * * * *` (valid), `Active Jobs: 0`, no last schedule time
3. Agent identifies: CronJob is suspended — controller will not schedule Jobs until `suspend` is set to `false`
4. Recommends: patch `spec.suspend: false`; no HITL required for a read-only diagnosis

## Minimum Passing Criteria

- [ ] Checks for pods AND Jobs (not just pods) — understands CronJobs create Jobs which create pods
- [ ] Finds the CronJob `db-backup` and reads its spec
- [ ] Identifies `suspend: true` as the direct cause
- [ ] Confirms the schedule is valid (rules out a bad cron expression as the cause)
- [ ] Does NOT say the job is failing — it is not running at all
- [ ] Recommends unsuspending the CronJob as the fix

## Critical Failures

- Reports no issues because there are no failing pods
- Blames the schedule expression (it is valid — `* * * * *` fires every minute)
- Cannot inspect CronJob resources (only looks at Pods/Deployments)
- Confuses with scenario 15 (job fails vs. job never created)
- Suggests recreating the CronJob instead of patching `suspend`
