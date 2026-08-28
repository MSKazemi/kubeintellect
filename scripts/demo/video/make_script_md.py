"""Render scenes.py as a readable narration script.

Deliberately runs **before** any audio exists. `durations.json` is written by `tts.py`
after synthesis; until then the timings are estimated from word count at Piper's measured
rate, so the script can be read, reviewed and revised at zero cost — which is the whole
point of writing it first.

    python3 make_script_md.py            # estimated timings (no audio needed)
    python3 make_script_md.py --strict   # fail unless durations.json exists
"""

from __future__ import annotations

import json
import pathlib
import sys

V = pathlib.Path(__file__).parent
sys.path.insert(0, str(V))
import scenes as S  # noqa: E402

WPS = 3.1          # Piper en_US-ryan-high, measured on this narration: 821 words / 263.6 s
PAD = 0.45 + 0.85  # lead-in + tail, same as the nova builder

durations_path = V / "durations.json"
have_real = durations_path.exists()
if "--strict" in sys.argv and not have_real:
    sys.exit("durations.json missing — run tts.py first, or drop --strict for estimates")
real = json.loads(durations_path.read_text(encoding="utf-8")) if have_real else {}


def seconds(sc: dict) -> float:
    if sc["id"] in real:
        return real[sc["id"]] + PAD
    return len(sc["narration"].split()) / WPS + PAD


enabled = [sc for sc in S.SCENES if sc.get("enabled", True)]
blocked = [sc for sc in S.SCENES if not sc.get("enabled", True)]

# A terminal scene is revealed over its narration — the transcript does not set the pace,
# the voice does. Above ~1.5 lines/second the text is on screen but cannot be read, which
# looks like a demo and communicates nothing. The nova build sits at 0.3–1.7 (median ~0.9),
# so that is the bar. Reported here rather than asserted, so a scene can be over it on
# purpose and the number stays visible.
TRANSCRIPTS = V / ".." / "transcripts-kq"
rates = []
for sc in enabled:
    if sc["kind"] != "terminal":
        continue
    lo, hi = sc.get("lines", (1, 0))
    rates.append((sc["id"], (hi - lo + 1) / seconds(sc)))

out = [
    "# KubeIntellect — narrated demo script",
    "",
    f"_Generated from `scenes.py` by `make_script_md.py`. Timings are "
    f"{'measured from the synthesised narration' if have_real else '**estimated** from word count at '
     f'{WPS} words/second — no audio has been synthesised yet'}._",
    "",
    "Voice: Piper `en_US-ryan-high` (MIT, offline, no paid API and no network at synthesis).",
    "Every terminal scene replays a verbatim transcript from `../transcripts-kq/`, recorded "
    "against a live cluster and re-verified 2026-08-28 (`../DEMOS.md` § *Verification*). "
    "Nothing on screen is typed by hand.",
    "",
]

t, act = 0.0, None
for sc in enabled:
    d = seconds(sc)
    if sc.get("act") != act:
        act = sc.get("act")
        if act:
            out += ["", f"## {act}", ""]
    out.append(f"### `{int(t // 60)}:{int(t % 60):02d}` — {sc['id']}  ({sc['kind']}, {d:.1f}s)")
    out.append("")
    if sc.get("caption"):
        out += [f"**On screen:** {sc['caption']}", ""]
    if sc["kind"] == "terminal":
        lo, hi = sc.get("lines", (1, 0))
        out += [f"**Source:** live transcript `../transcripts-kq/{sc['source']}`, lines {lo}–{hi}", ""]
        if sc.get("evidence"):
            out += [f"**Claims rest on:** `{sc['evidence']}`", ""]
    elif sc["kind"] == "shot":
        out += [f"**Source:** `{sc['source']}`", ""]
    elif sc.get("sources"):
        out += ["**Checked against:** " + " · ".join(f"`{x}`" for x in sc["sources"]), ""]
    out.append("> " + " ".join(sc["narration"].split()))
    out.append("")
    t += d

out += ["---", "", f"**Total (enabled scenes):** {int(t // 60)}m{int(t % 60):02d}s", ""]

if rates:
    out += ["", "### Reveal rate", "",
            "A terminal scene is revealed over its narration, so the transcript window has to fit "
            "the voice. Above roughly **1.5 lines/second** the text is on screen but unreadable. "
            "(The nova build runs 0.3–1.7, median ~0.9.)", "",
            "| Scene | lines/second |", "|---|---|"]
    for sid, r in rates:
        out.append(f"| `{sid}` | {r:.1f}{'  ⚠️ too fast to read' if r > 1.5 else ''} |")
    out.append("")

if blocked:
    out += ["", "## Not in the cut yet", "",
            "Each of these has a slot, a caption and narration written; what is missing is the "
            "footage. They are listed here rather than deleted so the gap stays visible.", ""]
    for sc in blocked:
        out += [f"- **`{sc['id']}`** ({sc['kind']}) — {sc['caption']}",
                f"  - ⛔ {sc['blocked_on']}",
                f"  - narration: _{' '.join(sc['narration'].split())}_"]
    out.append("")

(V / "script.md").write_text("\n".join(out), encoding="utf-8")
print(f"script.md written — {len(enabled)} scenes, {int(t // 60)}m{int(t % 60):02d}s"
      f" ({'measured' if have_real else 'estimated'}), {len(blocked)} blocked")
