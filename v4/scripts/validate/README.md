# V4 new-capabilities — validation & enablement runbook (ADR-010/011/012)

A self-contained plan you can run later to (1) finish the pre-registered kill-criteria
validation and (2) turn the features on in a deployment. Everything here is reproducible
from a clean shell; no prior session state is needed.

## What these three features are

| Feature | ADR | Flag (default) | Endpoint / CLI |
|---|---|---|---|
| Anticipatory / predictive detection | 010 | `PREDICTIVE_DETECTION_ENABLED` (off) | `kq findings` (predicted rows) |
| Grounded incident postmortems | 011 | `POSTMORTEM_ENABLED` (on, read-only), `POSTMORTEM_LLM_NARRATIVE` (off) | `GET /v1/episodes/{id}/postmortem`, `kq postmortem` |
| Natural-language detector authoring | 012 | `NL_DETECTOR_AUTHORING_ENABLED` (off) | `POST /v1/detectors`, `kq detector` |

## Status (last validated 2026-06-19 on `kind-testbed-v2`)

| Check | State | Evidence |
|---|---|---|
| F2 postmortem on real recorded episodes | ✅ done | `chain_valid=True`, 100% seq-cited timeline, root_cause from L1 `episodes` |
| F1 trend pipeline on live Prometheus | ✅ done | 46 real series, OLS projection, 0 errors |
| F3 NL→detector compile via real Azure LLM | ✅ done | 6/6 (100%) valid; OOM firing-parity PASS |
| **F1 lead-time A/B (≥20 incidents)** | ⏳ TODO | run §1 below |
| **F2 grounding-at-scale (judge)** | ⏳ TODO | run §2 below |
| **F3 24h shadow soak** | ⏳ TODO | run §3 below |
| **Enable on the Helm deploy** | ⏳ optional | run §4 below |

---

## Prerequisites (run once)

```bash
cd v4
kubectl config use-context kind-testbed-v2     # the testbed cluster
kubectl get nodes                              # must be Ready
curl -s http://localhost:8000/healthz          # v4 server up (or `make serve`)
curl -s "http://prometheus.local/api/v1/query?query=up" | head -c 80   # Prometheus reachable
docker ps | grep v4-postgres-1                 # flight-recorder DB up
export PROMETHEUS_URL=http://prometheus.local  # used by the F1 harness
```

If the server is not running: `cd v4 && make serve` (or your usual launch). The
read-only checks (F2, F3-compile) do **not** require the server — they call the app
modules directly via `uv run`.

---

## §1 — F1 predictive lead-time A/B  ⏳

**Goal / kill criterion:** over **≥20** slow-burn incidents, lead-time `> 0` on **≥60%**
with predicted-finding **precision ≥ 0.7**. (`lead-time = t(realized OOMKilled) − t(predicted finding)`.)

**One incident:**
```bash
cd v4
kubectl create namespace predictive-test
kubectl apply -f scripts/validate/oom_leak.yaml
PROMETHEUS_URL=http://prometheus.local uv run python scripts/validate/f1_lead_time.py
# prints: "lead time = N min → PASS (>0)" once the pod is predicted then OOMKilled
kubectl delete namespace predictive-test      # cleanup (always)
```
Each incident takes ~7–10 min (workload fills a 128Mi limit, gets predicted, then OOMKills).

**Full gate (≥20 incidents):** loop it, collecting lead times into the results table below:
```bash
for i in $(seq 1 20); do
  kubectl create namespace predictive-test 2>/dev/null
  kubectl apply -f scripts/validate/oom_leak.yaml
  PROMETHEUS_URL=http://prometheus.local uv run python scripts/validate/f1_lead_time.py | tee -a /tmp/f1_results.txt
  kubectl delete namespace predictive-test --wait=true
done
grep -c "PASS" /tmp/f1_results.txt    # want >= 12/20 (60%)
```
**Precision (false-positive predictions):** while a *healthy* workload runs (no leak),
predicted findings should stay at 0. Deploy a steady-memory pod for 1h and confirm
`f1_lead_time.py` reports no prediction.

**Troubleshooting:** if nothing is predicted, the pod likely has no memory *limit* (the
ratio metric needs `kube_pod_container_resource_limits`); confirm `oom_leak.yaml` applied
with `limits.memory`. If predicted but never OOMs, raise the leak rate (`count=` in the
workload) or `MAX_MINUTES` in the harness.

---

## §2 — F2 grounded-postmortem faithfulness at scale  ⏳

**Goal / kill criterion:** **≥90%** of LLM-narrative claims grounded (cite a real `seq`)
and **0 hallucinated facts**, judged blind. (The deterministic timeline is already 100%
seq-cited — this gates only the optional LLM narrative.)

