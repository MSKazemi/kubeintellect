# Scenario 42 — Expected Behavior

## Ground Truth
- **Task:** CronJob named log-cleaner in scenario-test, schedule every 6 hours, busybox:1.36, command: find /tmp -mtime +1 -delete
- **Success:** CronJob resource exists with correct schedule (0 */6 * * *)

## Expected Agent Path
1. Generates a CronJob manifest with schedule "0 */6 * * *", image busybox:1.36
2. Sets command correctly: ["find", "/tmp", "-mtime", "+1", "-delete"]
3. Requests HITL confirmation, applies to scenario-test
4. Confirms CronJob created

## Minimum Passing Criteria
- [ ] Schedule is every 6 hours (0 */6 * * * or equivalent)
- [ ] Image is busybox:1.36
- [ ] Command matches the find invocation
- [ ] Namespace is scenario-test
- [ ] Applies the manifest after HITL

## Critical Failures
- Wrong cron schedule (not 6-hourly)
- Omits the find command
- Creates in wrong namespace
