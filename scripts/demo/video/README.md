# KubeIntellect — narrated demo video

**Status: built, not published.** The video, subtitles, thumbnail and the YouTube
hand-over text all exist. Nothing has been uploaded, and nothing is committed.

| | |
|---|---|
| **Video** | `out/kubeintellect-demo.mp4` — **7:21** (441.53 s, 13,246 frames), 30 MB, 1920×1080, 30 fps, H.264 high, AAC stereo 48 kHz, `+faststart`, −15.4 LUFS measured (loudnorm target −14), 1 s fade in / 1.2 s out |
| **Subtitles** | `out/kubeintellect-demo.srt` — 117 cues, generated from the script |
| **Thumbnail** | `out/thumbnail.png` — 1280×720 (plus a 320×180 feed preview) |
| **Script** | [`script.md`](script.md) — what is said and when, timings measured |
| **Scene spec** | [`scenes.py`](scenes.py) — the single source of truth |
| **Clip frames** | `shots-dark/chatui/` — the chat recording, decoded to a PNG sequence |
| **Hand-over** | [`youtube.md`](youtube.md) — title, description, 8 chapters |
| **Narration** | `audio/*.wav` + `out/narration.wav`, synthesised offline |

Rebuild any of it with `python3 tts.py && python3 build.py && python3 make_srt.py &&
python3 thumbnail.py && python3 make_script_md.py && python3 make_youtube_md.py`.

### Why the runtime is what it is

A scene lasts `narration + 1.3 s` and nothing else — a terminal scene's transcript is
revealed *over* the narration, so replaying a longer transcript does not add a second of
runtime. The video is exactly as long as the words.

It was 4:52 when this note first said so, and reaching seven minutes was described as
"writing more script, which is a decision to take on merit rather than to reach a number".
Three scenes were then written on merit — the architecture diagram, the chat UI, and the
live Azure capture — and the payoff scene was extended to stop cutting its own evidence.
The runtime followed from that; it was not aimed at.

## Structure

| Act | Content |
|---|---|
| Title | Name, tagline, `kubeintellect.com` and `github.com/MSKazemi/kubeintellect` on the first frame |
| The problem | The evidence is scattered; the method lives in someone's head |
| | The two ways an AI cluster tool fails: it recites, or it acts unasked |
| The answer | Ask in English · it reads the cluster · it stops before it changes it |
| It diagnoses | 01 crashloop · 03 OOMKill · 05 pending pod · 08 whole-namespace triage |
| Attack it | A gate only ever approved has not been shown to gate anything → 07 **denied**, proved denied in-session → 06 **approved**, and the approved fix **did not work** |
| How it works | Coordinator + subagents · snapshot first · the gate at the tool boundary · a role per API key · hash-chained decision log |
| | **The pipeline as a diagram** — signal → sensorium → detectors → investigation → ladder → write chokepoint → three outcomes |
| | **The same server in a browser** — a real recording, retimed 1.74× |
| | **A real cluster in Azure**, 125 days up, captured live while this video was built |
| Honest limits | It inferred one root cause · found is not solved · no metrics backend and it said so |
| Who it is for | AGPL-3.0, self-hosted, links |

## Everything on screen is real

Every terminal scene replays a verbatim transcript from `../transcripts-kq/` — plain-text
renderings of the asciinema casts in `../casts-kq/`, recorded end to end against a live
`kind` cluster. Nothing is typed by hand or reconstructed. Each scene in `scenes.py` names
the transcript it replays and, where the narration makes a factual claim, the exact line
numbers that claim rests on. All eight were independently re-verified on 2026-08-28 —
`../DEMOS.md` § *Verification*.

The two gate scenes are the point of the video and are genuinely live:

- `07-approval-denied.txt` — a human types `deny` at the real `HITL>` prompt, and the
  refusal is verified **inside the session**: `:91` asks how many replicas `web` has, `:93`
  answers **2**. The `⚙ run_kubectl` line at `:85` is the client echoing the command it was
  *asked* to run; `:86` says it was cancelled. The video says so out loud, because a viewer
  will see that line and wonder.
