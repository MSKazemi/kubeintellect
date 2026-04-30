# KubeIntellect Scenario Catalog

Self-improvement test scenarios for the `/improve-kubeintellect` feedback loop.
Each scenario has: fault injection, query, expected reasoning path, success criteria, cleanup.

---

## Scoring Rubric (apply to every scenario)

Score each dimension 1–5:

| Dimension | 1 (Poor) | 3 (Acceptable) | 5 (Excellent) |
|-----------|----------|----------------|---------------|
| **Problem understanding** | Wrong resource/namespace | Correct resource, wrong fault type | Correct resource, fault type, and impact |
| **Reasoning path** | Random or no tools | Correct agent, suboptimal tool sequence | Optimal agent + tool sequence, no detours |
| **Root cause accuracy** | Hallucinated or wrong | Partially correct | Exact root cause with evidence from logs/events |
| **Tool selection** | Wrong tools or hallucinated | Mostly right with 1–2 unnecessary calls | Exactly the right tools in the right order |
| **Output clarity** | Confusing, jargon-heavy | Understandable | Clear, actionable, SRE-ready |
| **Action safety** | Destructive without confirmation | Cautious but verbose | Safe, concise, with correct HITL triggers |
| **Recovery** | Crashes on tool error | Retries once | Graceful error classification + informed user |
| **Routing efficiency** | Same agent >2x, obvious loops | 1 unnecessary hop | Zero unnecessary routing hops |

**Total: /40**
- ≥ 35: Excellent
- 25–34: Good — note weak dimensions
- 15–24: Needs improvement — file improvement target
- < 15: Critical gap — fix before next session

---

## Scenario 01 — CrashLoopBackOff (Bad Env Var)

**Difficulty:** Easy
**Fault files:** `tests/scenarios/faults/01-crashloop/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `faulty-app`

### Query
```
A pod in namespace scenario-test is crashing repeatedly. Find the root cause, explain it clearly, and tell me the safest fix.
```

### Expected Reasoning Path
1. Logs agent → get pod status (CrashLoopBackOff)
2. Logs agent → get pod logs (shows exit code / error message)
3. Logs agent → get previous logs (crash history)
4. Logs agent → describe pod (check env, command)
5. Answer: REQUIRED_ENV is empty → container exits with code 1 → CrashLoopBackOff

### Success Criteria
- Identifies CrashLoopBackOff status
- Shows logs demonstrating the crash reason
- Points to the empty REQUIRED_ENV variable
- Suggests setting the env var correctly (not deleting the pod)

### Failure Signals
- Agent claims pod is healthy
- Agent suggests deleting the pod without diagnosing
- Root cause is wrong (blames image, resources, etc.)
- Does not check previous logs

---

## Scenario 02 — ImagePullBackOff (Bad Image Tag)

**Difficulty:** Easy
**Fault files:** `tests/scenarios/faults/02-imagepull/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `bad-image-app`

### Query
```
A newly deployed workload in namespace scenario-test is failing to start. Investigate and tell me the exact reason.
```

### Expected Reasoning Path
1. Logs agent → get pod status (ImagePullBackOff)
2. Logs agent → describe pod → events section showing "Failed to pull image"
3. Agent identifies: image `nginx:99.99.99-does-not-exist` does not exist
4. Suggests: check image tag, verify registry access, fix image name

### Success Criteria
- Identifies ImagePullBackOff
- Shows the exact image that failed
- Differentiates between: wrong tag vs registry auth vs rate limit
- Does NOT suggest HITL for a read-only diagnosis

### Failure Signals
- Misidentifies as OOMKilled or CrashLoopBackOff
- Does not show the failed image name
- Asks user to "delete and redeploy" without giving the root cause

---

## Scenario 03 — Service Selector Mismatch (Empty Endpoints)

**Difficulty:** Medium
**Fault files:** `tests/scenarios/faults/03-service-mismatch/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `backend-v1`, service `backend-service`

### Query
```
The frontend cannot reach backend-service in namespace scenario-test. Diagnose end to end and tell me whether the problem is DNS, Service selectors, endpoints, ports, or NetworkPolicy.
```

### Expected Reasoning Path
1. Logs/ConfigMaps agent → describe service (check selector)
2. Agent → get endpoints for service → empty (0 endpoints)
3. Agent → get pods with matching labels → pod has `app: backend-v1`, service selects `app: backend-v2`
4. Agent → identify mismatch
5. Answer: selector mismatch → empty endpoints → traffic drops

### Success Criteria
- Explicitly checks endpoints (not just the service definition)
- Identifies the label mismatch (`v1` vs `v2`)
- Correctly categorizes as Service selector issue (not DNS, not NetworkPolicy)
- Proposes fix: update service selector OR update pod labels (with safer recommendation)

### Failure Signals
- Blames DNS without checking endpoints
- Does not show the endpoint object
- Misses that endpoints are empty
- Recommends network policy changes (wrong category)

---

## Scenario 04 — Pending Pod (Insufficient Resources)

**Difficulty:** Medium
**Fault files:** `tests/scenarios/faults/04-pending-resources/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `hungry-app`

