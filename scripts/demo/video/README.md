# KubeIntellect — narrated demo video

**Status: built, not published.** The video, subtitles, thumbnail and the YouTube
hand-over text all exist. Nothing has been uploaded, and nothing is committed.

| | |
|---|---|
| **Video** | `out/kubeintellect-demo.mp4` — **4:52**, 19 MB, 1920×1080, 30 fps, H.264 high, AAC stereo 48 kHz, `+faststart`, −14 LUFS, 1 s fade in / 1.2 s out |
| **Subtitles** | `out/kubeintellect-demo.srt` — 77 cues, generated from the script |
| **Thumbnail** | `out/thumbnail.png` — 1280×720 (plus a 320×180 feed preview) |
| **Script** | [`script.md`](script.md) — what is said and when, timings measured |
| **Scene spec** | [`scenes.py`](scenes.py) — the single source of truth |
| **Hand-over** | [`youtube.md`](youtube.md) — title, description, 8 chapters |
| **Narration** | `audio/*.wav` + `out/narration.wav`, synthesised offline |

Rebuild any of it with `python3 tts.py && python3 build.py && python3 make_srt.py &&
python3 thumbnail.py && python3 make_script_md.py && python3 make_youtube_md.py`.

### It is 4:52, not 7 minutes

A scene lasts `narration + 1.3 s` and nothing else — a terminal scene's transcript is
revealed *over* the narration, so replaying a longer transcript does not add a second of
runtime. The video is exactly as long as the words. The two blocked scenes add roughly 40 s
when they land. Reaching nova's 7 minutes would mean writing more script, which is a
decision to take on merit rather than to reach a number.

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

## Two scenes are written but cannot be filmed yet

Both are in `scenes.py` with `enabled=False` and a reason, and both are listed at the
bottom of `script.md` so the gap stays visible:

- `13-chat-ui` — no live recording of the chat interface exists. The CLI corpus is eight
  terminal casts; the browser surface, which is what a website visitor actually meets, has
  never been recorded.
- `14-install` — `../casts-kq/09-install.cast` exists, but it installs **2.2.0** from PyPI
  while this tree is **2.3.1**, and on 2.2.0 the demo's own pre-flight
  `kubeintellect --version` exits **2**. Publishing 2.3.1 and re-recording comes first;
  shipping this scene as it stands would demonstrate a broken first impression.

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