- `06-approval-gate.txt` — a human types `approve`, the restart runs and succeeds, and the
  follow-up turn reports that it **did not fix the fault**, because a restart was never
  going to supply a missing environment variable. That is the correct outcome and it looks
  like a failed demo if left unexplained, so the narration explains it.

## One scene is written and still cannot be filmed

`14-install` is in `scenes.py` with `enabled=False` and its reason, and it is listed at the
bottom of `script.md` so the gap stays visible. `../casts-kq/09-install.cast` exists, but it
installs **2.2.0** from PyPI, and on 2.2.0 the demo's own pre-flight `kubeintellect
--version` exits **2**.

**This blocker moved twice in one night, and the note here was stale after each move.** It
first claimed live PyPI still served 2.2.0 as latest; at 01:40 PyPI reported **2.4.0**; by
03:00 it reported **2.4.1**. Each re-measurement is dated for that reason.

That was not enough to film the scene, because released 2.4.0 **could not start from a plain
`pip install`** — a default-on feature was declared as an optional extra. Two things gated the
scene: a release carrying the fix, and a re-recorded cast against it.

**The release gate is now clear.** `v2.4.1` shipped that fix, and it was re-measured here at
03:00 against the *published* artifact rather than the tree — a clean venv with a fresh `HOME`:

```
pip install kubeintellect==2.4.1   → exit 0
kubeintellect --version            → kubeintellect 2.4.1, exit 0   (2.2.0 exited 2 here)
kubeintellect serve                → no ModuleNotFoundError; GET /healthz → 200
                                     {"status":"ok","version":"2.4.1",...}
```

**One gate remains: the cast.** `../casts-kq/09-install.cast` still installs 2.2.0, so it shows
a version whose own pre-flight fails. Until it is re-recorded the scene stays `enabled=False`,
because the alternative is narrating an install the video never shows.

`13-chat-ui` **was** the second entry here and is now filmed; see below.

## The chat scene: a clip, not a screenshot

`kind="shot"` used to mean one still under a slow pan. It now also means a *clip*: when
`source` names a directory rather than a file, `render_shot` walks the PNG sequence in it
with the scene clock, and the pan is switched off because panning a moving image reads as a
wobble. `ffmpeg` decodes `../chat-ui/chat-ui-crashloop.mp4` into `shots-dark/chatui/` once.

The recording is ~61 s and the narration under it is ~34 s, so the clip is **retimed, not
truncated** — the whole session plays, end to end, at about 1.7x. That is a claim in itself,
so the caption says *replayed faster than real time* out loud and the narration quotes the
**measured** answer time from `../chat-ui/chat-ui-crashloop.json` (15.3 s) rather than
anything a viewer could stopwatch off the video. The file's own note warns about exactly that
confusion.

One wording trap, caught and pinned by a test: that session holds a **read-only key**, so the
refusal it shows is an **RBAC refusal at the role boundary**, not the human approval gate of
scenes 09–11. Calling it "the same gate" would be a small false claim about the mechanism.

## The architecture scene: `kind="flow"`

`12a-flow` animates the pipeline in [`v4/docs/how-it-works.md`](../../../v4/docs/how-it-works.md),
whose own words are *"the stage names are the actual modules … not a simplified teaching
diagram"* — so the scene draws that diagram rather than a friendlier one. Each node carries
the module path the doc names, and a test asserts every one of those paths still exists, so
the diagram cannot quietly become a picture of a system that has moved. Stages light in
narration order; the two zones (`ALWAYS ON · ZERO TOKENS` and `LLM INVOKED`) and the three
outcomes `decide_write()` returns are the doc's own structure.

## Three defects the frame audit found

After the first full render, one frame was extracted from every enabled scene at 78% of its
duration and looked at. That is how all three of these were found — **each had passed the
build, the tests and a green pipeline.**

### The screen was reading the narration's phonetic spelling out

Narration is written phonetically so Piper says `kubectl` and `AGPL-3.0` correctly, and
`SUBS` maps those forms back **for the subtitles**. Card text was never put through the same
map. So a phonetic form written into a bullet reached the *screen* verbatim, and three cards
were affected — including the closing frame, the last thing a viewer sees:

| Scene | Was on screen | Now |
|---|---|---|
| `16-close` | `A G P L three, self hosted` | `AGPL-3.0, self-hosted` |
| `12-how` | `A role per A P I key` | `A role per API key` |
| `04-answer` | `real kube control against your real cluster` | `real kubectl against your real cluster` |

Fixed twice over: the three source strings are corrected, **and** every string a card draws
now goes through `render.written()`, which applies `SUBS` longest-key-first. That makes the
class impossible rather than merely fixed. Two tests cover it — one scans every display
string of every enabled scene for a phonetic key, one checks the renderer normalises anyway.

## Two more defects the first render of these scenes exposed

Both were visible only by extracting a frame from the finished mp4 and looking at it — the
scenes rendered, the tests passed, and the pipeline reported success:

- **the terminal title bar lied.** It was hard-coded to `kq · shop @ ki-demo ·
  AUTONOMY_LEVEL=A2` for *every* terminal scene. True for the eight kq casts, and false the
  moment `13b-azure` put Azure `kubectl` output under it — the bar claimed footage came from
  a session and a cluster it did not. It is now `term_title`, per scene, and a test asserts
  the Azure scene overrides it while the kq scenes do not.
- **a `shot` assumed a 16:9 source.** The window was sized to a fixed 1400 px width; the chat
  capture is 1280x800, so its window came out 921 px tall and ran under the act label at the
  top and beneath the caption bar at the bottom. It is now fitted to the safe area with the
  aspect ratio preserved, and a test recomputes the geometry from the real capture's
  dimensions and asserts it clears both ends.

## The Azure scene: captured live, and honest about what it shows

`13b-azure` replays `../transcripts-kq/10-azure-live.txt`, captured **read-only** on
2026-08-29 against the Azure VM that serves the public demo (RG `kubeintellect`, 20.119.62.10):
`kubectl get nodes -o wide`, `kubectl -n kubeintellect get pods,svc`, and a `curl` of
`https://api.kubeintellect.com/healthz`. Nothing was created, changed or deleted.