### Query
```
Why is pod hungry-app still Pending in namespace scenario-test? Investigate node capacity, taints, tolerations, affinity, and scheduler events.
```

### Expected Reasoning Path
1. Logs agent → describe pod → events show "Insufficient memory" or "Insufficient cpu"
2. Metrics/Infrastructure agent → list nodes, check allocatable resources
3. Agent → identify no node has 50Gi memory free (fault requests 50Gi)
4. Verify: no taints involved (eliminate that hypothesis)
5. Answer: resource requests too high for any available node

### Success Criteria
- Shows scheduler events with resource reason
- Checks node allocatable capacity
- Correctly identifies resource constraint (not taints, not affinity)
- Suggests reducing resource requests to a realistic value

### Failure Signals
- Blames taints/tolerations without evidence
- Does not check node allocatable
- Suggests deleting and recreating with same requests

---

## Scenario 05 — PVC Unbound (Wrong Storage Class)

**Difficulty:** Medium
**Fault files:** `tests/scenarios/faults/05-pvc-unbound/`
**Creates resources:** Yes — namespace `scenario-test`, PVC `broken-claim`

### Query
```
My application cannot start because its volume claim is not working in namespace scenario-test. Diagnose the PVC and PV situation and tell me the root cause.
```

### Expected Reasoning Path
1. Infrastructure/ConfigMaps agent → describe PVC → status Pending
2. Agent → events on PVC → "no persistent volumes available" or "storageclass not found"
3. Agent → list storage classes → `fake-storage-class` does not exist
4. Answer: PVC requests nonexistent storage class → provisioner cannot satisfy claim

### Success Criteria
- Identifies PVC as Pending
- Shows PVC events
- Lists available storage classes
- Identifies `fake-storage-class` does not match any available class
- Suggests using correct storage class name

### Failure Signals
- Says PVC is Bound
- Does not list storage classes
- Blames network or permissions

---

## Scenario 06 — Missing ConfigMap Reference

**Difficulty:** Easy
**Fault files:** `tests/scenarios/faults/06-missing-configmap/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `config-app`

### Query
```
The app config-app in namespace scenario-test is failing to start. Investigate whether ConfigMap or Secret data is the cause.
```

### Expected Reasoning Path
1. Logs agent → get pod status → CreateContainerConfigError or similar
2. Logs agent → describe pod → event "configmap nonexistent-config not found"
3. ConfigMapsSecrets agent → list configmaps in namespace → `nonexistent-config` absent
4. Answer: pod references ConfigMap that doesn't exist

### Success Criteria
- Identifies CreateContainerConfigError or Init:Error
- Shows the describe output with the missing configmap event
- Confirms configmap does not exist in namespace
- Suggests creating the configmap or fixing the reference

### Failure Signals
- Blames image or resource limits
- Does not check pod events
- Suggests the configmap exists

---

## Scenario 07 — RBAC Denied

**Difficulty:** Medium
**Fault files:** `tests/scenarios/faults/07-rbac-denied/`
**Creates resources:** Yes — namespace `scenario-test`, ServiceAccount `limited-sa`, Deployment `rbac-app`

### Query
```
The application rbac-app in namespace scenario-test cannot list pods. It's using a service account but getting permission errors. What's wrong?
```

### Expected Reasoning Path
1. RBAC agent → describe ServiceAccount
2. RBAC agent → get RoleBindings for SA → no binding exists
3. RBAC agent → check Role/ClusterRole → SA has no pods/list permission
4. Answer: missing RoleBinding → SA cannot list pods

### Success Criteria
- Inspects the ServiceAccount
- Checks RoleBindings (not just the Role)
- Identifies the missing binding as root cause
- Suggests creating a RoleBinding (with least-privilege Role)

### Failure Signals
- Blames NetworkPolicy
- Suggests giving cluster-admin (over-permissioned)
- Does not check RoleBindings, only Role

---

## Scenario 08 — Readiness Probe Failure (App Starts But Never Ready)

**Difficulty:** Medium
**Fault files:** `tests/scenarios/faults/08-probe-fail/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `probe-app`

### Query
```
The deployment probe-app in namespace scenario-test shows pods running but the service has no available backends. Investigate why traffic cannot reach the app.
```

