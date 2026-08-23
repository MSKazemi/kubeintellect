# `scripts/demo/` — the demo assets, and how to record them

Three tools. Two build the scripted README asset; the third drives a **real** server so a
recording can be made unattended.

| Script | What it does | Needs |
|---|---|---|
| `auto_approve_driver.py` | Drives a live session and answers approval gates like a human | a running server |
| `build_demo_cast.py` | Builds the README cast from the real `kq` renderer | `rich` |
| `cast_to_gif.py` | Renders an asciinema v2 cast to a GIF | `pyte`, `pillow` |

---

## `auto_approve_driver.py` — record the approval flow without a person at the keyboard

KubeIntellect stops before every mutating action and waits for a human to type `approve`. That
is the behaviour worth demonstrating, and it is also what makes an unattended recording
impossible and the gate tedious to debug — you cannot loop on something that waits for a person.

This driver is that person. It opens a session, streams the reply, and when a gate arrives it
pauses, types the approval at a human speed, and sends it on the same session.

```bash
# one prompt
scripts/demo/auto_approve_driver.py --api-key "$KI_API_KEY" \
    --prompt "restart the payments-api deployment"

# a scripted multi-turn demo, recorded
asciinema rec demo.cast -c "scripts/demo/auto_approve_driver.py \
    --scenario scripts/demo/scenario.example.txt --api-key $KI_API_KEY"
```

### It answers the gate, it does not remove it

The server also has an `auto_approve` request flag, and it is **not** what this uses. That flag
*skips* the gate, so a recording made with it would be missing the exact moment worth showing,
and a bug in the gate would be hidden rather than exposed. Everything here goes over the
ordinary HTTP API — the same requests any client makes — so what you record is the real path,
and a real gate defect still reproduces under it.

That is also why it is useful for debugging: it replays the approval path as often as you like,
and it is strict about what it sees rather than forgiving.

### Debugging options

| Flag | Why |
|---|---|
| `--deny-nth N` | Deny the Nth gate instead of approving — proves a denial actually stops the action |
| `--json-log FILE` | One JSON object per gate (`action_id`, `risk_level`, summary, what was sent) |
| `--max-approvals N` | Stop approving after N gates; the run then exits non-zero |
| `--max-chained N` | Bound on consecutive gates in one turn, so a misbehaving server cannot spin it |
| `--think-delay` / `--type-cps` | The human pacing; set both to `0` in tests |

It **exits non-zero** when a gate is left unanswered, when the stream fails, and when more than
one gate arrives in a single stream. That last one is a protocol violation — the server
interrupts and waits, so it cannot have two gates outstanding — and answering one while quietly
discarding the other would produce a recording that looks correct and is not.

### Requirements

* An **operator-or-above** key. A `readonly` key never reaches a gate, so the driver will
  correctly report that none appeared.
* Nothing outside the Python standard library.

Tested in `v4/tests/test_demo_auto_approve_driver.py`, which drives the script over a real
loopback socket against a server that reproduces the SSE wire format.

---

## `build_demo_cast.py` + `cast_to_gif.py` — the README asset

`build_demo_cast.py` renders every panel through the shipping `kq` renderer
(`v4/packages/kube-q/kube_q/cli/renderer.py`), and the answer text is taken verbatim from a real
recorded session — so nothing in the frame is a mock-up. The recording is nonetheless **scripted
and time-compressed**: spinner beats are shortened so the story fits in ~19 seconds, and **no
latency or cost claim is made from this asset.**

```bash
python scripts/demo/build_demo_cast.py out.cast
python scripts/demo/cast_to_gif.py out.cast .github/assets/kubeintellect-demo.gif
```

`cast_to_gif.py` needs no external binaries — no asciinema, `agg` or ffmpeg.
