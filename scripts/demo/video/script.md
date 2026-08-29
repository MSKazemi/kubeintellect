# KubeIntellect — narrated demo script

_Generated from `scenes.py` by `make_script_md.py`. Timings are measured from the synthesised narration._

Voice: Piper `en_US-ryan-high` (MIT, offline, no paid API and no network at synthesis).
Every terminal scene replays a verbatim transcript from `../transcripts-kq/`, recorded against a live cluster and re-verified 2026-08-28 (`../DEMOS.md` § *Verification*). Nothing on screen is typed by hand.

### `0:00` — 01-title  (card, 12.9s)

**Checked against:** `../../../README.md (tagline, links)`

> KubeIntellect. Human governed A I S R E for Kubernetes. Everything you are about to see is a real recording against a real cluster. No mock ups, no fixtures, no output typed by hand.


## THE PROBLEM

### `0:12` — 02-problem  (card, 18.5s)

**On screen:** Why this is hard

**Checked against:** `../DEMOS.md § The scenarios`

> A pod is failing, and the person who knows why is asleep. The evidence is scattered across pod status, events, logs, the deployment spec and, often, a Secret that is not there. None of that is difficult. Knowing the order to look in is the whole skill, and it lives in somebody's head.

### `0:31` — 03-two-failures  (card, 17.1s)

**On screen:** The two failure modes of an AI cluster tool

**Checked against:** `../DEMOS.md § The approval gate, in both directions`

> Point a language model at this and it fails in one of two ways. Either it recites. Fluent, generic advice that never actually looked at your cluster. Or it acts. It runs the command it thought of, without asking, and now your cluster is different and nobody decided that.


## THE ANSWER

### `0:48` — 04-answer  (card, 16.9s)

**On screen:** What KubeIntellect is

**Checked against:** `../../../README.md` · `../DEMOS.md`

> KubeIntellect is the third thing. You ask in plain English. It investigates with real kube control against your real cluster, reading before it writes. It explains by quoting what it read. And the moment it wants to change anything, it stops and asks a human.


## IT DIAGNOSES

### `1:05` — 05-crashloop  (terminal, 23.0s)

**On screen:** A pod that will not stay up

**Source:** live transcript `../transcripts-kq/01-crashloop.txt`, lines 59–86

**Claims rest on:** `transcripts-kq/01-crashloop.txt:71 (the command), :84-85 (the previous-container logs)`

> Here is the first one, unedited. A pod that will not stay up. It does not guess from the pod name. It reads the log, and it quotes the line back: fatal, DATABASE underscore U R L is not set, refusing to start. Then it finds where that value was supposed to come from. A Secret called payments dash D B. That is the difference between diagnosing and describing.

### `1:28` — 06-oomkill  (terminal, 16.7s)

**On screen:** A worker the kernel keeps killing

**Source:** live transcript `../transcripts-kq/03-oomkill.txt`, lines 43–64

**Claims rest on:** `transcripts-kq/03-oomkill.txt:59,62,63`

> The same again, on a different kind of failure. A worker the kernel keeps killing. Limit, sixty four mebibytes. Reason, O O M killed. Exit code one three seven. Three facts, all read from the cluster, and the conclusion follows from them.

### `1:44` — 07-pending  (terminal, 21.8s)

**On screen:** Capacity problem, or manifest problem?

**Source:** live transcript `../transcripts-kq/05-pending-pod.txt`, lines 113–133

**Claims rest on:** `transcripts-kq/05-pending-pod.txt:113-114`

> This one is the question an operator actually asks. A pod that will never schedule. Is this a cluster capacity problem, or a manifest problem? The honest answer is the second one. A pod asking for sixty four C P Us and five hundred and twelve gibibytes of memory is not a cluster that is too small. It is a manifest that is wrong.

### `2:06` — 08-triage  (terminal, 21.2s)

**On screen:** One namespace, six workloads, five faults, one healthy control

**Source:** live transcript `../transcripts-kq/08-complex-triage.txt`, lines 134–155

**Claims rest on:** `transcripts-kq/08-complex-triage.txt:89 (found), :135 + :143 (ranked, on screen)`

> Now the whole namespace at once. Six workloads. Five of them broken, one deliberately healthy, and the healthy one must not be named. It finds all five, including the one with no pod level symptom at all, and here it is ranking them by user impact rather than listing them. The silent Service, the one with no failing pod, comes second.


## ATTACK IT

### `2:27` — 09-gate-card  (card, 14.6s)

**On screen:** How the gate was tested

**Checked against:** `../DEMOS.md § The approval gate, in both directions`

> Everything so far was read only. This is the part that matters. A gate that is only ever approved has not been shown to gate anything, so both directions were recorded, and both were checked against the cluster afterwards rather than against the transcript.

### `2:42` — 10-deny  (terminal, 23.0s)

**On screen:** Denied — and then proved denied, in the same session

**Source:** live transcript `../transcripts-kq/07-approval-denied.txt`, lines 65–95

**Claims rest on:** `transcripts-kq/07-approval-denied.txt:84-87,91-93`

> Scale the web deployment to ten replicas. It stops. A human types deny. Watch the next line carefully, because it looks wrong. The client echoes the command it was asked to run, and then says: cancelled. The proof is the question after it. How many replicas does web have now? Two. The refusal is verified inside the session, against the cluster.

### `3:05` — 11-approve  (terminal, 48.0s)

**On screen:** Approved — and it did not fix the fault

**Source:** live transcript `../transcripts-kq/06-approval-gate.txt`, lines 108–145

**Claims rest on:** `transcripts-kq/06-approval-gate.txt:112 (gate), :127 (approve), :135 + :138 (it did not fix it)`