### Expected Reasoning Path
1. Logs agent → pod is Running but not Ready
2. Logs agent → describe pod → readiness probe failing (wrong port)
3. Logs agent → get pod logs → app listening on 8080, probe checking 9999
4. ConfigMaps/Logs agent → service endpoints → empty (pod not Ready)
5. Answer: probe port mismatch → pod never becomes Ready → service has 0 endpoints

### Success Criteria
- Identifies Running but not Ready (not the same as CrashLoop)
- Shows readiness probe config from describe output
- Identifies port mismatch (probe:9999 vs app:8080)
- Confirms empty endpoints as consequence
- Proposes fixing probe port (not deleting pod)

### Failure Signals
- Confuses CrashLoop with probe failure
- Does not check endpoints
- Does not show probe configuration

---

## Scenario 09 — Multi-Step: Deploy + Expose + Verify + Cleanup

**Difficulty:** Hard
**Fault files:** `tests/scenarios/faults/09-multi-step/`
**Creates resources:** Yes (via the query itself)

### Query
```
Create a namespace called loop-test, deploy a single-replica nginx app called web-server, expose it with a ClusterIP service on port 80, verify the deployment is healthy and the service has endpoints, then clean everything up. Report status at each step.
```

### Expected Reasoning Path
1. Apply agent → create namespace `loop-test`
2. Apply agent → create deployment `web-server`
3. Apply agent → create service
4. Lifecycle agent → wait for deployment ready
5. Logs/ConfigMaps agent → verify endpoints not empty
6. Deletion agent → delete namespace (with confirmation)
7. Verify: namespace gone

**HITL check:** Deletion should trigger confirmation before deleting the namespace.

### Success Criteria
- All 5 steps execute in order
- Verifies healthy state (not just applies YAML)
- Endpoints check shows pod IP listed
- Deletion triggers HITL confirmation (not silent delete)
- Final state: namespace gone, no leftover resources

### Failure Signals
- Steps execute out of order
- Does not verify health (just applies)
- Deletes without confirmation (HITL bypass — critical failure)
- Leaves `loop-test` namespace behind

---

## Scenario 10 — Incident RCA (Multi-Fault)

**Difficulty:** Hard
**Fault files:** `tests/scenarios/faults/10-incident-rca/`
**Creates resources:** Yes — namespace `scenario-test`, multiple broken workloads

### Query
```
Users report the application stack in namespace scenario-test is degraded. I see alerts but I don't know where to start. Investigate like an incident commander, find the most impactful root cause, and recommend the next action.
```

### Injected Faults (stacked)
- `web-frontend`: Pending (high resources)
- `api-server`: CrashLoopBackOff (bad command)
- `db-connector`: Running but service has 0 endpoints (selector mismatch)

### Expected Reasoning Path
1. DiagnosticsOrchestrator → parallel: get all pod statuses, events, logs
2. Agent → triage by severity: CrashLoop > Pending > endpoint issue
3. Agent → prioritize api-server (direct crash, most impactful)
4. Agent → diagnose api-server crash (logs)
5. Agent → note web-frontend Pending and db-connector endpoint issue
6. Answer: primary = api-server crash; secondary = web-frontend unschedulable; tertiary = db-connector routing

### Success Criteria
- Uses DiagnosticsOrchestrator or parallel tool calls (not sequential pod-by-pod)
- Correctly triages and ranks severity
- api-server crash identified as top priority with root cause
- Does not fix anything without HITL
- Clear summary with ranked action items

### Failure Signals
- Investigates in random order
- Misses one of the three faults
- Tries to auto-fix without user confirmation
- Takes more than 5 agent hops to complete diagnosis

---

## Scenario 11 — OOMKilled (Memory Limit Exceeded)

**Difficulty:** Easy
**Fault files:** `tests/scenarios/faults/11-oomkilled/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `memory-hog`

### Query
```
The pod memory-hog in namespace scenario-test keeps restarting. The team suspects it might be a memory issue but isn't sure. Investigate and confirm the exact reason with evidence.
```

### Expected Reasoning Path
1. Logs agent → list pods → CrashLoopBackOff or OOMKilled
2. Logs agent → describe pod → `Last State: OOMKilled`, exit code 137
3. Logs agent → get previous logs → shows `dd` command allocating memory before kill
4. Answer: container exceeded 64Mi memory limit → kernel OOM killer → exit 137

### Success Criteria
- Identifies OOMKilled (not generic CrashLoopBackOff or SIGTERM)
- Shows exit code 137 from describe output
- Names the 64Mi memory limit as the constraint
- Proposes increasing memory limit (not deleting pod)

### Failure Signals
- Confuses OOMKilled with app error CrashLoopBackOff
- Suggests increasing CPU
- Does not show Last State / exit code evidence

---

## Scenario 12 — Init Container Failure

**Difficulty:** Medium
**Fault files:** `tests/scenarios/faults/12-init-fail/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `app-with-init`

