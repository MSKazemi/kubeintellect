#!/usr/bin/env python3
"""Drive the real `kq` REPL through a pty, so a recording shows what a user actually sees.

The first demo corpus recorded the raw SSE stream, which meant the casts showed literal `###`
and `**bold**` markers instead of rendered output. `kq` renders through rich's Markdown, but it
uses prompt_toolkit for input and therefore refuses to run without a terminal. This allocates one.

Turn boundaries are detected by **silence**, not by matching the prompt string: the prompt is
themed, drawn by prompt_toolkit, and rewritten in place, so it is not reliably greppable in the
byte stream. "No output for QUIET seconds" is a property of the stream itself and survives
retheming.

    KUBE_Q_API_KEY=... python scripts/demo/kq_pty_driver.py \\
        --scenario scripts/demo/scenarios/06-approval-gate.txt \\
        --url http://127.0.0.1:30080 --json-log gates.jsonl
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import re
import select
import shutil
import struct
import sys
import termios
import time

import pyte

CSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")
# The gate banner the server emits. Matched on de-ANSI'd text because rich splits styled runs
# across escape sequences, so the literal words are not contiguous in the raw bytes.
# Two independent signals, because they fail differently. The server's banner ("Approval
# Required — risk level: ...") is the semantic one; kq's own "HITL>" prompt is structural and
# appears only when the client has actually entered the approval state.
#
# Deliberately NOT matched: a model that merely writes "this requires approval ... shall I
# proceed?" in prose. That is narration, not a gate -- the run continues whatever you answer.
# Treating it as a gate would manufacture evidence that the system enforced something it did not.
GATE = re.compile(r"approval required|hitl>", re.I)
# The command as shown to the operator. The tool-run line ("kubectl -> Running: kubectl ...")
# also matches, so take the LAST kubectl on the line and strip any arrow prefix.
CMD = re.compile(r"kubectl\s+(?!\u2192)[^\n`]+")


# prompt_toolkit asks the terminal where the cursor is with ESC[6n and, when no answer
# arrives, prints "your terminal doesn't support cursor position requests (CPR)" -- into
# the recording, above the first answer. There is no terminal behind this pty to reply:
# we are the terminal. So keep a screen model and answer from it.
CPR_QUERY = re.compile(rb"\x1b\[6n")


class CursorTracker:
    """A screen model kept only so the CPR reply is the truth rather than a guess."""

    def __init__(self, cols: int, rows: int) -> None:
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)

    def feed(self, chunk: bytes) -> None:
        self.stream.feed(chunk.decode("utf-8", "replace"))

    def report(self) -> bytes:
        # CPR is 1-based; pyte's cursor is 0-based.
        return f"\x1b[{self.screen.cursor.y + 1};{self.screen.cursor.x + 1}R".encode()


def drain(fd: int, out: list[bytes], budget: float,
          track: CursorTracker | None = None) -> bytes:
    """Copy whatever the child has written for up to `budget` seconds. Returns the new bytes."""
    got = b""
    end = time.time() + budget
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.15)
        if not r:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                raise EOFError from exc
            raise
        if not chunk:
            raise EOFError
        os.write(1, chunk)          # to our stdout, which asciinema is recording
        out.append(chunk)
        got += chunk
        if track is not None:
            track.feed(chunk)       # feed first: the reply must reflect this chunk
            if CPR_QUERY.search(chunk):
                os.write(fd, track.report())
    return got


def wait_quiet(fd: int, out: list[bytes], quiet: float, timeout: float,
               track: CursorTracker | None = None) -> bytes:
    """Block until the child has produced nothing for `quiet` seconds."""
    seen = b""
    last = time.time()
    end = time.time() + timeout
    while time.time() < end:
        new = drain(fd, out, 0.25, track)
        if new:
            seen += new
            last = time.time()
        elif time.time() - last >= quiet:
            return seen
    return seen


def send(fd: int, text: str, cps: float) -> None:
    """Type a line at a human rate, so the recording is readable rather than instant."""
    for ch in text:
        os.write(fd, ch.encode())
        time.sleep(1.0 / cps)
    time.sleep(0.35)
    os.write(fd, b"\r")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:30080")
    ap.add_argument("--json-log")
    ap.add_argument("--approve-phrase", default="approve")
    ap.add_argument("--deny-phrase", default="deny")
    ap.add_argument("--deny-nth", type=int)
    ap.add_argument("--quiet", type=float, default=3.0, help="silence that ends a turn")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--type-cps", type=float, default=18.0)
    ap.add_argument("--read-pause", type=float, default=2.0,
                    help="pause after an answer, so a viewer can read it before the next turn")
    args = ap.parse_args()

    if not os.environ.get("KUBE_Q_API_KEY"):
        print("KUBE_Q_API_KEY is not set; kq would reach no gate at all.", file=sys.stderr)
        return 2
    kq = shutil.which("kq")
    if not kq:
        print("kq not on PATH", file=sys.stderr)
        return 2

    prompts = [ln for ln in open(args.scenario, encoding="utf-8").read().splitlines()
               if ln.strip() and not ln.lstrip().startswith("#")]

    # The inner pty MUST be the size of the terminal this driver is running in --
    # which, under `asciinema rec`, is the size written into the cast header. Pick a
    # size of our own and rich lays the answer out for that width while the header
    # advertises another; every replayer then hard-wraps 100-column output into 80
    # columns, breaking words mid-token. The recording is not wrong, its declared
    # geometry is, and nothing downstream can tell the difference.
    try:
        term = os.get_terminal_size(1)
    except OSError:                                  # not a tty (piped run)
        term = os.terminal_size((100, 34))

    pid, fd = pty.fork()
    if pid == 0:                                     # child
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLUMNS"], os.environ["LINES"] = str(term.columns), str(term.lines)
        # env alone is a fallback; anything that asks the tty directly needs this.
        fcntl.ioctl(0, termios.TIOCSWINSZ,
                    struct.pack("HHHH", term.lines, term.columns, 0, 0))
        os.execvp(kq, [kq, "--url", args.url])
        os._exit(127)

    out: list[bytes] = []
    gates: list[dict] = []
    gate_n = 0
    try:
        track = CursorTracker(term.columns, term.lines)
        wait_quiet(fd, out, args.quiet, 60, track)   # banner + first prompt
        for turn, prompt in enumerate(prompts, 1):
            send(fd, prompt, args.type_cps)
            body = wait_quiet(fd, out, args.quiet, args.timeout, track)
            text = CSI.sub(b"", body).decode("utf-8", "replace")

            if GATE.search(text):
                gate_n += 1
                deny = args.deny_nth is not None and gate_n == args.deny_nth
                answer = args.deny_phrase if deny else args.approve_phrase
                hits = [m.group(0).strip() for m in CMD.finditer(text)]
                gates.append({"turn": turn, "gate": gate_n,
                              "human_summary": hits[0] if hits else "",
                              "answered_with": "deny" if deny else "approve"})
                time.sleep(args.read_pause)          # the human reads before answering
                send(fd, answer, args.type_cps)
                wait_quiet(fd, out, args.quiet, args.timeout, track)
            time.sleep(args.read_pause)
        # Ctrl-D rather than typing "/exit": the completion menu pops open on the leading "/"
        # and the recording ends on a dropdown covering the last answer.
        os.write(fd, b"\x04")
        wait_quiet(fd, out, 1.5, 20, track)
    except EOFError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    if args.json_log:
        with open(args.json_log, "w", encoding="utf-8") as fh:
            for g in gates:
                fh.write(json.dumps(g) + "\n")
    print(f"\n[driver] {len(prompts)} prompt(s), {len(gates)} gate(s) answered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
