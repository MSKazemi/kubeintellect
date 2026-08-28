"""Render every enabled scene of the KubeIntellect narrated demo to PNG frames.

1920x1080 @ 30fps. Adapted from `nova/experiments/azure-2026-08-27/video/render.py`, which
is the generic half of that pipeline; what changes here is the palette, the mark, and how a
terminal scene is loaded — KubeIntellect replays a **window** of a real `kq` transcript
rather than a whole captured file, because the window has to fit the narration (see the
reveal-rate note in README.md).

Typography: JetBrains Mono for identity, terminal and data; Inter for prose.
"""
from __future__ import annotations

import json, math, pathlib, re, shutil, sys
from PIL import Image, ImageDraw, ImageFilter, ImageFont

V = pathlib.Path(__file__).parent
sys.path.insert(0, str(V))
import scenes as S  # noqa: E402

W, H, FPS = 1920, 1080, 30

# ---------------------------------------------------------------- palette
# Every colour below comes from kubeintellect.com or from the shipped brand mark, and names
# the file it comes from. It used to come from neither: the accent was `#7c8cf8`, commented
# "KubeIntellect indigo", which is in no stylesheet and no mark — it was inherited from the
# upstream build this renderer was adapted from, along with the variable name `ACCENT`.
#
# The site is a light page, so its *dark* mapping is taken from the dark panels it already
# renders: `--ink-panel` behind them, `--teal-soft` for the mono text on top (HowItWorks.tsx,
# HowItConnects.tsx), `--amber-soft` for the human-in-the-loop gate, and the mark's own green
# for a healthy outcome. The mark's gradient is not used as a UI accent — it belongs to the
# logo, and reusing it for chrome is what makes a mark stop reading as one.

def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


BG          = _rgb("#0b1220")      # website globals.css --ink-panel
BG_RAISED   = (17, 25, 45)         # derived: --ink-panel lifted, for raised surfaces
BG_SUNKEN   = _rgb("#0b1020")      # website public/logo.svg — the mark's own tile
BORDER      = _rgb("#1e2642")      # website public/logo.svg — the mark's tile stroke
BORDER_HARD = (42, 52, 82)         # derived: the same stroke, one step brighter
TEXT        = _rgb("#f6f8fc")      # website globals.css --cloud
MUTED       = _rgb("#cbd5e1")      # the slate-300 the site uses inside dark panels
FAINT       = (127, 140, 163)      # derived: slate-300 at ~60% against --ink-panel
ACCENT      = _rgb(S.TEAL)         # --teal-soft: the site's own accent on a dark panel
CORAL       = _rgb(S.CORAL)        # unsourced — the site has no error red
AMBER       = _rgb(S.AMBER)        # --amber-soft, which the site labels the approval gate
TEAL        = ACCENT             # one accent; the old palette had two near-identical
ON_ACCENT   = _rgb("#0b1220")
GREEN       = _rgb(S.ACCENT)       # the mark's green = a healthy outcome, not the chrome
MARK_A      = _rgb("#00ff88")      # logo.svg gradient stop 0%
MARK_B      = _rgb("#00c758")      # logo.svg gradient stop 100%

JB = "/home/mohsen/.local/share/fonts/JetBrainsMonoNerd/JetBrainsMonoNerdFontMono-{}.ttf"
IN = "/usr/share/fonts/opentype/inter/Inter{}-{}.otf"

_cache: dict = {}


