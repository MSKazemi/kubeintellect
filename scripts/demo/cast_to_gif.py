#!/usr/bin/env python3
"""Render an asciinema v2 cast to an optimized GIF (pyte + Pillow).

No external binaries required — no asciinema, agg or ffmpeg.

    pip install pyte pillow
    python scripts/demo/cast_to_gif.py in.cast out.gif
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

# Dark palette close to GitHub's dark theme, so the GIF sits well in the README.
BG = (13, 17, 23)
FG = (201, 209, 217)

ANSI: dict[str, tuple[int, int, int]] = {
    "black": (48, 54, 61), "red": (255, 123, 114), "green": (86, 211, 100),
    "brown": (210, 168, 76), "yellow": (210, 168, 76), "blue": (121, 192, 255),
    "magenta": (219, 158, 255), "cyan": (86, 205, 216), "white": (201, 209, 217),
    "brightblack": (110, 118, 129), "brightred": (255, 163, 155),
    "brightgreen": (126, 231, 135), "brightyellow": (232, 196, 121),
    "brightblue": (150, 208, 255), "brightmagenta": (231, 186, 255),
    "brightcyan": (137, 224, 232), "brightwhite": (240, 246, 252),
}

# Tried in order, and used in order per *character*, not per file. No single monospace font
# covers what a terminal UI actually emits: JetBrains Mono has no Braille (the spinner) and no
# U+2699 (the tool marker), and DejaVu Sans Mono has the gear but not the Braille either. A
# renderer with one font draws the .notdef box for those and the demo grows a row of tofu.
# Explicit (regular, bold) pairs rather than one template with a slot: the four families name
# their weights four different ways, and a template that guesses wrong does not fail loudly --
# the pair just never loads and that family silently leaves the chain.
#: Per-user fonts live under the running user's home, never a hardcoded one. The absolute
#: path baked in here worked on exactly one machine and silently dropped this family -- the
#: preferred one -- out of the chain for everybody else, with no error anywhere.
_USER_FONTS = f"{Path.home()}/.local/share/fonts"

FONT_CANDIDATES = [
    (f"{_USER_FONTS}/JetBrainsMonoNerd/JetBrainsMonoNerdFontMono-Regular.ttf",
     f"{_USER_FONTS}/JetBrainsMonoNerd/JetBrainsMonoNerdFontMono-Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"),
    ("/usr/share/fonts/opentype/freefont/FreeMono.otf",
     "/usr/share/fonts/opentype/freefont/FreeMonoBold.otf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    # Last, and the same file in both slots on purpose: it ships no bold, and the risk marker
    # on the approval banner (U+1F7E1) is the one character in the whole corpus that nothing
    # above carries. NotoColorEmoji has it too, but it is a CBDT bitmap font Pillow will only
    # render at its native 109px -- unusable in a character cell.
    ("/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
     "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf"),
]

# A codepoint permanently unassigned by Unicode, so whatever a font draws for it is that font's
# .notdef. Comparing a glyph's bitmap against it is the coverage test that actually works:
# ``getmask(ch).getbbox()`` is not None for .notdef either -- the tofu box has ink.
_NOTDEF_PROBE = "\uffff"


def to_rgb(name: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """Resolve a pyte colour name to RGB.

    ``"default"`` must resolve to the *caller's* default (FG for text, BG for
    background). Mapping it to a fixed colour paints a filled rectangle behind
    every cell and hides the text.
    """
    if name == "default":
        return default
    if name in ANSI:
        return ANSI[name]
    if len(name) == 6:
        try:
            return tuple(int(name[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            pass
    return default


def load_font_chain(size: int) -> list[tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]]:
    """Every candidate that loads, in preference order. The first one sets the cell metrics."""
    chain = []
    for regular, bold in FONT_CANDIDATES:
        try:
            chain.append((ImageFont.truetype(regular, size), ImageFont.truetype(bold, size)))
        except OSError:
            continue
    if not chain:
        raise SystemExit("no monospace font found; install DejaVu Sans Mono")
    return chain


def load_fonts(size: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """The primary pair -- the one whose advance width defines the cell grid."""
    return load_font_chain(size)[0]


def font_picker(chain: list[tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]]):
    """Return ``pick(char, bold)`` -> the first font in the chain that really has the glyph.

    Falls back to the primary font when nobody has it, so an unknown character still draws the
    box rather than vanishing: a silently dropped character would be a worse lie than a visible
    one. Memoised because this runs per cell per frame.
    """
    notdef = [(bytes(r.getmask(_NOTDEF_PROBE)), bytes(b.getmask(_NOTDEF_PROBE)))
              for r, b in chain]
    cache: dict[tuple[str, bool], ImageFont.FreeTypeFont] = {}

    def pick(ch: str, bold: bool) -> ImageFont.FreeTypeFont:
        key = (ch, bold)
        hit = cache.get(key)
        if hit is not None:
            return hit
        chosen = chain[0][1 if bold else 0]
        for i, pair in enumerate(chain):
            font = pair[1 if bold else 0]
            try:
                if bytes(font.getmask(ch)) != notdef[i][1 if bold else 0]:
                    chosen = font
                    break
            except OSError:
                continue
        cache[key] = chosen
        return chosen

    return pick


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cast")
    ap.add_argument("out")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--speed", type=float, default=1.0,
                    help="playback rate; <1 is slower. Streaming answers arrive faster than "
                         "anyone reads them, so real time is the wrong speed for a demo")
    ap.add_argument("--min-frame-ms", type=int, default=0,
                    help="floor on any single frame, so a frame that changed cannot flash past")
    ap.add_argument("--idle-cells", type=int, default=2,
                    help="a frame changing at most this many cells has not changed the screen -- "
                         "it is an animation. Measured against the last frame actually kept, so "
                         "a spinner (one cell, oscillating forever) stays below the bar while "
                         "typing (one more cell each frame) climbs past it within a few "
                         "characters. Without this the spinner alone is 60%% of the frames")
    ap.add_argument("--max-frame-ms", type=int, default=0,
                    help="ceiling on any single frame. A cast of a real session is mostly dead "
                         "time -- the model thinks for 30s, then paints an answer in 190ms -- "
                         "so unclamped playback spends its length on a spinner and flashes the "
                         "content past. The cap compresses the waiting, not the reading")
    ap.add_argument("--font-size", type=int, default=16)
    ap.add_argument("--pad", type=int, default=14)
    ap.add_argument("--colors", type=int, default=64)
    ap.add_argument("--start", type=float, default=0.0,
                    help="first second of the cast to show. Everything before it is still fed "
                         "to the screen -- a window that only replayed its own events would "
                         "open on a blank terminal instead of the state the user was looking at")
    ap.add_argument("--end", type=float,
                    help="last second of the cast to show; default is the end of the recording")
    ap.add_argument("--tail-hold", type=float, default=2.5,
                    help="extra seconds held on the final frame")
    ap.add_argument("--alias", action="store_true",
                    help="disable font antialiasing (smaller file, harsher text)")
    args = ap.parse_args()

    with open(args.cast, encoding="utf-8") as fh:
        header = json.loads(fh.readline())
        events = [json.loads(line) for line in fh if line.strip()]
    cols, rows = header["width"], header["height"]

    chain = load_font_chain(args.font_size)
    font, font_b = chain[0]
    pick_font = font_picker(chain)
    cw = int(round(font.getlength("M")))
    ch = int(round(args.font_size * 1.34))
    # Draw from the BASELINE. Anchoring at the cell top pushes commas and periods
    # upward (they read as apostrophes) and clips p/g/y descenders.
    f_ascent, f_descent = font.getmetrics()
    baseline = (ch - (f_ascent + f_descent)) // 2 + f_ascent

    W = cols * cw + 2 * args.pad
    H = rows * ch + 2 * args.pad

    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)

    def cell_sig(cell) -> tuple | None:  # noqa: ANN001
        """Exactly what ``snapshot`` would paint for one cell, and nothing else.

        This mirrors the drawing rules rather than dumping the cell, because a
        terminal erases by writing a space: ``""`` and ``" "`` differ as data and
        render identically. It is a cheap *pre-filter* only — never authoritative,
        because two different characters can still paint the same pixels (every
        glyph the font lacks draws the same tofu box, and a spinner cycling
        through braille frames the font does not carry is exactly that case).
        The frame list is deduplicated on rendered bytes below.
        """
        blank = not cell.data or cell.data == " "
        if blank and cell.bg == "default":
            return None
        fg = to_rgb(cell.fg, FG)
        bg = to_rgb(cell.bg, BG)
        if cell.reverse:
            fg, bg = bg, fg
        painted_bg = bg if bg != BG else None
        if blank:
            return (None, None, painted_bg)
        if cell.bold and cell.fg == "default":
            fg = ANSI["brightwhite"]
        return (cell.data, fg, painted_bg, cell.bold)

    def changed_enough(sig, prev, limit: int) -> bool:
        """Do more than ``limit`` painted cells differ between two screen signatures?

        Short-circuits, and skips whole rows that compare equal, because the common case
        is a screen where one cell moved. ``prev is None`` is the first frame: everything
        changed.
        """
        if prev is None:
            return True
        n = 0
        for row, prow in zip(sig, prev):
            if row == prow:
                continue
            for cell, pcell in zip(row, prow):
                if cell != pcell:
                    n += 1
                    if n > limit:
                        return True
        return False

    def snapshot() -> Image.Image:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        if args.alias:
            d.fontmode = "1"
        for y in range(rows):
            line = screen.buffer[y]
            for x in range(cols):
                cell = line[x]
                blank = not cell.data or cell.data == " "
                if blank and cell.bg == "default":
                    continue
                fg = to_rgb(cell.fg, FG)
                bg = to_rgb(cell.bg, BG)
                if cell.reverse:
                    fg, bg = bg, fg
                px, py = args.pad + x * cw, args.pad + y * ch
                if bg != BG:
                    d.rectangle([px, py, px + cw, py + ch], fill=bg)
                if not blank:
                    if cell.bold and cell.fg == "default":
                        fg = ANSI["brightwhite"]
                    # U+FE0F rides along with symbols like the gear; no monospace font
                    # carries it, so drawing it paints a tofu box next to the glyph.
                    glyph = cell.data.replace("\ufe0f", "")
                    d.text((px, py + baseline), glyph,
                           font=pick_font(glyph, cell.bold), fill=fg, anchor="ls")
        return img

    frames: list[Image.Image] = []
    durations: list[int] = []
    interval = 1.0 / args.fps
    duration = args.end if args.end is not None else events[-1][0]
    next_t, ei, prev_sig, prev_raw = 0.0, 0, None, None

    # Wind the screen forward to --start without recording anything. The window has to open on
    # the terminal as it really was, banner and scrollback included.
    while ei < len(events) and events[ei][0] <= args.start:
        if events[ei][1] == "o":
            stream.feed(events[ei][2])
        ei += 1
    next_t = args.start

    while next_t <= duration:
        while ei < len(events) and events[ei][0] <= next_t:
            if events[ei][1] == "o":
                stream.feed(events[ei][2])
            ei += 1
        sig = tuple(
            tuple(cell_sig(screen.buffer[y][x]) for x in range(cols))
            for y in range(rows)
        )
        # ``prev_sig`` is the last frame that was *kept*, not the last one sampled, so a
        # change that arrives one cell at a time accumulates against what is on screen
        # instead of being compared away frame by frame.
        if changed_enough(sig, prev_sig, args.idle_cells):
            img = snapshot()
            # Authoritative test: identical pixels are not a frame. Pillow drops
            # such a frame on save anyway and folds its duration into the one
            # before it, so keeping it here would apply --min-frame-ms and
            # --max-frame-ms to frames that never reach the file — and the length
            # this script reports would not be the length of the GIF it wrote.
            raw = img.tobytes()
            if raw != prev_raw:
                frames.append(img)
                durations.append(int(interval * 1000))
                prev_sig = sig
                prev_raw = raw
                next_t += interval
                continue
            # Cells differ but pixels do not, so nothing new is on screen and ``prev_sig``
            # must keep pointing at the frame that is.
        if durations:
            durations[-1] += int(interval * 1000)
        next_t += interval

    if not frames:
        raise SystemExit("cast produced no frames")
    if args.speed != 1.0:
        durations = [max(1, int(d / args.speed)) for d in durations]
    if args.max_frame_ms:
        durations = [min(d, args.max_frame_ms) for d in durations]
    if args.min_frame_ms:
        # Floor last, so a cap set below the floor still yields the floor instead
        # of silently inverting the two into a cap.
        durations = [max(d, args.min_frame_ms) for d in durations]
    durations[-1] += int(args.tail_hold * 1000)

    # One shared adaptive palette. Build it from frames that actually have
    # content — a palette derived from the blank first frame collapses every
    # frame to solid background.
    idx = sorted({int(len(frames) * f) for f in (0.35, 0.6, 0.8, 0.99)}
                 & set(range(len(frames))))
    montage = Image.new("RGB", (W, H * len(idx)))
    for i, j in enumerate(idx):
        montage.paste(frames[j], (0, i * H))
    pal = montage.quantize(colors=args.colors, method=Image.Quantize.FASTOCTREE)
    quantized = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]

    quantized[0].save(
        args.out, save_all=True, append_images=quantized[1:], duration=durations,
        loop=0, optimize=True, disposal=1,
    )
    print(f"{args.out}: {len(quantized)} frames, {W}x{H}, {sum(durations) / 1000:.1f}s")


if __name__ == "__main__":
    main()
