"""Render the architecture animation from `spec.py`. No narration — it is a diagram that moves.

Layout is a stack, because the system is one: a question enters at the top, and everything
below it is what happens before an answer comes back. Seven layers, twenty-six components, each
box labelled with the module it stands for so the drawing can be checked by reading the code.

Four phases, one story each: a human asks · it wants to change something · nobody asked ·
afterwards. In a phase, the components that participate light up and the rest dim, and a pulse
travels each edge in the direction the data goes.

**Components that are off by default are drawn off** — dashed border, dimmed, and an explicit
`OFF by default` chip carrying the flag name. Four of the twenty-six are in that state on a
stock install. A diagram that drew them like the rest would be claiming a system nobody runs.

Palette, fonts and the mark come from `../video/render.py`, which sources every value from the
site's own tokens. Nothing is invented here.

    python3 animate.py            # -> out/architecture.mp4 + out/architecture.png
"""
from __future__ import annotations

import math
import pathlib
import subprocess
import sys

from PIL import Image, ImageDraw

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "video"))

import render as R  # noqa: E402
import spec as SP  # noqa: E402

OUT = HERE / "out"
FPS = 30
INTRO = 4.0          # the stack assembles
PHASE = 7.5          # each of the four stories
OUTRO = 3.5
TOTAL = INTRO + PHASE * len(SP.PHASES) + OUTRO

# ------------------------------------------------------------------ layout
# A gutter has to be tall enough to hold an edge label, or the label lands on a box and covers
# the module path it is meant to be read beside. CORRIDOR is a routing lane on the right: an
# edge that skips a band travels down it instead of straight through whatever is in the way.
PAD_X, TOP, BOT = 60, 132, 54
LAYER_LABEL_W = 186
GAP_Y = 34
CORRIDOR = 96


def layout() -> dict:
    """Boxes for every node, plus each layer's band. Computed once; the frames read it."""
    n = len(SP.LAYERS)
    band_h = (R.H - TOP - BOT - GAP_Y * (n - 1)) / n
    boxes, bands = {}, {}
    for i, lay in enumerate(SP.LAYERS):
        y0 = TOP + i * (band_h + GAP_Y)
        bands[lay.key] = (PAD_X, int(y0), R.W - PAD_X, int(y0 + band_h))
        bx0 = PAD_X + LAYER_LABEL_W
        avail = (R.W - PAD_X - CORRIDOR) - bx0
        k = len(lay.nodes)
        bw = (avail - 14 * (k - 1)) / k
        for j, nd in enumerate(lay.nodes):
            x = bx0 + j * (bw + 14)
            boxes[nd.key] = (int(x), int(y0), int(x + bw), int(y0 + band_h))
    return {"boxes": boxes, "bands": bands, "band_h": band_h,
            "index": {nd.key: i for i, lay in enumerate(SP.LAYERS) for nd in lay.nodes}}


L = layout()
CORRIDOR_X = R.W - PAD_X - CORRIDOR // 2