def mono(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    k = ("m", size, weight)
    if k not in _cache:
        _cache[k] = ImageFont.truetype(JB.format(weight), size)
    return _cache[k]


def sans(size: int, weight: str = "Regular", display: bool = False) -> ImageFont.FreeTypeFont:
    k = ("s", size, weight, display)
    if k not in _cache:
        _cache[k] = ImageFont.truetype(IN.format("Display" if display else "", weight), size)
    return _cache[k]


# ------------------------------------------------------------- primitives
def canvas() -> Image.Image:
    return Image.new("RGB", (W, H), BG)


def rrect(d: ImageDraw.ImageDraw, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=width)


def logo(im: Image.Image, x: int, y: int, size: int) -> None:
    """The KubeIntellect mark, drawn from `website/public/logo.svg` geometry.

    Until 2026-08-28 this drew a hexagon with a `K` in it — a mark that appears on no surface
    the project ships. The real one is a chevron, the lowercase `ki`, and a cursor block, in a
    green gradient on a `#0b1020` tile, and it is what the site's navbar and footer carry on
    every page. Coordinates below are the SVG's own 256-unit viewBox, scaled.
    """
    d = ImageDraw.Draw(im)
    rrect(d, (x, y, x + size, y + size), int(size * 0.227), fill=BG_SUNKEN,
          outline=BORDER, width=max(1, size // 128))
    s = size / 256.0

    # The SVG fills chevron and text with one diagonal gradient, so the mark is drawn once as
    # a mask and the gradient composited through it — rather than picking a flat mid-colour,
    # which is what makes a copied logo look like a copy.
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.line([(53 * s, 96 * s), (86 * s, 128 * s), (53 * s, 160 * s)],
            fill=255, width=max(2, int(14 * s)), joint="curve")
    r = max(1, int(7 * s))
    for cx, cy in ((53 * s, 96 * s), (86 * s, 128 * s), (53 * s, 160 * s)):
        md.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    # The SVG sets `ki` at 112 and the cursor at a hard-coded x=214, which assumes the advance
    # of its own font stack. JetBrains Mono Nerd is wider, so those two numbers cannot both be
    # honoured: taken literally the cursor lands on top of the `i`. The type size gives way and
    # the layout is kept, because the layout is what the mark is recognised by.
    size_em = max(8, int(112 * s))
    f = mono(size_em, "Bold")
    while size_em > 8 and 104 * s + md.textlength("ki", font=f) + 24 * s > 236 * s:
        size_em -= 2
        f = mono(size_em, "Bold")
    md.text((104 * s, 170 * s), "ki", font=f, fill=255, anchor="ls")
    cur_x = 104 * s + md.textlength("ki", font=f) + 6 * s
    md.rounded_rectangle((cur_x, 141 * s, cur_x + 18 * s, 171 * s),
                         radius=max(1, int(2 * s)), fill=255)

    grad = Image.new("RGB", (size, size))
    gd = ImageDraw.Draw(grad)
    for i in range(2 * size):                   # the SVG gradient runs 30,30 -> 226,226
        # Anti-diagonals cover the whole square only if i runs to 2*size; stopping at size
        # leaves the lower-right half of the mark unpainted, which reads as a black `ki`.
        a = min(1.0, max(0.0, (i / 2 - 30 * s) / (196 * s)))
        gd.line([(i, 0), (0, i)], fill=tuple(
            int(MARK_A[j] + (MARK_B[j] - MARK_A[j]) * a) for j in range(3)))
    im.paste(grad, (x, y), mask)


def wordmark(im: Image.Image, x: int, y: int, size: int = 26) -> None:
    d = ImageDraw.Draw(im)
    f = mono(size, "Bold")
    d.text((x, y), "Kube", font=f, fill=TEXT)
    d.text((x + d.textlength("Kube", font=f), y), "Intellect", font=f, fill=ACCENT)


def chrome(im: Image.Image, progress: float, act: str, caption: str) -> None:
    """Persistent furniture: watermark, act label, caption bar, progress line."""
    d = ImageDraw.Draw(im)
    if act:
        f = mono(19, "Medium")
        d.text((96, 66), " ".join(act.upper()), font=f, fill=FAINT)
    wordmark(im, W - 96 - 200, 60, 24)
    if caption:
        d.rectangle((0, H - 108, W, H), fill=BG_SUNKEN)
        d.line((0, H - 108, W, H - 108), fill=BORDER, width=2)
        d.rectangle((96, H - 84, 100, H - 36), fill=ACCENT)
        d.text((124, H - 82), caption, font=sans(27, "Medium"), fill=TEXT)
    d.rectangle((0, H - 5, int(W * progress), H), fill=ACCENT)


# ------------------------------------------------------------------ cards
def wrap(text: str, font, maxw: int) -> list[str]:
    out: list[str] = []
    m = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            t = (line + " " + word).strip()
            if m.textlength(t, font=font) <= maxw:
                line = t
            else:
                out.append(line); line = word
        out.append(line)
    return out


def render_card(sc: dict, t: float, dur: float, progress: float) -> Image.Image:
    im = canvas()
    d = ImageDraw.Draw(im)
    ease = min(1.0, t / 0.55)                      # fade-in
    x = 160
    y = 268 if sc.get("bullets") else 430

    if sc.get("logo"):
        # An act label sits at y=66 and runs past x=160, so a card carrying both put the mark
        # on top of the words. The mark gives way, not the label.
        logo(im, x, y - 190 + (34 if sc.get("act") else 0), 116)

    if sc["title"] == "KubeIntellect":              # the wordmark, set as the wordmark
        tf = mono(92, "Bold")
        d.text((x, y), "Kube", font=tf, fill=TEXT)
        d.text((x + d.textlength("Kube", font=tf), y), "Intellect", font=tf, fill=ACCENT)
        y += int(tf.size * 1.24)
    else:
        tf = sans(84 if len(sc["title"]) < 34 else 66, "SemiBold", display=True)
        for line in wrap(sc["title"], tf, W - 2 * x - 320):
            d.text((x, y), line, font=tf, fill=TEXT)
            y += int(tf.size * 1.22)

    if sc.get("subtitle"):
        y += 18
        d.text((x, y), sc["subtitle"], font=sans(38, "Regular"), fill=MUTED)
        y += 74

    if sc.get("links"):
        y += 26
        d.line((x, y, x + 150, y), fill=BORDER_HARD, width=2)
        y += 36
        lf = mono(32, "Medium")
        for i, url in enumerate(sc["links"]):
            a = max(0.0, min(1.0, (t - (1.15 + i * 0.4)) / 0.5))
            if a <= 0:
                continue
            bar = tuple(int(BG[j] + (ACCENT[j] - BG[j]) * a) for j in range(3))
            col = tuple(int(BG[j] + (TEXT[j] - BG[j]) * a) for j in range(3))
            d.rectangle((x, y + 7, x + 5, y + 33), fill=bar)
            d.text((x + 28, y), url, font=lf, fill=col)
            y += 54

    if sc.get("bullets"):
        y += 54
        d.line((x, y - 28, x + 150, y - 28), fill=BORDER_HARD, width=2)
        n = len(sc["bullets"])
        for i, (head, sub) in enumerate(sc["bullets"]):
            # stagger each bullet in over the first 60% of the scene
            start = 0.7 + i * (dur * 0.45 / max(1, n))
            a = max(0.0, min(1.0, (t - start) / 0.5))
            if a <= 0:
                continue
            col = tuple(int(BG[j] + (ACCENT[j] - BG[j]) * a) for j in range(3))
            d.rectangle((x, y + 12, x + 5, y + 40), fill=col)
            hc = tuple(int(BG[j] + (TEXT[j] - BG[j]) * a) for j in range(3))
            hf = mono(38, "Medium") if head.isupper() or "/" in head else sans(38, "Medium")
            d.text((x + 30, y), head, font=hf, fill=hc)
            if sub:
                sc_ = tuple(int(BG[j] + (FAINT[j] - BG[j]) * a) for j in range(3))
                d.text((x + 30, y + 50), sub, font=sans(27, "Regular"), fill=sc_)
                y += 108
            else:
                y += 74

    if ease < 1.0:
        im = Image.blend(canvas(), im, ease)
    chrome(im, progress, sc.get("act", ""), sc.get("caption", ""))
    return im


# --------------------------------------------------------------- terminal
# Two glyphs in the casts are not in JetBrains Mono Nerd Font and render as a slashed box:
# the tool-call gear (U+2699) and the gate's yellow circle (U+1F7E1). Both were checked by
# rendering them and comparing the bitmap against .notdef — they are tofu, and a tofu on
# every kubectl line looks like a broken recording. They are substituted with glyphs the
# font really has. This is the ONLY place the screen differs from the transcript byte for
# byte, and it is recorded in README.md.
GLYPHS = {"\u2699": "\u25b8", "\U0001f7e1": "\u25cf"}


def colourise(line: str):
    t = line.strip()
    if t.startswith("You:"):
        return "cmd"
    if t.startswith("HITL>"):
        return "gate"
    if t.startswith("\u25b8") or t.startswith("\u2699"):        # a real tool call
        return "tool"
    if "Approval Required" in t or "needs your approval" in t:
        return "gate"
    if t.startswith("kube-q ") or "tok  \u00b7" in t or "cost unknown" in t:
        return "muted"
    if t and all(c in "\u2500\u2502\u256d\u256e\u2570\u256f \u2014" for c in t):
        return "muted"
    if any(w in line for w in ("FATAL", "OOMKilled", "Error", "error", "failing", "failed",
                               "did not resolve", "Exit Code: 1", "crash-looping")):
        return "bad"
    if any(w in line for w in ("successfully", "cancelled", "\u2713")):
        return "ok"
    return "out"


COLS = {"cmd": TEXT, "tool": TEAL, "gate": AMBER, "ok": GREEN,
        "bad": CORAL, "muted": FAINT, "out": MUTED}


def load_transcript(name: str, window: tuple[int, int] | None = None) -> list[str]:
    """One transcript, sliced to the 1-indexed inclusive window the scene declares.

    The window is not decoration: a scene lasts exactly as long as its narration, and the
    lines are revealed over that time, so the window is what makes the text readable rather
    than a blur. `make_script_md.py` reports the resulting lines/second per scene.
    """
    raw = (V / ".." / "transcripts-kq" / name).read_text(encoding="utf-8").split("\n")
    if window:
        lo, hi = window
        raw = raw[lo - 1:hi]
    out = []
    for ln in raw:
        for a, b in GLYPHS.items():
            ln = ln.replace(a, b)
        ln = ln.rstrip()
        while len(ln) > 104:
            cut = ln.rfind(" ", 60, 104)
            cut = cut if cut > 60 else 104
            out.append(ln[:cut]); ln = "  " + ln[cut:].lstrip()
        out.append(ln)
    return out


TERM_ROWS = 23


def render_terminal(sc: dict, t: float, dur: float, progress: float, lines: list[str]) -> Image.Image:
    im = canvas()
    d = ImageDraw.Draw(im)

    bx0, by0, bx1, by1 = 96, 150, W - 96, H - 140
    rrect(d, (bx0, by0, bx1, by1), 16, fill=BG_SUNKEN, outline=BORDER_HARD, width=2)
    d.line((bx0 + 2, by0 + 52, bx1 - 2, by0 + 52), fill=BORDER, width=2)
    for i, c in enumerate(((232, 125, 125), (232, 184, 102), ACCENT)):
        d.ellipse((bx0 + 26 + i * 26, by0 + 20, bx0 + 38 + i * 26, by0 + 32), fill=c)
    d.text((bx0 + 130, by0 + 15), "kq  \u00b7  shop @ ki-demo  \u00b7  AUTONOMY_LEVEL=A2",
           font=mono(21), fill=FAINT)

    # reveal lines over the first 82% of the scene, then hold
    reveal = min(1.0, (t / (dur * 0.82)) if dur > 0 else 1.0)
    shown = max(1, int(round(reveal * len(lines))))
    window = lines[max(0, shown - TERM_ROWS):shown]

    f = mono(23)
    fb = mono(23, "Bold")
    lh = 30
    y = by0 + 78
    for ln in window:
        kind = colourise(ln)
        if kind in ("cmd", "gate"):                # what the human typed
            d.text((bx0 + 34, y), ln, font=fb, fill=COLS[kind])
        else:
            d.text((bx0 + 34, y), ln, font=f, fill=COLS[kind])
        y += lh
        if y > by1 - 34:
            break

    # blinking cursor while still revealing
    if reveal < 1.0 and int(t * 2.4) % 2 == 0:
        d.rectangle((bx0 + 34, y + 4, bx0 + 47, y + 24), fill=ACCENT)

    chrome(im, progress, sc.get("act", ""), sc.get("caption", ""))
    return im


# ------------------------------------------------------------------ shots
SHOTS = V / "shots-dark"
_shot_cache: dict = {}


def render_shot(sc: dict, t: float, dur: float, progress: float) -> Image.Image:
    im = canvas()
    key = sc["source"]
    if key not in _shot_cache:
        _shot_cache[key] = Image.open(SHOTS / key).convert("RGB")
    src = _shot_cache[key]

    # gentle whole-frame scale — nothing is ever cropped away
    grow = 1.0 + 0.018 * (t / dur if dur else 0)
    vw = int(1400 * grow)
    vh = int(round(vw * src.height / src.width))
    bar = 46
    fw, fh = vw, vh + bar
    bx0 = (W - fw) // 2
    by0 = 124 - int((fh - (786 + bar)) / 2)

    shadow = Image.new("RGB", (W, H), BG)
    sd = ImageDraw.Draw(shadow)
    rrect(sd, (bx0 - 8, by0 - 8, bx0 + fw + 8, by0 + fh + 8), 22, fill=(26, 26, 31))
    im = Image.blend(im, shadow.filter(ImageFilter.GaussianBlur(16)), 0.9)
    d = ImageDraw.Draw(im)

    rrect(d, (bx0, by0, bx0 + fw, by0 + fh), 14, fill=BG_RAISED, outline=BORDER_HARD, width=2)
    for i, c in enumerate(((232, 125, 125), (232, 184, 102), ACCENT)):
        d.ellipse((bx0 + 20 + i * 24, by0 + 17, bx0 + 31 + i * 24, by0 + 28), fill=c)
    rrect(d, (bx0 + 130, by0 + 12, bx0 + 610, by0 + 34), 11, fill=BG_SUNKEN)
    d.text((bx0 + 148, by0 + 14), "127.0.0.1:4380/dashboard", font=mono(17), fill=FAINT)

    im.paste(src.resize((vw, vh), Image.LANCZOS), (bx0, by0 + bar))
    d = ImageDraw.Draw(im)
    d.rectangle((bx0, by0 + bar, bx0 + vw, by0 + fh), outline=BORDER_HARD, width=2)

    ease = min(1.0, t / 0.45)
    if ease < 1.0:
        im = Image.blend(canvas(), im, ease)
    chrome(im, progress, sc.get("act", ""), sc.get("caption", ""))
    return im


# ------------------------------------------------------------------- main
def main() -> None:
    durations = json.loads((V / "durations.json").read_text(encoding="utf-8"))
    PAD_IN, PAD_OUT = 0.45, 0.85
    enabled = [sc for sc in S.SCENES if sc.get("enabled", True)]
    plan = [(sc, durations[sc["id"]] + PAD_IN + PAD_OUT) for sc in enabled]
    total = sum(d for _, d in plan)

    frames = V / "frames"
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir()

    n = 0
    elapsed = 0.0
    for sc, dur in plan:
        lines = (load_transcript(sc["source"], sc.get("lines"))
                 if sc["kind"] == "terminal" else [])
        nf = int(round(dur * FPS))
        for i in range(nf):
            t = i / FPS
            progress = (elapsed + t) / total
            if sc["kind"] == "card":
                im = render_card(sc, t, dur, progress)
            elif sc["kind"] == "terminal":
                im = render_terminal(sc, t, dur, progress, lines)
            else:
                im = render_shot(sc, t, dur, progress)
            im.save(frames / f"f{n:06d}.png", compress_level=1)
            n += 1
        elapsed += dur
        print(f"  {sc['id']:<16} {dur:6.2f}s  {nf:5d} frames")

    (V / "plan.json").write_text(json.dumps(
        [{"id": sc["id"], "dur": round(d, 3)} for sc, d in plan], indent=2), encoding="utf-8")
    print(f"\n{n} frames, {total:.1f}s = {int(total//60)}m{int(total%60):02d}s")


if __name__ == "__main__":
    main()