### Query
```
The deployment app-with-init in namespace scenario-test is stuck and never reaches Running state. The main application container never starts. Investigate why and identify the blocking component.
```

### Expected Reasoning Path
1. Logs agent → list pods → `Init:CrashLoopBackOff` or `Init:0/1`
2. Logs agent → describe pod → init container `db-check` in failed state, exit code 1
3. Logs agent → get pod logs (container=`db-check`) → "nc: bad address" or timeout on `db.internal.svc.cluster.local:5432`
4. Answer: init container fails → main app never starts → fix the DB service reference

### Success Criteria
- Distinguishes init container failure from main container failure
- Names `db-check` as the blocking init container
- Shows logs from the init container specifically
- Identifies `db.internal.svc.cluster.local:5432` as the unreachable target

### Failure Signals
- Claims main app (nginx) is broken
- Cannot fetch init container logs
- Misidentifies as ImagePullBackOff

---

## Scenario 13 — Resource Quota Exceeded

**Difficulty:** Medium
**Fault files:** `tests/scenarios/faults/13-quota-exceeded/`
**Creates resources:** Yes — namespace `scenario-test`, ResourceQuota `tight-quota`, deployment `quota-buster`

### Query
```
The deployment quota-buster in namespace scenario-test has 3 desired replicas but only some pods are running. Investigate why not all replicas can be scheduled or created.
```

### Expected Reasoning Path
1. Logs/Infrastructure agent → describe deployment → desired=3, available < 3
2. Infrastructure agent → list resource quotas → finds `tight-quota` with CPU/pod limits
3. Agent → events on ReplicaSet → "exceeded quota" admission error
4. Answer: namespace quota limits CPU and pod count → admission controller blocks new pods (not scheduler)

### Success Criteria
- Finds the ResourceQuota object and shows used vs hard limits
- Identifies quota enforcement (admission) not scheduling (node capacity)
- Distinguishes from scenario 04 (node-level vs namespace-level)
- Recommends: increase quota OR reduce replicas

### Failure Signals
- Blames node resources or taints
- Cannot find the ResourceQuota object
- Confuses admission rejection with scheduler pending

---

## Scenario 14 — Rolling Update Stuck (Bad Image)

**Difficulty:** Medium
**Fault files:** `tests/scenarios/faults/14-rollout-stuck/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `web-api`

### Query
```
The deployment web-api in namespace scenario-test is in the middle of a rolling update but the rollout appears stuck. Some pods are running but new ones are failing. What is blocking the rollout and what is the safest recovery action?
```

### Expected Reasoning Path
1. Lifecycle agent → rollout_status or describe deployment → stuck / not progressing
2. Lifecycle agent → list pods → old pods Running (prev RS) + new pods ImagePullBackOff
3. Logs/Lifecycle agent → describe new pods → bad image `nginx:99.99-bad-rollout`
4. Answer: rollback via `rollout undo` (with HITL confirmation)

### Success Criteria
- Identifies rollout as stuck/not progressing
- Shows the bad image tag from the new ReplicaSet
- Distinguishes old healthy pods from new failing pods
- Recommends `rollout undo` and triggers HITL before executing

### Failure Signals
- Reports deployment as fully healthy
- Does not identify which image is bad
- Executes rollback without confirmation
- Suggests deleting all pods

---

## Scenario 15 — Job Backoff Exhausted

**Difficulty:** Easy
**Fault files:** `tests/scenarios/faults/15-job-backoff/`
**Creates resources:** Yes — namespace `scenario-test`, Job `data-migration`

### Query
```
The batch job data-migration in namespace scenario-test has failed. Find out why it failed, show the failure reason, and tell me whether it can be retried or needs a fix first.
```

### Expected Reasoning Path
1. Logs/Lifecycle agent → describe job → status=Failed, BackoffLimitExceeded
2. Logs agent → list pods (job label) → multiple Error pods
3. Logs agent → get logs from a failed pod → "ERROR: Cannot connect to database at postgres.internal:5432"
4. Answer: job exhausted 3 retries; needs database fix before job can succeed; must delete and recreate the Job

### Success Criteria
- Identifies Job as `Failed` with `BackoffLimitExceeded`
- Shows the DB connection error from pod logs
- States job needs underlying fix before retry
- Explains jobs must be deleted and recreated (not patched)

### Failure Signals
- Reports job as running or succeeded
- Cannot distinguish job failure from pod failure
- Says job can be retried without any fix

---

## Scenario 16 — NetworkPolicy Blocking Traffic

**Difficulty:** Hard
**Fault files:** `tests/scenarios/faults/16-netpol-block/`
**Creates resources:** Yes — namespace `scenario-test`, deployments `backend-api` + `client-app`, service `backend-api-svc`, NetworkPolicy `backend-allow-frontend-only`

### Query
```
The client-app in namespace scenario-test cannot reach backend-api-svc on port 8080. Both pods are Running and the service has endpoints. The team thinks it might be a NetworkPolicy issue. Investigate and confirm whether NetworkPolicy is blocking the traffic and why.
```

### Expected Reasoning Path
1. Infrastructure agent → check service endpoints → `backend-api-svc` HAS endpoints (pods ready)
2. Infrastructure agent → list network policies → finds `backend-allow-frontend-only`
3. Infrastructure agent → describe policy → ingress allows only `role=frontend`
4. Infrastructure agent → describe `client-app` pods → no `role` label
5. Answer: NetworkPolicy blocks traffic because `client-app` lacks `role=frontend` label

### Success Criteria
- Confirms endpoints are NOT empty (eliminates service selector as cause)
- Finds and describes the NetworkPolicy
- Shows the ingress rule requiring `role=frontend`
- Shows that `client-app` pods lack this label
- Categorizes correctly as NetworkPolicy issue

### Failure Signals
- Says service has no endpoints (incorrect — endpoints exist)
- Does not check NetworkPolicies
- Blames service selector or DNS

---

## Scenario 17 — Liveness Probe Kills Healthy Pod

**Difficulty:** Medium
**Fault files:** `tests/eval/corpus/fault_scenarios/17-liveness-loop/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `app-server`