> The other direction. Medium risk. A human types approve. The restart runs, and it works. And then the follow up question: did the restart change anything? No. The pods fail with the same error, because a restart was never going to supply a missing environment variable. It says so. That is the most valuable thirty seconds in this video, and it is the one a demo normally cuts. So watch what it actually returns, rather than taking my word for it. It re-reads the pods. It re-reads the events. And it names the same root cause it found at the very beginning: the DATABASE underscore U R L environment variable is not set, so the container exits with code one. The restart was a reasonable thing to try and the wrong thing to fix it — and the system is the one telling you that, not me.


## HOW IT WORKS

### `3:53` — 12-how  (card, 32.8s)

**On screen:** The parts that matter

**Checked against:** `../../../v4/docs/how-it-works.md § Reading the diagram` · `../../../v4/docs/security.md § 2. Role capabilities` · `app/db/flight_recorder.py`

> Briefly, how. A coordinator plans, subagents fetch in parallel, and only then does it conclude. A cluster snapshot is taken first, which biases a healthy cluster toward answering from the snapshot rather than fanning out. The approval gate sits at the tool boundary, not in the prompt, which is why it cannot be talked around. Once you configure keys, each one carries a role: read only, operator, admin or superadmin. And every decision is written to a hash chained log, so a run can be replayed after the fact rather than remembered.

### `4:26` — 12a-flow  (flow, 56.9s)

**On screen:** Perception is free; the model is the expensive part, so it is entered last

**Checked against:** `../../../v4/docs/how-it-works.md` · `../../../v4/docs/security.md § 2. Role capabilities`

> Here is the whole pipeline. Signals arrive from the cluster — kube control watches, Prometheus queries, log queries — and the sensorium normalises each one into a single observation shape. Detectors are compiled predicates. They are always on, they run on every observation, and they cost zero tokens, because no model is involved. That is the point of the design: watching your cluster is free. Only when a detector actually fires is the L L M invoked at all, and only then does an investigation correlate the evidence into a root cause. If that investigation wants to change something, it does not simply change it. The proposal goes to the autonomy ladder, A zero through A three, and then through one write chokepoint — decide underscore write — which returns exactly three answers: do it, ask a human, or refuse. Every one of those outcomes is appended to a hash chained flight recorder, so the run can be replayed afterwards instead of remembered.

### `5:23` — 13-chat-ui  (shot, 34.9s)

**On screen:** The same server, in the browser — replayed faster than real time

**Source:** `chatui`

> Not everyone lives in a terminal, so the same server answers in a browser. This is a real recording, replayed here faster than it happened. The first question is the crash loop you already saw, and it settled in fifteen point three seconds — measured from pressing enter, not timed off this video. Then watch what happens on the second one. The user asks it to scale a deployment to three replicas. This session is holding a read only key, so the write is refused at the role boundary — before it is ever proposed, let alone run. Same evidence, same server, a different door.

### `5:58` — 13b-azure  (terminal, 32.7s)

**On screen:** A real cluster in Azure — captured live while this video was built

**Source:** live transcript `../transcripts-kq/10-azure-live.txt`, lines 1–17

> None of this is a laptop demo. This is a two node cluster running in Azure, and it has been up for one hundred and twenty five days. The server, its Postgres, and the ingress in front of them have been running for four months without attention. That endpoint, api dot kubeintellect dot com, is what the public demo talks to, and it answers health Z right now. One honest note while it is on screen: it reports version two point zero. That deployment has not been updated since April. The code you have been watching is newer than the box serving that URL.

### `6:30` — 14-install  (terminal, 42.3s)

**On screen:** Install it

**Source:** live transcript `../transcripts-kq/09-install.txt`, lines 1–47

> Installing it is one pip command, and that one command brings both pieces: the server, and the kq client you have been watching. Use pip rather than uv tool here, because uv tool links only the executable of the package you name, and kq belongs to a different distribution. Then a wizard asks four questions. Which model provider. Your key, and it offers the one it already found in the environment. Whether to create a local cluster. And whether to start the server now. Notice that it finds kubectl missing and installs that itself, without being asked. At the end it writes two configuration files and mints an admin key. Everything on this screen is a clean container that had nothing installed on it a minute earlier.


## HONEST LIMITS

### `7:13` — 15-limits  (card, 29.5s)

**On screen:** The most useful section of the docs

**Checked against:** `../DEMOS.md § What it did not do`

> What it did not do. In one run it reached the right root cause without ever reading the value it was diagnosing. Correct, but not confident. In another it found the silent Service and then stopped at a next step, rather than solving it. Found is not solved. This cluster ships no metrics backend, and when asked a metrics question it reported the backend unavailable instead of inventing a number, which is the failure that would matter most. All of that is written down, in the same page as the demos.


## WHO IT IS FOR

### `7:42` — 16-close  (card, 21.1s)

**Checked against:** `../../../README.md (licence badge, DOI badge)`

> KubeIntellect is A G P L three and self hosted. It runs where your cluster runs, with your keys, and it does not change anything without a human answering a gate. The recordings in this video, and the page listing everything they did not do, are both in the repository. It is all at kubeintellect dot com — the repository link is on screen.

---

**Total (enabled scenes):** 8m03s


### Reveal rate

A terminal scene is revealed over its narration, so the transcript window has to fit the voice. Above roughly **1.5 lines/second** the text is on screen but unreadable. (The nova build runs 0.3–1.7, median ~0.9.)

| Scene | lines/second |
|---|---|
| `05-crashloop` | 1.2 |
| `06-oomkill` | 1.3 |
| `07-pending` | 1.0 |
| `08-triage` | 1.0 |
| `10-deny` | 1.3 |
| `11-approve` | 0.8 |
| `13b-azure` | 0.5 |
| `14-install` | 1.1 |