def centre(key: str) -> tuple[int, int]:
    x0, y0, x1, y1 = L["boxes"][key]
    return ((x0 + x1) // 2, (y0 + y1) // 2)


def _gutter_below(i: int) -> int:
    """Mid-line of the gap under band `i` — where a horizontal run and its label are safe."""
    return int(TOP + (i + 1) * (L["band_h"] + GAP_Y) - GAP_Y / 2)


def route(a: str, b: str) -> list[tuple[float, float]]:
    """An orthogonal path from a to b that never crosses a box it is not connecting.

    Three cases, in the order they matter: same band (up into the gutter, across, back down),
    adjacent bands (straight down the gap), and a jump (out to the right-hand corridor, down it,
    back in). Before this, edges were straight lines: `gate -> kubectl` drew itself through
    `L1 episodes` and `Flight recorder`, and every label landed on top of a module path.
    """
    ia, ib = L["index"][a], L["index"][b]
    ax0, ay0, ax1, ay1 = L["boxes"][a]
    bx0, by0, bx1, by1 = L["boxes"][b]
    acx, acy = centre(a)
    bcx, bcy = centre(b)

    if ia == ib:
        g = _gutter_below(ia) if ia == 0 else _gutter_below(ia - 1)
        y_out, y_in = (ay1, by1) if ia == 0 else (ay0, by0)
        return [(acx, y_out), (acx, g), (bcx, g), (bcx, y_in)]

    if abs(ia - ib) == 1:
        g = _gutter_below(min(ia, ib))
        if ib > ia:
            return [(acx, ay1), (acx, g), (bcx, g), (bcx, by0)]
        return [(acx, ay0), (acx, g), (bcx, g), (bcx, by1)]

    # Down the corridor, then back in along the gutter that touches the destination band -- not
    # along its mid-height, which would cross every box between the corridor and the target.
    down = ib > ia
    y_turn = _gutter_below(ia) if down else _gutter_below(ia - 1)
    y_land = _gutter_below(ib - 1) if down else _gutter_below(ib)
    return [(acx, ay1 if down else ay0), (acx, y_turn),
            (CORRIDOR_X, y_turn), (CORRIDOR_X, y_land),
            (bcx, y_land), (bcx, by0 if down else by1)]


# ------------------------------------------------------------------ pieces
def dashed(d: ImageDraw.ImageDraw, box, r: int, colour, dash: int = 9) -> None:
    """A dashed rounded box. `off by default` has to look different, not just read different."""
    x0, y0, x1, y1 = box
    for x in range(x0 + r, x1 - r, dash * 2):
        d.line([(x, y0), (min(x + dash, x1 - r), y0)], fill=colour, width=2)
        d.line([(x, y1), (min(x + dash, x1 - r), y1)], fill=colour, width=2)
    for y in range(y0 + r, y1 - r, dash * 2):
        d.line([(x0, y), (x0, min(y + dash, y1 - r))], fill=colour, width=2)
        d.line([(x1, y), (x1, min(y + dash, y1 - r))], fill=colour, width=2)


def _mix(c, other, a: float):
    return tuple(int(c[i] + (other[i] - c[i]) * a) for i in range(3))


def node_box(d: ImageDraw.ImageDraw, nd, box, lit: float, appear: float) -> None:
    """`lit` 0..1 = participating in this phase. `appear` 0..1 = fading in during the intro."""
    x0, y0, x1, y1 = box
    off = nd.on is False
    dim = _mix(R.BG, R.BG_RAISED, appear)
    edge = R.ACCENT if lit > 0.5 else (R.BORDER_HARD if not off else R.BORDER_HARD)
    edge = _mix(R.BG, edge, appear)

    R.rrect(d, box, 10, fill=dim, outline=None)
    if off:
        dashed(d, box, 10, _mix(R.BG, R.AMBER if lit > 0.5 else R.BORDER_HARD, appear * 0.85))
    else:
        R.rrect(d, box, 10, fill=None, outline=edge, width=2 if lit > 0.5 else 1)

    base = R.TEXT if not off else R.MUTED
    fg = _mix(R.BG, _mix(base, R.TEXT, lit * 0.5), appear)
    sub = _mix(R.BG, R.MUTED if not off else R.BORDER_HARD, appear)

    d.text((x0 + 14, y0 + 10), nd.label, font=R.sans(21, "SemiBold"), fill=fg)
    d.text((x0 + 14, y0 + 38), nd.module.replace("v4/packages/kubeintellect-server/", ""),
           font=R.mono(12), fill=sub)

    chip = None
    if off:
        chip = (f"OFF by default · {nd.flag}", R.AMBER)
    elif isinstance(nd.on, str):
        chip = (f"{nd.flag} = {nd.on}", R.TEAL)
    if chip:
        txt, col = chip
        f = R.mono(12, "Bold")
        w = d.textlength(txt, font=f)
        cy = y1 - 26
        R.rrect(d, (x0 + 14, cy, x0 + 26 + w, cy + 19), 5,
                fill=_mix(R.BG, R.BG_SUNKEN, appear), outline=_mix(R.BG, col, appear * 0.7))
        d.text((x0 + 20, cy + 3), txt, font=f, fill=_mix(R.BG, col, appear))
    elif nd.note:
        d.text((x0 + 14, y1 - 26), nd.note[:64], font=R.sans(14), fill=sub)


def _walk(pts: list[tuple[float, float]], t: float) -> tuple[float, float, float]:
    """Point at fraction `t` along the polyline, plus the heading there."""
    segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    lens = [math.dist(a, b) for a, b in segs]
    total = sum(lens) or 1.0
    want = t * total
    for (a, b), ln in zip(segs, lens, strict=True):
        if want <= ln or ln == 0:
            f = (want / ln) if ln else 0.0
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f,
                    math.atan2(b[1] - a[1], b[0] - a[0]))
        want -= ln
    a, b = segs[-1]
    return (b[0], b[1], math.atan2(b[1] - a[1], b[0] - a[0]))