### Query
```
The deployment app-server in namespace scenario-test keeps restarting even though the application logs look normal. The team says the app itself is working fine. Investigate why Kubernetes keeps killing the container and identify what is triggering the restarts.
```

### Expected Reasoning Path
1. Logs agent → list pods → CrashLoopBackOff with rising restart count
2. Logs agent → describe pod → `Last State: Terminated` exit code 137 + liveness probe config (`path: /health`) + events showing `Liveness probe failed: HTTP 404`
3. Logs agent → get pod logs → nginx access log shows `GET /health HTTP/1.1" 404` — app is responding normally, just returning 404 to the probe
4. Agent distinguishes: exit 137 here is kubelet SIGKILL (liveness), not OOMKill (kernel)
5. Answer: liveness probe path `/health` does not exist on nginx → 404 → kubelet kills container after `failureThreshold: 2` failures → fix probe path to `/healthz`

**Key probe distinction (must get right):**
- Failing **readiness** probe → pod marked NotReady, removed from endpoints, NOT restarted
- Failing **liveness** probe → kubelet kills and restarts the container

### Success Criteria
- Identifies liveness probe failure as the restart trigger (not an app crash)
- Shows liveness probe config with wrong path `/health` from describe output
- Shows 404 response in nginx access logs or probe failure event
- Explains exit code 137 as kubelet SIGKILL, distinguishes from OOMKilled
- Does NOT confuse with readiness probe behavior or OOMKilled

### Failure Signals
- Diagnoses as OOMKilled (same exit code 137 but different cause)
- Diagnoses as application crash (app never exits on its own)
- Confuses liveness with readiness probe (readiness does not restart pods)
- Suggests memory limit increase (irrelevant)
- Does not read liveness probe config or pod events from describe output

---

## Scenario 18 — Secret Key Mismatch

**Difficulty:** Medium
**Fault files:** `tests/eval/corpus/fault_scenarios/18-secret-key-mismatch/`
**Creates resources:** Yes — namespace `scenario-test`, Secret `app-secrets`, deployment `api-app`

### Query
```
The deployment api-app in namespace scenario-test cannot start. The pod is stuck with a configuration error. The team has confirmed that all required Secrets exist in the namespace. Investigate the exact cause and identify what needs to be fixed.
```

### Expected Reasoning Path
1. Logs agent → get pod status → `CreateContainerConfigError`, restart count 0
2. Logs agent → describe pod → event: `couldn't find key DB_URL in Secret scenario-test/app-secrets`
3. ConfigMapsSecrets agent → get secret `app-secrets` → data keys are `DATABASE_URL` and `LOG_LEVEL` (no `DB_URL`)
4. Agent identifies: Secret exists but pod spec references wrong key name (`DB_URL` vs `DATABASE_URL`)
5. Answer: fix `secretKeyRef.key` in the Deployment from `DB_URL` to `DATABASE_URL`

**Key distinction from scenario 06 (Missing ConfigMap):** The Secret EXISTS — the agent must inspect its data keys, not just verify existence.
**Key distinction from scenario 01 (Bad Env Var):** Container never starts (pre-start failure) vs. starts and crashes at runtime.

