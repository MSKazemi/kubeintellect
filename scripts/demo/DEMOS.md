# The recorded demos

Eight scenarios, recorded end to end against a live cluster. Nothing here is scripted output:
every answer was produced by the deployed server answering a prompt, and every `kubectl` line in
a cast is the cluster's own output at the moment it was asked.

The point of recording all eight rather than one is that a single demo cannot distinguish a
system that diagnoses from a system that recites. These deliberately include a **healthy control
that must not be named**, a **denied** approval, and questions whose honest answer is "I cannot
see that from here" — see [What it did not do](#what-it-did-not-do), which is the most useful
section on this page.

There are two corpora, recorded a day apart against two different deployments, in the
same eight scenarios:

| | `casts-kq/` — **current** | `casts/` — first |
|---|---|---|
| Recorded | 2026-08-27 | 2026-08-26 |
| Driven through | the real `kq` REPL, over a pty | the HTTP/SSE API directly |
| Shows | what a user sees: rendered markdown, panels, the `HITL>` prompt | the raw stream: literal `###` and `**bold**` |
| Cluster | local `kind` (`ki-demo`) | `kind-ki-camp1-c2` on the campaign VM |

The kq corpus supersedes the first one for anything user-facing. The first is kept because it is
the record of a different deployment answering the same eight questions, and the two disagree in
places that are worth knowing about — noted inline below.

## How the kq corpus was recorded

| | |
|---|---|
| Cluster | local `kind` cluster `ki-demo`, server reached at `http://127.0.0.1:30080` |
| Image | `kubeintellect:demo-local`, built from this tree at `d96f2c2` |
| Chart | `kubeintellect-2.3.1`, Helm revision 3, `AUTONOMY_LEVEL=A2` (propose-and-gate) |
| RBAC | `rbac.createClusterOps=true` — without it an approved command fails on `patch` |
| Namespace | `shop`, six workloads, five injected faults + one healthy control |
| Human | `scripts/demo/kq_pty_driver.py` — a scripted stand-in for the operator |
| Recorder | asciinema 2.4.0, via `scripts/demo/record_all_kq.sh`, pinned to 100×34 |

The image is built locally rather than pulled. The published `ghcr.io/mskazemi/kubeintellect:latest`
at the time of recording could not start at all — its virtualenv was built under Python 3.12 while
the runtime stage carries 3.13, so the packages sit in a `lib/python3.12/site-packages` the
interpreter never looks in and the container exits with `No module named uvicorn`. The fix landed
after that image was built and the image was never rebuilt.

`kq_pty_driver.py` stands in for the human at the approval gate. It types into the real REPL
through a pty, and answers `approve` or `deny` at the real `HITL>` prompt. It deliberately does
**not** use the server's `auto_approve` flag: setting that flag would prove the flag works, not
that the gate works.

The operator key is passed via `$KUBE_Q_API_KEY`, never on the command line — an argv key is
visible in `ps` to everyone on the box, and would be baked into any recording of the shell.

### Four things that make a recording lie

All four were live in the first pass of this corpus, and none of them makes a recording *look*
broken. They are fixed in the recorder and pinned by
`evaluation/test_a_cast_recorded_at_the_wrong_width_still_looks_like_a_recording.py` and
`v4/tests/test_demo_gif_renderer.py`.

1. **A width the client never used.** `asciinema` writes the geometry of the terminal it records
   into; the driver was allocating its own pty at a different size. `rich` laid the answers out
   for 100 columns while the header advertised 80, so every replayer wrapped them mid-token and
   dropped what ran past the edge. The driver now sizes its pty from the terminal it is running
   in, and the recorder pins both to 100×34.
2. **A terminal that could not answer.** `prompt_toolkit` asks the terminal where the cursor is;
   nothing was behind the pty to reply, so *"your terminal doesn't support cursor position
   requests"* was printed into every recording above the first answer. The driver now keeps a
   screen model and answers from it.
3. **A GIF that reported a length it did not have.** Frames were deduplicated on cell contents,
   but different characters can paint identical pixels — the spinner is exactly that case. Pillow
   dropped the duplicates on save and folded their durations into the previous frame, so the
   renderer's `--min-frame-ms` and `--max-frame-ms` were being applied to frames that never
   reached the file. It printed *436 frames, 178s* for a file holding 26 frames and 143s.
   Deduplication now happens on rendered bytes.