def _label_at(pts: list[tuple[float, float]]) -> tuple[float, float]:
    """Middle of the longest horizontal run — which routing guarantees is inside a gutter."""
    best, bx, by = -1.0, pts[0][0], pts[0][1]
    for i in range(len(pts) - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        if abs(y1 - y0) < 2 and abs(x1 - x0) > best:
            best, bx, by = abs(x1 - x0), (x0 + x1) / 2, y0
    return bx, by


def edge(d: ImageDraw.ImageDraw, a: str, b: str, label: str, t: float) -> None:
    """One flow, with a pulse at position `t` (0..1) travelling a -> b along its route."""
    pts = route(a, b)
    d.line([(int(x), int(y)) for x, y in pts], fill=R.BORDER_HARD, width=2, joint="curve")

    px, py, ang = _walk(pts, t)
    for rad, al in ((13, 0.20), (8, 0.45), (4, 1.0)):
        d.ellipse([px - rad, py - rad, px + rad, py + rad], fill=_mix(R.BG, R.ACCENT, al))

    ex, ey = pts[-1]
    _, _, eang = _walk(pts, 1.0)
    hx, hy = ex - 9 * math.cos(eang), ey - 9 * math.sin(eang)
    d.polygon([(ex, ey),
               (hx - 6 * math.sin(eang), hy + 6 * math.cos(eang)),
               (hx + 6 * math.sin(eang), hy - 6 * math.cos(eang))], fill=R.ACCENT)

    if label:
        f = R.sans(14, "SemiBold")
        mx, my = _label_at(pts)
        w = d.textlength(label, font=f)
        R.rrect(d, (mx - w / 2 - 7, my - 11, mx + w / 2 + 7, my + 11), 6,
                fill=R.BG_SUNKEN, outline=R.BORDER)
        d.text((mx - w / 2, my - 8), label, font=f, fill=R.TEAL)


def frame(t: float) -> Image.Image:
    im = R.canvas()
    d = ImageDraw.Draw(im)

    # which phase, and how far into it
    if t < INTRO:
        phase, pt = 0, t / INTRO
    elif t < INTRO + PHASE * len(SP.PHASES):
        k = (t - INTRO) / PHASE
        phase, pt = int(k) + 1, k - int(k)
    else:
        phase, pt = 0, 1.0

    flows = [f for f in SP.FLOWS if f[3] == phase]
    active = {k for a, b, _l, _p in flows for k in (a, b)}

    # header
    R.logo(im, PAD_X, 40, 58)
    d.text((PAD_X + 76, 44), "KubeIntellect", font=R.sans(30, "Bold", display=True), fill=R.TEXT)
    d.text((PAD_X + 76, 80), "architecture and data flow", font=R.sans(17), fill=R.MUTED)

    if phase:
        num, title, blurb = SP.PHASES[phase - 1]
        f = R.sans(27, "Bold", display=True)
        w = d.textlength(title, font=f)
        d.text((R.W - PAD_X - w, 44), title, font=f, fill=R.ACCENT)
        f2 = R.sans(16)
        d.text((R.W - PAD_X - d.textlength(blurb, font=f2), 82), blurb, font=f2, fill=R.MUTED)
        cnt = f"{num} / {len(SP.PHASES)}"
        f3 = R.mono(13)
        d.text((R.W - PAD_X - d.textlength(cnt, font=f3), 106), cnt, font=f3, fill=R.BORDER_HARD)

    # layers
    for i, lay in enumerate(SP.LAYERS):
        bx0, by0, _bx1, by1 = L["bands"][lay.key]
        # during the intro the stack assembles top-down, one band at a time
        app = 1.0 if phase else max(0.0, min(1.0, (pt * len(SP.LAYERS) - i) * 1.6))
        if app <= 0:
            continue
        d.text((bx0, by0 + 8), lay.title, font=R.sans(18, "Bold"),
               fill=_mix(R.BG, R.TEXT, app))
        for line_i, line in enumerate(R.wrap(lay.blurb, R.sans(13), LAYER_LABEL_W - 24)[:3]):
            d.text((bx0, by0 + 34 + line_i * 17), line, font=R.sans(13),
                   fill=_mix(R.BG, R.BORDER_HARD, app))
        for nd in lay.nodes:
            lit = 1.0 if nd.key in active else 0.0
            node_box(d, nd, L["boxes"][nd.key], lit, app)

    # flows for this phase — each pulse staggered so the eye can follow the order
    for i, (a, b, label, _p) in enumerate(flows):
        span = 1.0 / max(1, len(flows))
        local = (pt - i * span * 0.55) / max(span, 0.28)
        if 0.0 <= local <= 1.0:
            edge(d, a, b, label, local)

    # the standing caption: the honest half
    off = SP.default_off()
    d.text((PAD_X, R.H - 44),
           f"{len(off)} of {len(SP.nodes())} components are OFF by default and drawn as off — "
           f"{', '.join(n.flag for n in off)}",
           font=R.mono(14), fill=R.AMBER)
    return im


def main() -> None:
    OUT.mkdir(exist_ok=True)
    n = int(TOTAL * FPS)
    png = OUT / "architecture.png"
    frame(INTRO + PHASE * 1.5).save(png)          # a poster from the middle of phase 2
    print(f"poster: {png}")

    mp4 = OUT / "architecture.mp4"
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{R.W}x{R.H}", "-r", str(FPS), "-i", "-",
         "-vf", f"fade=t=in:st=0:d=0.8,fade=t=out:st={TOTAL - 1.0:.2f}:d=1.0",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)],
        stdin=subprocess.PIPE)
    for i in range(n):
        ff.stdin.write(frame(i / FPS).tobytes())
        if i % 60 == 0:
            print(f"\r  {i}/{n} frames", end="", flush=True)
    ff.stdin.close()
    rc = ff.wait()
    print(f"\nffmpeg exit {rc}; {n} frames, {TOTAL:.1f}s -> {mp4}")
    if rc:
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