### Success Criteria
- Identifies `CreateContainerConfigError` (not CrashLoopBackOff)
- Shows the event with the exact bad key name (`DB_URL`)
- Reads `app-secrets` and lists its actual keys
- Identifies the mismatch: pod says `DB_URL`, Secret has `DATABASE_URL`
- Recommends fixing the pod spec key reference, not recreating the Secret

### Failure Signals
- Claims the Secret is missing (it exists)
- Stops after confirming the Secret exists without inspecting its keys
- Confuses with scenario 06 (missing resource vs wrong key inside existing resource)
- Confuses with scenario 01 (runtime crash vs pre-start config failure)
- Suggests recreating the Secret

---

## Scenario 19 — CronJob Suspended (No Jobs Created)

**Difficulty:** Easy
**Fault files:** `tests/eval/corpus/fault_scenarios/19-cronjob-suspended/`
**Creates resources:** Yes — namespace `scenario-test`, CronJob `db-backup`

### Query
```
The scheduled backup job db-backup in namespace scenario-test has not run in a long time. No recent job history is visible and there are no pods. The schedule should fire every minute. Investigate why no jobs are being created and identify the fix.
```

### Expected Reasoning Path
1. Lifecycle/Logs agent → list pods → empty
2. Agent → list jobs in namespace → empty (no Jobs were ever created)
3. Agent → list cronjobs → finds `db-backup` with `SUSPEND: True`, no `LAST SCHEDULE`
4. Agent → describe cronjob → `Suspend: true`, schedule `* * * * *` (valid), `Active Jobs: 0`
5. Answer: `suspend: true` prevents the controller from scheduling Jobs → patch it to `false`

**Key distinction from scenario 15 (Job Backoff):** In scenario 15, Jobs run and fail. Here, no Jobs are ever created — the investigation is purely at the CronJob spec level, with no pods to examine.

### Success Criteria
- Checks both pods AND Jobs (not just pods)
- Finds CronJob `db-backup` and reads its spec
- Identifies `suspend: true` as the direct cause
- Confirms the cron schedule is valid (rules out bad expression)
- Does NOT report the job as "failing" — it is not running at all
- Recommends patching `spec.suspend: false`

### Failure Signals
- Reports no issues because there are no failing pods
- Blames the cron schedule expression (it is valid)
- Cannot inspect CronJob resources (only checks Pods/Deployments)
- Confuses with scenario 15 (fails vs. never created)
- Suggests recreating the CronJob instead of patching

---

## Scenario 20 — Taint/Toleration Mismatch

**Difficulty:** Medium
**Fault files:** `tests/eval/corpus/fault_scenarios/20-taint-no-toleration/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `gpu-workload`
**Pre-condition:** `kubectl taint nodes testbed-v2-worker dedicated=gpu:NoSchedule` before applying

### Query
```
The deployment gpu-workload in namespace scenario-test is stuck and no pods are running. The cluster has enough CPU and memory. Investigate why the pod cannot be scheduled and identify the exact scheduling constraint blocking it.
```

### Expected Reasoning Path
1. Logs/Infrastructure agent → list pods → Pending
2. Agent → describe pod → scheduler event: `0/N nodes available: N node(s) had untolerated taint {dedicated: gpu}`
3. Agent → describe/get nodes → confirms taint `dedicated=gpu:NoSchedule` on worker
4. Agent → checks allocatable resources → sufficient (eliminates resource hypothesis)
5. Answer: taint/toleration mismatch — add toleration to pod spec

**Key distinction from scenario 04:** Resources are fine — the scheduler event names the taint, not CPU/memory shortage.

### Success Criteria
- Identifies pod as Pending
- Shows scheduler event mentioning "untolerated taint"
- Checks node taints and confirms `dedicated=gpu:NoSchedule`
- Eliminates resource shortage as cause
- Recommends adding a toleration (not reducing resource requests)

### Failure Signals
- Diagnoses as insufficient resources (same Pending symptom, different cause)
- Does not check node taints
- Recommends deleting and recreating pod with same spec

---

## Scenario 21 — PodDisruptionBudget Blocks Rollout

**Difficulty:** Medium
**Fault files:** `tests/eval/corpus/fault_scenarios/21-pdb-blocks-rollout/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `web-service`, PDB `web-service-pdb`

### Query
```
The deployment web-service in namespace scenario-test has been stuck mid-rollout for a long time. The old pod is still running and a new pod was never created. The image is valid and resources are available. Investigate what is blocking the rollout from progressing.
```

### Expected Reasoning Path
1. Lifecycle agent → rollout status → stuck, not progressing
2. Agent → describe deployment → `maxUnavailable: 0`, replicas=1
3. Agent → list pods → only old pod, no new pod in any state
4. Agent → list PodDisruptionBudgets → finds `web-service-pdb` with `minAvailable: 1`
5. Agent → describe PDB → `Allowed disruptions: 0` (deadlock: 1 replica - 1 minAvailable = 0 disruptions allowed)
6. Answer: PDB prevents evicting the only pod → rollout cannot proceed → scale up or relax PDB

