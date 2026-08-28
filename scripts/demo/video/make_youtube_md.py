"""Generate `youtube.md` — the title, description and chapters, from scenes.py.

Chapters are act boundaries, timed from `durations.json`, so they cannot drift from the
video. The file is a hand-over artifact: nothing here uploads anything.
"""

from __future__ import annotations

import json
import pathlib
import sys

V = pathlib.Path(__file__).parent
sys.path.insert(0, str(V))
import scenes as S  # noqa: E402

PAD = 0.45 + 0.85
d = json.loads((V / "durations.json").read_text(encoding="utf-8"))
enabled = [sc for sc in S.SCENES if sc.get("enabled", True)]

chapters, t, act = [], 0.0, None
for sc in enabled:
    if sc.get("act") != act:
        act = sc.get("act")
        chapters.append((t, act.title() if act else "Opening"))
    t += d[sc["id"]] + PAD


def ts(x: float) -> str:
    return f"{int(x // 60)}:{int(x % 60):02d}"


body = f"""# YouTube hand-over — not uploaded

Uploading is outward-facing and needs an explicit go-ahead. Everything below is ready to
paste; nothing here contacts YouTube.

**File:** `out/kubeintellect-demo.mp4` · {ts(t)} · 1920×1080 · H.264 / AAC 48 kHz · `+faststart`
**Subtitles:** `out/kubeintellect-demo.srt`
**Thumbnail:** `out/thumbnail.png` (1280×720)

## Title

    KubeIntellect — an AI SRE for Kubernetes that asks before it acts

## Description

    KubeIntellect diagnoses a live Kubernetes cluster in plain English, quotes the evidence
    it read, and stops at a human approval gate before it changes anything.

    Every terminal scene in this video is a verbatim recording against a live cluster —
    nothing is typed by hand or reconstructed. That includes the parts that do not flatter
    it: an approved restart that did NOT fix the fault, and a root cause it inferred rather
    than read.

    Website:  https://{S.WEBSITE}
    Source:   https://{S.REPO}
    Licence:  AGPL-3.0, self-hosted

    The recordings, and the page listing everything they did not do, are in the repository
    under scripts/demo/.

## Chapters

"""
body += "\n".join(f"    {ts(x)}  {name}" for x, name in chapters) + "\n"

(V / "youtube.md").write_text(body, encoding="utf-8")
print(f"youtube.md written — {len(chapters)} chapters, {ts(t)}")