It is the strongest scene in the video and the easiest to overstate. The endpoint answers
`{"status":"ok","version":"2.0.0"}` — that deployment has not been updated since April, and
**the narration says so on screen while the version is visible**, because a viewer who reads
`2.0.0` after six minutes of 2.3.1 behaviour is owed the explanation rather than left to
assume. Four tests pin it: the transcript exists, the uptime spoken matches the `125d` in the
captured output, the version spoken matches the captured JSON, and the two sentences that do
the disclosing cannot be deleted without going red.

## The demo was cutting its own payoff

`11-approve`'s narration calls the moment after an approved restart *"the most valuable
thirty seconds in this video, and it is the one a demo normally cuts"*. It was cutting it.

The window was `lines=(108, 140)` and line 140 is `with the same error:` — a colon. It was
the only terminal scene in the video that ended mid-sentence. The evidence the sentence was
pointing at lives on lines 143–145 of `06-approval-gate.txt`: the restarted pod comes back
up, crashes again, and the agent names the same root cause it named at the start —
`DATABASE_URL` is not set, so the container exits with code 1. None of that reached the
screen. The video said "watch this" and then changed the subject.

The window is now `(108, 145)` and the narration was lengthened to match, so the reveal rate
stays inside the 1.5 lines/second readability ceiling the rest of the file is held to. The
scene grew from 22.4 s to 46.7 s, which is most of the runtime this pass added.

`TestThePayoffIsNotCut` pins it: no terminal window may end on a dangling colon, this
window must reach the line naming `DATABASE_URL`, and the reveal rate must stay readable.

### The test fixture would have hidden all of it

While pinning that, the `spoken` fixture in the test file turned out to read
`sc.get("enabled")` with no default. Every scene written before this pass spells
`enabled=True` out in full, so the fixture worked — by coincidence. The three new scenes
were written the way `build.py` reads them, `sc.get("enabled", True)`, which meant they were
**silently excluded from every claim check in the file**: the diagram, the chat UI and the
Azure capture could have claimed anything at all and the suite would still have been green.

The fixture now defaults to `True`, so a scene has to be explicitly switched off to escape
the checks, and the three scenes carry `enabled=True` explicitly to match the house style.
The lesson is the same one the frame audit taught: a green suite is only as wide as what it
actually looks at.