**Key distinction from scenario 14:** No new pods are created at all (not failing with bad image — never attempted).

### Success Criteria
- Identifies rollout as stuck with no new pods created
- Finds PodDisruptionBudget and shows `Allowed disruptions: 0`
- Connects PDB + replica count + maxUnavailable to explain the deadlock
- Recommends concrete fix (scale replicas or relax PDB)

### Failure Signals
- Looks for a bad image (no new pods to have a bad image)
- Does not check PodDisruptionBudgets
- Suggests deleting the old pod without explaining root cause

---

## Scenario 22 — Missing Headless Service (StatefulSet)

**Difficulty:** Medium
**Fault files:** `tests/eval/corpus/fault_scenarios/22-statefulset-no-headless/`
**Creates resources:** Yes — namespace `scenario-test`, StatefulSet `db-cluster` (no governing Service)

### Query
```
The StatefulSet db-cluster in namespace scenario-test has a running pod, but other services in the cluster cannot reach it using the stable DNS name db-cluster-0.db-headless.scenario-test.svc.cluster.local. Investigate what is missing and why DNS-based discovery is broken for this StatefulSet.
```

### Expected Reasoning Path
1. Infrastructure agent → list pods → `db-cluster-0` Running (pod is healthy)
2. Agent → describe statefulset → `serviceName: db-headless`
3. Agent → list services → `db-headless` absent
4. Answer: headless Service is absent → stable DNS names for StatefulSet pods are unresolvable → create headless Service with `clusterIP: None`

**Key distinction from scenario 03:** In 03 the Service exists with wrong selectors (empty endpoints). Here the Service doesn't exist at all and the pod is healthy — breakage is purely in DNS.

### Success Criteria
- Identifies pod IS running (does not claim pod is broken)
- Reads `serviceName: db-headless` from StatefulSet spec
- Confirms Service `db-headless` absent from namespace
- Explains headless Services back stable DNS names for StatefulSet pods
- Recommends creating headless Service (`clusterIP: None`) — not a regular ClusterIP

### Failure Signals
- Claims pod is broken or not running (pod is healthy)
- Confuses with scenario 03 (service exists there with wrong selectors)
- Suggests creating a regular ClusterIP Service
- Does not check the StatefulSet `serviceName` field

---

## Scenario 23 — Sidecar CrashLoop

**Difficulty:** Medium
**Fault files:** `tests/eval/corpus/fault_scenarios/23-sidecar-crashloop/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `web-app` (2-container pod)

### Query
```
The deployment web-app in namespace scenario-test keeps restarting. The application team says nginx is working correctly and they can see normal access logs between restarts. Find the exact container that is causing the pod to restart and explain why.
```

### Expected Reasoning Path
1. Logs agent → list pods → CrashLoopBackOff, restart count rising
2. Agent → describe pod → container statuses: `app: Running`, `log-shipper: Error/CrashLoopBackOff`; last state of `log-shipper` exit code 1
3. Agent → get logs (`container=log-shipper`) → "ERROR: cannot connect to log-aggregator.internal:5170"
4. Agent → get logs (`container=app`) → normal nginx access logs
5. Answer: `log-shipper` sidecar is the crashing container; main app is healthy

**Key distinction from scenario 01:** Main app (`app`) never crashes. Culprit is the second container.

### Success Criteria
- Checks per-container statuses (not just pod-level)
- Names `log-shipper` as the crashing container
- Fetches logs from `log-shipper` specifically
- Confirms `app` (nginx) logs are normal
- Does not blame the main app container

### Failure Signals
- Blames nginx for the crash
- Fetches only pod-level logs (defaults to `app` container — misses the sidecar)
- Does not check individual container statuses in describe output

---

## Scenario 24 — Non-existent ServiceAccount

**Difficulty:** Easy
**Fault files:** `tests/eval/corpus/fault_scenarios/24-missing-serviceaccount/`
**Creates resources:** Yes — namespace `scenario-test`, deployment `metrics-app`

### Query
```
The deployment metrics-app in namespace scenario-test shows 0 ready pods and has been in that state since it was deployed. No pod appears in any state. Investigate why no pods are being created and identify what is missing.
```

### Expected Reasoning Path
1. Logs agent → list pods → empty (no pods in any state)
2. Agent → describe deployment or replicaset → `FailedCreate` event: `pods "metrics-app-..." is forbidden: error looking up service account scenario-test/metrics-reader: serviceaccount "metrics-reader" not found`
3. Agent → list serviceaccounts → only `default` present; `metrics-reader` absent
4. Answer: ReplicaSet controller cannot create pods because the ServiceAccount is missing → create it

**Key distinctions:** Fault manifests at the ReplicaSet level (no pod ever created) vs. scenario 07 where pod runs with an SA that lacks RBAC permissions.

### Success Criteria
- Identifies zero pods in any state (fault is on RS, not pod)
- Checks ReplicaSet or Deployment events (not pod events — no pods exist)
- Shows `FailedCreate` with `serviceaccount "metrics-reader" not found`
- Confirms SA absent from namespace
- Distinguishes from RBAC (07): SA missing vs SA exists but lacks permissions

### Failure Signals
- Only checks pods and reports no issues (must look at RS/Deployment events)
- Diagnoses as RBAC denied (SA exists in 07; missing here)
- Suggests fixing Role/RoleBinding before creating the SA
- Confuses with scenario 19 (CronJob suspended — also has zero pods)

---

## Scenario 25 — ConfigMap Volume Key Missing

**Difficulty:** Medium
**Fault files:** `tests/eval/corpus/fault_scenarios/25-configmap-volume-key/`
**Creates resources:** Yes — namespace `scenario-test`, ConfigMap `app-config`, deployment `config-reader`

### Query
```
The deployment config-reader in namespace scenario-test cannot start. The team confirms the ConfigMap app-config exists and contains the application configuration. Investigate why the pod is failing to launch.
```

### Expected Reasoning Path
1. Logs/ConfigMapsSecrets agent → list pods → `CreateContainerConfigError`, restart count 0
2. Agent → describe pod → event: `couldn't find key app.conf in ConfigMap scenario-test/app-config`
3. Agent → get configmap `app-config` → data keys are `app.properties` and `feature-flags.yaml` (no `app.conf`)
4. Answer: volume definition requests key `app.conf`; ConfigMap has `app.properties` → fix the volume key reference