```bash
cd v4
export POSTMORTEM_LLM_NARRATIVE=true     # enable the narrative path for this run
# list candidate episodes (>= 8 events) from the flight recorder:
docker exec v4-postgres-1 psql -U kubeintellect -d kubeintellect -t -c \
 "SELECT episode_id FROM decision_log GROUP BY episode_id HAVING count(*)>=8 ORDER BY count(*) DESC LIMIT 10;"
# generate a postmortem (markdown) for one:
uv run python - <<'PY'
import asyncio
from app.db import flight_recorder
from app.digest import postmortem
async def main():
    await flight_recorder.init_recorder()
    pm = await postmortem.build_postmortem("<episode_id>")
    print(postmortem.render_markdown(pm))
    await flight_recorder.close_recorder()
asyncio.run(main())
PY
```
**Judge step:** feed each postmortem + its underlying events to the gpt-5.4 judge (the
paper-campaign judge, `EVAL_JUDGE_AZURE_DEPLOYMENT=gpt-5.4`) with the prompt: *"For each
sentence, is it supported by an event with the cited [#seq]? Count grounded vs ungrounded
and any invented facts."* Record grounded-ratio + hallucination count per episode.

**If it fails (<90% or any hallucination):** keep `POSTMORTEM_LLM_NARRATIVE=false` — the
deterministic seq-cited timeline ships alone (already validated).

---

## §3 — F3 natural-language detector 24h shadow soak  ⏳

**Goal / kill criterion:** **≥70%** NL descriptions compile schema-valid (✅ already 6/6),
and a **bounded shadow false-positive rate** over a 24h soak before any promotion.

```bash
# server must run with NL_DETECTOR_AUTHORING_ENABLED=true
export NL_DETECTOR_AUTHORING_ENABLED=true        # then restart the server
kq detector new "pods stuck terminating for more than 5 minutes"   # → staged as shadow
kq detector list --status shadow
# ... wait 24h on a normally-running cluster ...
kq detector shadow nl:<name>     # review firings: a good detector fires ~0 on healthy traffic
kq detector promote nl:<name>    # only if precision is acceptable
# or: kq detector reject nl:<name>
```
**Safety invariant (already unit-proven):** a `shadow`/`candidate` detector NEVER reaches
the watchtower — it only accrues firings into its buffer until you promote it.

**Firing-parity check (optional, fast):** for 5 failures that have hand-written playbooks,
compile via `kq detector new "<description>"` and compare the predicates to the playbook's
`detect:` block on shared fixtures; want ≥70% firing-parity.

---

## §4 — Enable the features on the Helm deploy  ⏳ (optional)

The flags default off (postmortem read-only is on). Add to
`deploy/helm/kubeintellect/values.yaml` under `config:`:
```yaml
  predictiveDetectionEnabled: true
  postmortemLlmNarrative: false        # leave off unless §2 passes
  nlDetectorAuthoringEnabled: true
```
and the matching explicit env lines to `deploy/helm/kubeintellect/templates/configmap.yaml`
under `data:`:
```yaml
  PREDICTIVE_DETECTION_ENABLED:  {{ .Values.config.predictiveDetectionEnabled | default false | toString | quote }}
  POSTMORTEM_LLM_NARRATIVE:      {{ .Values.config.postmortemLlmNarrative | default false | toString | quote }}
  NL_DETECTOR_AUTHORING_ENABLED: {{ .Values.config.nlDetectorAuthoringEnabled | default false | toString | quote }}
```
Then `helm upgrade` as usual. (These edits were intentionally NOT pre-applied because the
`deploy/` files carry other unrelated in-flight changes; apply them alongside that work.)

To enable locally instead of via Helm, set the env vars in `v4/.env` and restart the server.

---

## Results table (fill in as you run)

| Date | Check | Metric | Gate | Result | Pass? |
|---|---|---|---|---|---|
|  | F1 lead-time (n=20) | % incidents lead>0; precision | ≥60%; ≥0.7 |  |  |
|  | F2 grounding (n=10) | grounded-ratio; hallucinations | ≥90%; 0 |  |  |
|  | F3 shadow soak (24h) | shadow FP rate | bounded |  |  |

---

## Future enhancement (not implemented — has a known race)

**Auto-generate a postmortem when a watchtower episode resolves.** Tempting to call
`build_postmortem(session_id)` at the end of `app/autonomy/watchtower.py::_investigate`,
but the flight recorder writes are batched/fire-and-forget, so the episode may not be fully
flushed when `_investigate` returns → a partial postmortem. Implement only with an explicit
recorder flush/drain first (`flight_recorder._drain`-style) before building. On-demand
`kq postmortem` / the endpoint already covers the use case with no race.

---

## Disable / rollback

All features are flag-gated and fail-open. To revert behaviour to pre-feature: unset (or set
`false`) `PREDICTIVE_DETECTION_ENABLED`, `NL_DETECTOR_AUTHORING_ENABLED`,
`POSTMORTEM_LLM_NARRATIVE` (and `POSTMORTEM_ENABLED=false` to hide the read-only view).
No schema migration was added, so nothing to undo in the database.

## Reference

ADRs `v4/design/adr/010-v4-predictive-detection.md`, `011-…postmortems.md`,
`012-…nl-detector-authoring.md` (private tier). Vision card
`design/vision/ideas/2026-06-19-anticipatory-self-extending-operator{,-sharpen}.md`.
Code commits on `main`: `5b35386` (features), `5faa72c` (real-data fix + this harness).
User docs: `docs/{cli-reference,api-reference,configuration,capabilities}.md`, `CHANGELOG.md`.
