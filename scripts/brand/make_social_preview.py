#!/usr/bin/env python3
"""Generate the 1280x640 GitHub social preview card.

GitHub renders this image for every share on X, LinkedIn, Slack, Discord and
Hacker News. Without it those shares fall back to a generic grey card.

The palette and the `>ki` mark match v4/docs/assets/brand/ki-c-indigo.svg:
indigo #4f46e5 -> cyan #06b6d4 on #0b1020.

    pip install pillow
    python scripts/brand/make_social_preview.py .github/assets/social-preview.png

Upload it at: Settings -> General -> Social preview -> Edit.
(There is no API for this; it must be set in the web UI, per repo.)
"""
from __future__ import annotations

import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
BG = (11, 16, 32)          # #0b1020
EDGE = (30, 38, 66)        # #1e2642
INDIGO = (79, 70, 229)     # #4f46e5
CYAN = (6, 182, 212)       # #06b6d4
TEXT = (226, 232, 240)
MUTED = (148, 163, 184)

MONO = "/home/mohsen/.local/share/fonts/JetBrainsMonoNerd/JetBrainsMonoNerdFont-{}.ttf"
FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono{}.ttf"


def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    for tmpl, name in ((MONO, weight), (FALLBACK, "" if weight == "Regular" else "-Bold")):
        try:
            return ImageFont.truetype(tmpl.format(name), size)
        except OSError:
            continue
    raise SystemExit("no usable font found")


def gradient(size: tuple[int, int]) -> Image.Image:
    """Diagonal indigo -> cyan gradient, matching the brand mark."""
    w, h = size
    grad = Image.new("RGB", (w, h))
    px = grad.load()
    assert px is not None
    for y in range(h):
        for x in range(w):
            t = (x / max(w - 1, 1) + y / max(h - 1, 1)) / 2
            px[x, y] = (
                round(INDIGO[0] + (CYAN[0] - INDIGO[0]) * t),
                round(INDIGO[1] + (CYAN[1] - INDIGO[1]) * t),
                round(INDIGO[2] + (CYAN[2] - INDIGO[2]) * t),
            )
    return grad


def gradient_text(img: Image.Image, xy: tuple[int, int], text: str,
                  f: ImageFont.FreeTypeFont) -> None:
    """Draw text filled with the brand gradient (PIL has no gradient fill)."""
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=f, fill=255)
    img.paste(gradient(img.size), (0, 0), mask)


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "social-preview.png"
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Hairline frame, echoing the mark's inner stroke.
    d.rectangle([0, 0, W - 1, H - 1], outline=EDGE, width=2)

    # Accent bar down the left edge.
    img.paste(gradient((8, H)), (0, 0))

    pad = 84

    # ── Mark: >ki  (chevron + wordmark + cursor block) ──────────────────────
    f_mark = font("Bold", 66)
    gradient_text(img, (pad, 70), ">ki", f_mark)
    cw = int(d.textlength(">ki", font=f_mark))
    img.paste(gradient((22, 40)), (pad + cw + 14, 92))

    # ── Title ───────────────────────────────────────────────────────────────
    d.text((pad, 196), "KubeIntellect", font=font("Bold", 92), fill=TEXT)

    # ── Positioning line — the sentence every other surface reuses ──────────
    d.text((pad, 312), "Human-governed AI SRE for Kubernetes",
           font=font("Regular", 40), fill=MUTED)

    # ── The command, in the product's own voice ─────────────────────────────
    f_code = font("Regular", 30)
    cmd_y = 396
    d.rounded_rectangle([pad - 20, cmd_y - 22, W - pad + 20, cmd_y + 52],
                        radius=12, fill=(17, 24, 45), outline=EDGE, width=2)
    prompt_w = int(d.textlength("$ ", font=f_code))
    gradient_text(img, (pad, cmd_y), "$ ", f_code)
    d.text((pad + prompt_w, cmd_y),
           'kq -q "why is my api-server pod crashlooping?"', font=f_code, fill=TEXT)

    # ── Proof line — what makes it credible, kept short enough to read small ─
    d.text((pad, 512),
           "kubectl + Prometheus + Loki  ·  every action approval-gated  ·  AGPL-3.0",
           font=font("Regular", 26), fill=MUTED)
    d.text((pad, 556),
           "Peer-reviewed — Journal of Grid Computing, 2026",
           font=font("Regular", 24), fill=(100, 116, 139))

    img.save(out, "PNG", optimize=True)
    print(f"{out}: {W}x{H}")


if __name__ == "__main__":
    main()