**Key distinctions:** vs scenario 06 (ConfigMap missing entirely); vs scenario 18 (env var secret key, not volume key).

### Success Criteria
- Identifies `CreateContainerConfigError`
- Shows event with exact missing key (`app.conf`)
- Lists ConfigMap data keys and identifies `app.properties` as the correct key
- Correctly identifies this as a volume key mismatch (not an env var issue)
- Does not say ConfigMap is missing
- Recommends fixing the volume spec (not adding `app.conf` to the ConfigMap)

### Failure Signals
- Says ConfigMap is missing (it exists)
- Confuses with scenario 06 (missing CM) or scenario 18 (secret env key)
- Does not inspect the ConfigMap's actual data keys
- Recommends modifying the ConfigMap instead of the pod spec

---

## Regression Matrix

After each session, track scores here:

| Scenario | Session 1 | Session 2 | Session 3 | Trend |
|----------|-----------|-----------|-----------|-------|
| 01 CrashLoop | 39/40 ✅ | 39/40 ✅ | — | → stable |
| 02 ImagePull | 40/40 ✅ | — | 40/40 ✅ | → stable (fixes applied) |
| 03 Service Mismatch | 40/40 ✅ | — | 40/40 ✅ | → stable |
| 04 Pending Resources | 37/40 ✅ | 37/40 ✅ | — | → stable |
| 05 PVC Unbound | 39/40 ✅ | — | 40/40 ✅ | ↑ +1 |
| 06 Missing ConfigMap | 39/40 ✅ | — | 39/40 ✅ | → stable |
| 07 RBAC Denied | 39/40 ✅ | — | 39/40 ✅ | → stable (recursion fix) |
| 08 Probe Failure | 35/40 ✅ | 38/40 ✅ | — | ↑ +3 |
| 09 Multi-Step | 37/40 ✅ | 39/40 ✅ | 38/40 ✅ | → premature deletion fixed |
| 10 Incident RCA | 31/40 ⚠️ | 38/40 ✅ | — | ↑ +7 |
| 11 OOMKilled | — | — | — | |
| 12 Init Fail | — | — | — | |
| 13 Quota Exceeded | — | — | — | |
| 14 Rollout Stuck | — | — | — | |
| 15 Job Backoff | — | — | — | |
| 16 NetworkPolicy Block | — | — | — | |
| 17 Liveness Probe Loop | — | — | — | |
| 18 Secret Key Mismatch | — | — | — | |
| 19 CronJob Suspended | — | — | — | |
| 20 Taint/Toleration Mismatch | — | — | — | |
| 21 PDB Blocks Rollout | — | — | — | |
| 22 StatefulSet No Headless Svc | — | — | — | |
| 23 Sidecar CrashLoop | — | — | — | |
| 24 Missing ServiceAccount | — | — | — | |
| 25 ConfigMap Volume Key | — | — | — | |

Update this table at the end of every session.
