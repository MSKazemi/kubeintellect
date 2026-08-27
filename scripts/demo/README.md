# `scripts/demo/` — the demo assets, and how to record them

Everything here exists to produce recordings that are **evidence**, not illustration: a real
server, a real cluster, a real approval gate, and a real answer. The write-up of what the
recordings show — and what they failed to show — is [`DEMOS.md`](DEMOS.md).

| Script | What it does | Needs |
|---|---|---|
| `kq_pty_driver.py` | Drives the real `kq` REPL through a pty and answers the gate | a running server, `pyte` |
| `record_all_kq.sh` | Records one cast per scenario, gate log beside each | a cluster at **A2** |
| `render_all_kq.sh` | Rebuilds transcripts and GIFs from the casts | nothing but the casts |
| `cast_to_gif.py` | Renders an asciinema v2 cast to a GIF | `pyte`, `pillow` |
| `cast_to_text.py` | Renders a cast to plain text, for reading and grepping | `pyte` |
| `auto_approve_driver.py` | The older driver: same job, but over the HTTP/SSE API | a running server |
| `build_demo_cast.py` | Builds a *scripted* cast from the shipping `kq` renderer | `rich` |

---

## `kq_pty_driver.py` — record what a user actually sees

The first corpus was driven against the HTTP/SSE API, so the casts showed literal `###` and
`**bold**` where a user sees rendered output. `kq` renders through rich's Markdown, but it takes
input through prompt_toolkit and refuses to run without a terminal. This driver allocates one.

```bash
KUBE_Q_API_KEY="$(cat ~/.ki-demo-operator-key)" \
  python scripts/demo/kq_pty_driver.py \
    --scenario scripts/demo/scenarios/06-approval-gate.txt \
    --url http://127.0.0.1:30080 --json-log gates.jsonl
```

Three things about it are load-bearing, and each one is a bug that produces a recording which
still *looks* fine:

**Turn boundaries are silence, not the prompt string.** The prompt is themed, drawn by
prompt_toolkit, and rewritten in place, so it is not reliably greppable in the byte stream.
"Nothing for `--quiet` seconds" is a property of the stream itself and survives retheming.

**The pty is sized from the terminal the driver is running in**, never a size of its own
choosing. `asciinema` writes the geometry of the terminal *it* records into. Pick a different one
here and rich lays the answer out for a width the cast header does not declare — every replayer
then wraps it mid-token and drops what runs past the edge, with nothing anywhere reporting an
error.

**It answers the cursor-position query.** prompt_toolkit asks the terminal where the cursor is
(`ESC[6n]`). There is no terminal behind a pty — the driver is the terminal — so it keeps a pyte
screen model and replies from it. Without that, every recording carries *"your terminal doesn't
support cursor position requests"* above its first answer.

### It answers the gate, it does not remove it

The server has an `auto_approve` request flag and this does **not** use it. That flag *skips* the
gate, so a recording made with it would be missing the exact moment worth showing, and a gate
defect would be hidden rather than exposed.

It also does not treat prose as a gate. A model that writes *"this requires approval… shall I
proceed?"* has narrated, not gated: the run continues whatever you answer. The driver matches the
server's `Approval Required` banner **and** `kq`'s own `HITL>` prompt, which the client draws only
once it has really entered the approval state.

| Flag | Why |
|---|---|
| `--deny-nth N` | Deny the Nth gate instead of approving — proves a denial stops the action |
| `--json-log FILE` | One JSON object per gate: turn, the literal command, what was answered |
| `--quiet` / `--timeout` | Silence that ends a turn; ceiling on one turn |
| `--type-cps` / `--read-pause` | The human pacing — typing speed, and a beat to read before answering |

An **operator-or-above** key is required. A `readonly` key never reaches a gate, so the driver
correctly reports that none appeared — a run that proves nothing and exits 0.

## `record_all_kq.sh` / `render_all_kq.sh`

```bash
bash scripts/demo/record_all_kq.sh     # eight casts + gate logs; needs the cluster
bash scripts/demo/render_all_kq.sh     # transcripts and GIFs; needs only the casts
```

`record_all_kq.sh` pins the recorded geometry (`COLS`/`ROWS`, default 100×34) so it cannot
disagree with the pty, and freezes each scenario's prompts as `<stem>.prompts.txt` beside the
cast. A recording has to stay checkable against what was sent to *it*; scenario files get edited.