## Two more things the second audit changed

**The Cluster node wrapped badly.** `kubectl watch · PromQL · LogQL` is 30 characters against
a box that fits about 22, so `wrap()` put `· LogQL` alone on the second line — a line opening
with a separator. `render_flow` now splits on an authored `\n` before measuring, and the node
carries its own break. An authored break beats a measured one when the text is this short.

**The Azure title bar named the SSH account.** It read
`kubectl · azureuser @ 20.119.62.10 · read-only`. The host IP is already public through DNS
and `azureuser` is the Azure default, so this leaked nothing secret — but this repo and the
video are both public, and publishing the login of a live production box buys a viewer
nothing. It now reads `kubectl · production cluster in Azure · read-only`, which carries the
same honest signal (this is not the demo cluster) with none of the detail.

## Voice

Piper `en_US-ryan-high` — MIT-licensed, runs fully offline, no paid API and no network at
synthesis time. **The 120 MB voice model is not vendored here**; `tts.py` points at the copy
already on the machine and takes `KI_PIPER` / `KI_PIPER_VOICE` if it moves. Piper is not
deterministic, so each WAV is cached against a SHA-256 of its text — editing one line
re-synthesises one scene, not the whole track. Narration in `scenes.py` is written **phonetically** so acronyms and units
are pronounced correctly (`kube control`, `O O M killed`, `A G P L three`). Subtitles are
read rather than heard, so the `SUBS` table in `scenes.py` restores the written form before
the `.srt` is emitted. **A new scene with a new acronym needs a new `SUBS` entry.**

## Brand — where every colour comes from

Rebuilt 2026-08-28, after the owner's review. What shipped first was the upstream build's
identity, not this project's:

- the accent was `#7c8cf8`, commented *"KubeIntellect indigo"* — a colour in no stylesheet, no
  brand asset and no mark. It arrived with the renderer and nothing ever checked it;
- `logo()` drew **a hexagon with a `K` in it**. The project's mark is a chevron, the lowercase
  `ki` and a cursor block, and it is what kubeintellect.com carries in its navbar and footer on
  every page.

The site is a light page, so its *dark* mapping is taken from the dark panels it already
renders rather than invented here:

| Role | Value | Source |
|---|---|---|
| background | `#0b1220` | website `app/globals.css` → `--ink-panel` ("dark panels/terminals") |
| accent, chrome | `#14b8a6` | `--teal-soft`, what the site uses for mono text on those panels |
| approval gate | `#f59e0b` | `--amber-soft`, which the site labels *human-in-the-loop* |
| healthy outcome | `#00e07a` | the mark's gradient, mid-stop |
| mark tile / stroke | `#0b1020` / `#1e2642` | `v4/docs/assets/brand/ki-c-green.svg` |
| error | `#e87d7d` | **unsourced** — the site defines no error red. Recorded, not hidden |

The mark is drawn from the SVG's own 256-unit geometry with the gradient composited through a
mask, so it is the mark rather than a redrawing of it. One deviation is deliberate and
commented: the SVG hard-codes the cursor at `x=214`, which assumes its own font's advance for
`ki`; JetBrains Mono Nerd is wider, so the type size gives way and the layout is kept.

`v4/tests/test_the_video_says_only_what_the_product_does.py` pins all of it — the mark's
colours against the shipped SVG, the retired indigo against coming back, and every palette
entry against losing the comment that says where it came from.

## Claims — checked against the papers as well (2026-08-28)

The first audit checked the video against the code, the README, the licence and the docs. This
is the other half the owner asked for: against **the papers**.

There are two, and they are different artifacts.

| Paper | State | What it describes |
|---|---|---|
| Journal of Grid Computing 24(3), DOI `10.1007/s10723-026-09837-6` | **published, peer reviewed** | the *earlier* system |
| `papers/v4-operator/` | built and verified, **not submitted** to anything | V4 |

**Nothing in the video presents the V4 paper as reviewed**, and the closing card's wording —
*"an earlier version is described in a peer reviewed paper"* — is exactly right for the
published one. Both are pinned by tests.

### The one real tension, and why the video is still correct

