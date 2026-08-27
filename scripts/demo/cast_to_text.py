#!/usr/bin/env python3
"""Render an asciinema v2 cast to plain text.

The cast is the source of record, but nobody reviews a cast -- and a GIF cannot be grepped or
diffed. This produces the reading copy that `evaluation/test_a_demo_that_only_ever_approves_
has_not_shown_a_gate.py` asserts against, so the transcripts stay derivable from the recordings
rather than being a second, editable source of truth.

    python scripts/demo/cast_to_text.py casts/06-approval-gate.cast > transcripts/06-approval-gate.txt
"""
from __future__ import annotations

import json
import re
import sys

# CSI sequences only. This is deliberately not a terminal emulator: the casts are append-only
# streams of a chat client, with no cursor addressing to replay, so stripping the colour codes
# reproduces the visible text exactly. A cast that ever uses cursor movement would need pyte.
CSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def cast_to_text_emulated(path: str, width: int | None = None) -> str:
    """Render through a real terminal emulator.

    Required for any cast where the program repaints: rich's Live display, prompt_toolkit's
    input line and its completion menu all move the cursor and overwrite, so concatenating the
    output bytes reproduces every intermediate frame instead of what was on screen. Stripping
    CSI codes from such a stream yields a transcript that is both unreadable and wrong.
    """
    import pyte  # optional; only the emulated path needs it

    lines = open(path, encoding="utf-8").read().splitlines()
    header = json.loads(lines[0])
    screen = pyte.Screen(width or header.get("width", 100), 8000)
    stream = pyte.Stream(screen)
    for line in lines[1:]:
        if not line.strip():
            continue
        ev = json.loads(line)
        if len(ev) >= 3 and ev[1] == "o":
            stream.feed(ev[2])
    text = "\n".join(row.rstrip() for row in screen.display).rstrip()
    return re.sub(r"\n{3,}", "\n\n", text) + "\n"


def cast_to_text(path: str) -> str:
    out: list[str] = []
    with open(path, encoding="utf-8") as fh:
        fh.readline()  # header
        for line in fh:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if len(ev) >= 3 and ev[1] == "o":
                out.append(ev[2])
    text = CSI.sub("", "".join(out))
    return text.replace("\r\n", "\n").replace("\r", "")


def main() -> int:
    args = sys.argv[1:]
    emulate = "--emulate" in args
    if emulate:
        args.remove("--emulate")
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    sys.stdout.write(cast_to_text_emulated(args[0]) if emulate else cast_to_text(args[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