`render_all_kq.sh` uses `cast_to_text.py --emulate`, not the append-only path: `kq` repaints in
place, so stripping escape sequences from the byte stream yields overwritten garbage. Only a
terminal emulator can say what was on screen.

## The README hero image

`.github/assets/kubeintellect-demo.gif` is a real recorded session — scenario 06, diagnose →
propose → gate → approve → check — rendered straight from its cast:

```bash
python scripts/demo/cast_to_gif.py scripts/demo/casts-kq/06-approval-gate.cast \
    .github/assets/kubeintellect-demo.gif \
    --fps 8 --font-size 16 --speed 0.8 --min-frame-ms 300 --max-frame-ms 1500 --tail-hold 2.5
```

Nothing is cut: `--max-frame-ms` shortens the *waiting* (a real session is mostly a spinner) and
`--min-frame-ms` holds the reading. `build_demo_cast.py` — which synthesises a cast by feeding
answer text from a past session through the shipping renderer — is what produced this asset
before a genuine end-to-end recording existed. It is kept for building an illustrative frame, but
**the README asset is now a recording**, so do not regenerate it from that script.

## `cast_to_gif.py`

No external binaries — no asciinema, `agg` or ffmpeg.

| Flag | Why |
|---|---|
| `--start` / `--end` | Show a window of the cast. Everything before `--start` is still fed to the screen, so the window opens on the state the user was looking at rather than a blank terminal |
| `--idle-cells` | How many changed cells still counts as *no change* — separates the spinner from content |
| `--max-frame-ms` | Ceiling on one frame — compresses the model's thinking time |
| `--min-frame-ms` | Floor on one frame — stops a painted answer from flashing past |
| `--speed` | Playback rate; `<1` is slower |
| `--colors` / `--alias` | Palette size and antialiasing — the two size knobs |

Three properties it has to keep, all pinned in `v4/tests/test_demo_gif_renderer.py`:

* **What it reports is what it wrote.** Frames are deduplicated on rendered *bytes*, not on cell
  contents, because different characters can paint identical pixels — a spinner cycling through
  glyphs the font lacks is exactly that. Pillow drops such frames on save and folds their
  durations into the frame before, so deduplicating any other way makes `--min-frame-ms` and
  `--max-frame-ms` apply to frames that never reach the file.
* **An animation is not content.** A frame that changes at most `--idle-cells` cells has not
  changed the screen, and its time is folded into the wait. The measurement that sets the
  default: across all eight casts the only sub-12-cell change is the spinner, at exactly one
  cell. The comparison is against the last frame *kept*, not the last one sampled, which is what
  separates a spinner (one cell oscillating forever) from typing (one more cell each frame).
  Without it the spinner is roughly 60% of every recording — the GIFs came out at 76–178 s.
* **No character renders as the missing-glyph box.** No single monospace font covers a terminal
  UI: JetBrains Mono has no Braille (the spinner) and no `⚙`; DejaVu Sans Mono has the gear but
  not the Braille. Fonts are therefore chosen *per character* from a chain, and coverage is
  tested by comparing the glyph's bitmap against that font's `.notdef` — `getbbox()` is not None
  for `.notdef` either, because the tofu box has ink. The casts are checked against the chain in
  `evaluation/test_a_cast_recorded_at_the_wrong_width_still_looks_like_a_recording.py`, which
  asks the recordings rather than a hand-written list; that is what caught `🟡` on the approval
  banner, the one character in the corpus no monospace font here carries.

## `auto_approve_driver.py` — the SSE-level driver

The first corpus's driver, kept because it exercises a different layer: it speaks the ordinary
HTTP API rather than the client, so a change to the SSE wire format breaks it loudly. It exits
non-zero when a gate is left unanswered, when the stream fails, and when more than one gate
arrives in a single stream — that last one is a protocol violation, and answering one while
quietly discarding the other would produce a recording that looks correct and is not.

Tested in `v4/tests/test_demo_auto_approve_driver.py`, which drives it over a real loopback
socket against a server reproducing the SSE wire format. Pass the key via `$KI_API_KEY`, never on
the command line: an argv key is visible in `ps` and lands in any recording of the shell.