The published paper describes the write path as *"a conversational confirmation step embedded
in the agent's reasoning prompt"*, and its latency table notes the Deletion agent uses
*"conversational confirmation only; no workflow-level `interrupt_before` checkpoint"*.

Scene `12-how` says the opposite: **"the gate is at the tool boundary — not in the prompt, and
not advisory."**

Both are true of their own version. The video describes V4, where the interrupt fires inside
the tool node (`from langgraph.types import interrupt` in `tools/kubectl_tool.py`). What makes
the stronger phrase *"every mutating command"* safe is not an enumeration but an **allowlist**:

```python
return verb not in _READ_ONLY_VERBS      # anything unclassified is treated as a write
```

A verb nobody has classified is gated rather than let through. If that ever inverts to a
deny-list, the word "every" stops being true — and a test fails and says so. A second test
guards the discrepancy itself, so that nobody later "corrects" the video toward the published
paper's description of an older design.

## Claims — checked against the code, not against memory

Also 2026-08-28. `scenes.py` claimed every fact in it was checked against a file in this
repository; that held for the terminal scenes, which name their transcript and line numbers,
and did not hold for the cards, where the product claims live. Three defects:

- **"read-only by default"**, said twice, was **false**. `core/config.py` § *Production auth
  hardening* says the opposite: with no keys configured the server treats every unauthenticated
  caller as `admin`, and `REQUIRE_AUTH` is off. What is true — and is what the video now says —
  is that it reads before it writes, and every mutating command stops at a gate.
- **"a role per API key — read only, operator, admin"** named three of four roles and did not
  say roles exist only once keys are configured.
- Four card citations pointed one directory too high (`../../README.md` resolves to
  `scripts/README.md`, which does not exist). The test now fails on a citation that does not.

The closing narration also spelled a repository URL out loud — *"github dot com slash M S
Kazemi slash kubeintellect"* — which is the sound of a machine, and redundant, since both links
are on the closing card for its full 21 seconds.

## How it was built

The pipeline came from `nova/experiments/azure-2026-08-27/video/`, whose `build.py` /
`render.py` / `tts.py` / `make_srt.py` / `thumbnail.py` are generic. What was adapted here:

- **palette and mark** — see *Brand* below; the upstream palette and its placeholder mark
  were both replaced on 2026-08-28;
- **`load_transcript(name, window)`** — reads `../transcripts-kq/` and slices the
  1-indexed window each scene declares, because the window is what makes the text readable;
- **`colourise`** — rewritten for `kq` output: `You:` prompts, `▸ run_kubectl` tool calls,
  `HITL>` and the gate box, and failure lines (`FATAL`, `OOMKilled`, `did not resolve`);
- **two glyph substitutions** — see below;
- **`enabled=False` is honoured** everywhere, so a scene with no footage is skipped rather
  than rendered blank.

### The one place the screen is not the transcript

The tool-call gear (`U+2699`) and the gate's yellow circle (`U+1F7E1`) are **not in
JetBrains Mono Nerd Font**. Both were checked by rendering them and comparing the bitmap
against `.notdef`: both come out as a slashed box, and a tofu on every `kubectl` line looks
like a broken recording. `render.py` substitutes `▸` and `●`, glyphs the font really has.
That is the only difference between what is on screen and the transcript on disk.

`make_script_md.py` also works with no `durations.json` at all: it then estimates each
scene from word count at 3.1 words/second (Piper's measured rate on this narration —
821 words in 263.6 s). The current timings are measured, not estimated.

`script.md` also reports a **reveal rate** per terminal scene. The transcript window has to
fit the voice: above roughly 1.5 lines/second the text is on screen but cannot be read.
The first draft of `scenes.py` ran at 3.7–4.5 lines/second — the windows were narrowed to
the lines the narration actually points at, and now run 1.0–1.4 against nova's 0.3–1.7.

Target format, matching the nova build: 1920×1080, 30 fps, H.264 high profile, AAC stereo
48 kHz, `+faststart`, −14 LUFS, 1 s fade in / 1.2 s fade out.

## Publishing

**Not uploaded, and not committed.** Uploading to YouTube — and putting these on the
website, the repo README or the docs — is outward-facing and needs the owner's explicit
go-ahead.
