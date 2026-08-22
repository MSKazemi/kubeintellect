#!/usr/bin/env python3
"""Render an asciinema v2 cast to an optimized GIF (pyte + Pillow).

No external binaries required — no asciinema, agg or ffmpeg.

    pip install pyte pillow
    python scripts/demo/cast_to_gif.py in.cast out.gif
"""
from __future__ import annotations

import argparse
import json

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

FONT_CANDIDATES = [
    "/home/mohsen/.local/share/fonts/JetBrainsMonoNerd/JetBrainsMonoNerdFontMono-{}.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono{}.ttf",
]


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


def load_fonts(size: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    for tmpl in FONT_CANDIDATES:
        try:
            regular = ImageFont.truetype(tmpl.format("Regular"), size)
            bold = ImageFont.truetype(tmpl.format("Bold"), size)
            return regular, bold
        except OSError:
            continue
    raise SystemExit("no monospace font found; install DejaVu Sans Mono")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cast")
    ap.add_argument("out")
    ap.add_argument("--fps", type=float, default=10.0)
    ap.add_argument("--font-size", type=int, default=16)
    ap.add_argument("--pad", type=int, default=14)
    ap.add_argument("--colors", type=int, default=64)
    ap.add_argument("--tail-hold", type=float, default=2.5,
                    help="extra seconds held on the final frame")
    ap.add_argument("--alias", action="store_true",
                    help="disable font antialiasing (smaller file, harsher text)")
    args = ap.parse_args()

    with open(args.cast, encoding="utf-8") as fh:
        header = json.loads(fh.readline())
        events = [json.loads(line) for line in fh if line.strip()]
    cols, rows = header["width"], header["height"]

    font, font_b = load_fonts(args.font_size)
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
                    d.text((px, py + baseline), cell.data,
                           font=font_b if cell.bold else font, fill=fg, anchor="ls")
        return img

    frames: list[Image.Image] = []
    durations: list[int] = []
    interval = 1.0 / args.fps
    duration = events[-1][0]
    next_t, ei, prev_sig = 0.0, 0, None

    while next_t <= duration:
        while ei < len(events) and events[ei][0] <= next_t:
            if events[ei][1] == "o":
                stream.feed(events[ei][2])
            ei += 1
        sig = tuple(
            tuple((screen.buffer[y][x].data, screen.buffer[y][x].fg,
                   screen.buffer[y][x].bg, screen.buffer[y][x].bold)
                  for x in range(cols))
            for y in range(rows)
        )
        if sig != prev_sig:
            frames.append(snapshot())
            durations.append(int(interval * 1000))
            prev_sig = sig
        elif durations:
            durations[-1] += int(interval * 1000)
        next_t += interval

    if not frames:
        raise SystemExit("cast produced no frames")
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
