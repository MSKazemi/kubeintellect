"""Render the 1280x720 YouTube thumbnail in the KubeIntellect brand system.

Design rule, inherited from the nova build: a thumbnail is seen at ~320x180 in the feed and
smaller on mobile, so the payoff must be the LARGEST element, not the smallest. The payoff
here is the gate answered in both directions and the honest outcome of each — which is what
separates this from every other "AI for Kubernetes" clip.

Both panels are quotations from the recordings, not slogans:
`transcripts-kq/07-approval-denied.txt:84,93` and `06-approval-gate.txt:127` + the answer
that follows at `:138`.
"""

import pathlib
import sys

from PIL import Image, ImageDraw

V = pathlib.Path(__file__).parent
sys.path.insert(0, str(V))
import render as R  # noqa: E402

W, H = 1280, 720
OK_BG, BAD_BG = (14, 26, 20), (40, 18, 18)

im = Image.new("RGB", (W, H), R.BG)
d = ImageDraw.Draw(im)

R.logo(im, 64, 44, 62)
R.wordmark(im, 142, 66, 26)

fh = R.sans(72, "SemiBold", display=True)
d.text((64, 150), "It asked before it acted.", font=fh, fill=R.MUTED)
d.text((64, 234), "Then it admitted it failed.", font=fh, fill=R.TEXT)

# left: denied — and the cluster proves it, in the same session
R.rrect(d, (64, 360, 618, 596), 14, fill=OK_BG, outline=R.GREEN, width=4)
d.text((100, 392), "HITL> deny", font=R.mono(40, "Bold"), fill=R.GREEN)
d.text((100, 470), "web: still", font=R.mono(34, "Medium"), fill=R.TEXT)
d.text((100, 514), "2 replicas", font=R.mono(34, "Medium"), fill=R.TEXT)

# right: approved, it ran — and it did not fix the fault
R.rrect(d, (662, 360, 1216, 596), 14, fill=BAD_BG, outline=R.CORAL, width=4)
d.text((698, 392), "HITL> approve", font=R.mono(40, "Bold"), fill=R.AMBER)
d.text((698, 470), "restarted, and", font=R.mono(34, "Medium"), fill=R.TEXT)
d.text((698, 514), "still failing", font=R.mono(34, "Medium"), fill=R.CORAL)

d.text((64, 646), "human-governed AI SRE for Kubernetes  ·  self-hosted  ·  AGPL-3.0",
       font=R.mono(24, "Medium"), fill=R.FAINT)
d.rectangle((0, H - 9, W, H), fill=R.ACCENT)

out = V / "out/thumbnail.png"
im.save(out)
im.resize((320, 180), Image.LANCZOS).save(V / "out/thumbnail-feed-preview.png")
print(f"{out}  {im.size}  {out.stat().st_size} bytes")