4. **A row of boxes where the UI has icons.** No single monospace font covers what a terminal UI
   emits: JetBrains Mono has no Braille (the spinner) and no `⚙`, DejaVu Sans Mono has the gear
   but not the Braille, and nothing monospace here carries the `🟡` risk marker on the approval
   banner — the one frame this whole corpus exists to show. Fonts are now picked *per character*
   from a chain, and coverage is tested by comparing a glyph's bitmap against that font's
   `.notdef`, because the missing-glyph box has ink and every "does this font have it?" check
   that asks for a bounding box says yes.

Points 3 and 4 pull against each other, which is worth knowing before touching either. A cast of
a real session is mostly dead time: the model thinks for tens of seconds, then paints an answer
in a few hundred milliseconds. At 1× the GIF spends its length on a spinner and flashes the
content past. While the spinner had no glyphs it deduplicated away by accident — every frame of
it painted the same box — so fixing the fonts put roughly 60% of every recording back as frames
and the GIFs went to 76–178 s.

The rule that replaces the accident is explicit: **a frame that changes at most `--idle-cells`
cells has not changed the screen — it is an animation, and its time is dead time.** The default
of 2 comes from measuring the corpus, where the only change smaller than twelve cells is the
spinner, at exactly one. The comparison is against the last frame *kept*, not the last one
sampled, which is what keeps typing (one *more* cell each frame, accumulating) on the content
side of the line while a spinner (one cell, oscillating forever) stays on the dead-time side.
`--max-frame-ms` then caps that waiting and `--min-frame-ms` holds the reading, which takes these
eight from 74–143 s each down to 32–51 s. No frame of content is dropped; what is dropped is the
spinner, so the GIFs show it paused rather than turning.

## Reproducing

The cluster must be at **A2** first. At A0 or A1 the agent never proposes a mutating action, so
scenarios 06 and 07 record a conversation with no gate in it — the recording still succeeds and
still looks fine, which is exactly how a demo corpus stops proving anything without failing.

```bash
# 1. put the SUT at propose-and-gate, with write verbs and an operator-or-above key
helm upgrade kubeintellect v4/deploy/helm/kubeintellect -n kubeintellect --reuse-values \
  --set config.autonomyLevel=A2 --set rbac.createClusterOps=true \
  --set secrets.operatorApiKeys="$(cat ~/.ki-demo-operator-key)" --wait

# 2. inject the faults, and give them ~150s to reach their steady states
kubectl apply -f scripts/demo/env/

# 3. record all eight, then rebuild the reading copies and the GIFs
bash scripts/demo/record_all_kq.sh
bash scripts/demo/render_all_kq.sh
```

A `readonly` key is not enough: it is rejected before it can ever reach a gate, and the driver
will tell you so rather than failing. That is the same trap as the autonomy level — a run that
proves nothing exits 0.

The `.cast` files are the source of record; the GIFs and transcripts are build artifacts
regenerated from them. Each cast carries a `.prompts.txt` beside it holding exactly the prompts
that were sent, because scenario files get edited and a recording has to stay checkable against
what was sent to *it*.

## The scenarios

| # | Situation | Injected fault | Did it find it? |
|---|---|---|---|
| 01 | A pod that will not stay up | `payments-api` exits 1: `FATAL: DATABASE_URL is not set` | ✅ quoted the log line and the `payments-db` Secret hint |
| 02 | A rollout that never finished | `checkout` pinned to `nginx:5.1.0-does-not-exist`, `maxUnavailable: 0` | ✅ |
| 03 | A worker the kernel keeps killing | `report-worker` allocates 100 MB against a 64Mi limit → OOMKilled, exit 137 | ✅ |
| 04 | A Service returning no data, pods all `Running` | `inventory` Service selects `app: inventory-api`; pods are `app: inventory` | ✅ root cause and the exact `kubectl patch` — but *inferred*, see below |
| 05 | A pod that will never schedule | `ml-scorer` requests 64 CPU / 512Gi | ✅ and correctly called it a *manifest* problem, not a capacity problem |
| 06 | **Human-in-the-loop — approved** | mutating request on a real deployment | ✅ gated, approved, executed — see below |
| 07 | **Human-in-the-loop — denied** | mutating request on the *healthy* deployment | ✅ gated, denied, **cluster untouched** — see below |
| 08 | Triage a whole broken namespace | six workloads, five faults, one healthy | ✅ **5 of 5**, and ranked by impact — see below |

## The approval gate, in both directions

A gate that is only ever approved has not been shown to gate anything. Both directions were
recorded, and both were checked against the cluster afterwards rather than against the transcript.

**06 — approved.** The agent proposed the literal command and stopped. `kq` draws its own panel
over the server's banner, and the prompt changes to `HITL>`:

```
🟡 Approval Required — risk level: MEDIUM
Command:
    kubectl rollout restart deployment payments-api -n shop

╭──────── Approval required ────────╮
│ This action needs your approval   │
│ before it runs.                   │
│  /approve to run it   /deny …     │
╰───────────────────────────────────╯
HITL> approve
```

After `approve`, the cluster shows the action really happened:

```
spec.template.metadata.annotations
  {"kubectl.kubernetes.io/restartedAt":"2026-08-27T07:56:45Z"}
payments-api-56f659864c   2   2   0   (new ReplicaSet, created at the moment of approval)
```

This cast is also the README hero image. `.github/assets/kubeintellect-demo.gif` is rendered
directly from `casts-kq/06-approval-gate.cast` — the whole story, diagnosis through gate through
verification, with no frame removed. Before this corpus existed the hero was assembled by
`build_demo_cast.py` from answer text of an earlier session; that script is kept, but it is no
longer what the README shows.

**07 — denied.** Same gate, answered `deny`:

```
HITL> deny
⚙ run_kubectl → Running: kubectl scale deployment web -n shop --replicas=10
```

That second line is the client echoing the command it proposed — **not** a record of it running.
The proof is outside the session: `web` `spec.replicas` is still **2**. That is the only evidence
that matters, because the transcript saying "canceled" would read identically if the gate were
cosmetic.

### A gate and a sentence that says "may I?" are not the same thing

07 opens with an investigation turn (*"why is checkout not rolling out?"*) before the mutating
request. That is not decoration. On this deployment, a **cold** single-turn mutating request came
back as prose — *"this requires approval… shall I proceed?"* — with no structured gate behind it,
six times out of six, for `scale`, `restart` and `delete` alike. The run continues whatever you
answer. In a transcript that is indistinguishable from a gate, and it is not one.

The first corpus, recorded against the campaign VM, *did* gate a cold single-turn `scale`. So this
is not a rule about the system; it is a behaviour that varies with the deployment, which is
exactly why the recording checks for `HITL>` — drawn by the client only once it has actually
entered the approval state — rather than for the words "approval required" in an answer.

## What it did not do

**04 reasoned its way to the selector rather than reading it.** The fault is a Service selecting
`app: inventory-api` over pods labelled `app: inventory`. This run never read the offending
value. It saw `endpoints/inventory <none>`, ran `kubectl get pods --show-labels`, and concluded
the selector *"is likely misconfigured"* — the right root cause and the right patch command,
reached without ever looking at the thing it was diagnosing. The first corpus, on the other
deployment, named `app: inventory-api` outright. The answer is correct either way; the confidence
in it should not be the same.

**08 finds the silent service but does not solve it.** Asked to triage the whole namespace, it
lists all five faults — including `inventory`, whose pods all read `Running` and whose only
symptom is a Service with no endpoints — categorises it as its own incident, and ranks it second
by user impact. It stops at *"likely due to pod label mismatch or pod readiness issues"* and files
the diagnosis as a next step. Found is not the same as solved.

This is the one place the two corpora disagree outright: **the first corpus missed `inventory`
entirely**, in all three turns, checked by grep. Two recordings of the same prompt against two
deployments, one finding it and one not, is worth more than either on its own — unprompted
discovery of a fault with no pod-level signal is not something to state as a capability.

**The telemetry backends are not there, and it says so.** This cluster ships no Loki and no
Prometheus; both URLs are empty in the ConfigMap. 04 asks a question that sends the agent to both,
and the answer reports them unavailable and falls back to what `kubectl` can show. An agent that
answered the metrics question anyway would be the worst failure in the corpus, because an invented
number is indistinguishable from a real one.

## Files

```
scripts/demo/
  env/                    manifests that inject the faults (and the healthy control)
  scenarios/              the prompts, one file per demo, '#' comments explain the intent
  kq_pty_driver.py        drives the real REPL through a pty; answers the gate
  record_all_kq.sh        records the eight casts (needs a cluster)
  render_all_kq.sh        rebuilds transcripts and GIFs from the casts (needs nothing)
  casts-kq/               asciinema v2 recordings — the source of record
  casts-kq/*.gates.jsonl  one line per approval gate: turn, command, answer
  casts-kq/*.prompts.txt  the prompts as sent, frozen with the cast
  transcripts-kq/         plain-text renderings, for reading and grepping
  gifs-kq/                regenerated from the casts
  casts/, transcripts/, gifs/    the first corpus, recorded off the SSE stream
```
