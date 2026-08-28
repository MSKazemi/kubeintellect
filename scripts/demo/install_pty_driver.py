#!/usr/bin/env python3
"""Drive `kubeintellect init` through a pty, so the install can be recorded as a real session.

`init` is a wizard built on plain `input()`: it prints a prompt, blocks, and reads a line. Piping
a here-doc into it does record *a* session, but not the one a user sees -- with no terminal the
answers never echo, so the cast shows prompts answering themselves. This allocates a pty and
types the answers into it at a human cadence, the same way `kq_pty_driver.py` does for the REPL.

Where it deliberately differs from that driver: **turns are matched on the prompt text, not on
silence.** The REPL's prompt is themed and rewritten in place by prompt_toolkit, so it is not
reliably greppable and silence is the only honest signal. `input()` is the opposite -- it writes
one literal, stable string and then blocks forever. Matching it is what makes the recording
checkable: a prompt that never arrives, or arrives in an order the scenario did not expect, ends
the run non-zero instead of quietly recording a different demo. A wizard that silently took the
default for a question the scenario meant to answer is exactly the failure that looks fine.

Scenario format -- one `expect -> send` pair per line, `#` comments and blank lines ignored:

    # substring to wait for            :: what to type
    Choose [1/2]                       :: 1
    OPENAI_API_KEY:                    :: sk-demo-not-a-real-key
    Create a local Kind cluster        :: y

    KUBEINTELLECT_HOME=... python scripts/demo/install_pty_driver.py \\
        --scenario scripts/demo/scenarios/09-install.txt \\
        --command 'kubeintellect init' --json-log answers.jsonl
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import re
import select
import shlex
import shutil
import struct
import sys
import termios
import fcntl
import time

import pyte

CSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]")


def _load_scenario(path: str) -> list[tuple[str, str]]:
    """Parse `expect :: send` pairs. Order is significant and is enforced at run time."""
    steps: list[tuple[str, str]] = []
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "::" not in line:
            raise SystemExit(f"{path}: line is not 'expect :: send': {line!r}")
        expect, send = line.split("::", 1)
        steps.append((expect.strip(), send.strip()))
    if not steps:
        raise SystemExit(f"{path}: no steps")
    return steps


def _winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--command", required=True, help="the command to drive, e.g. 'kubeintellect init'")
    ap.add_argument("--json-log", help="one line per answered prompt: step, expect, sent")
    ap.add_argument("--timeout", type=float, default=600.0, help="per-step wait for its prompt")
    ap.add_argument("--type-cps", type=float, default=18.0)
    ap.add_argument("--read-pause", type=float, default=1.2,
                    help="pause before answering, so a viewer can read the prompt")
    args = ap.parse_args()

    steps = _load_scenario(args.scenario)

    # Size the child's pty from the terminal we are running in. asciinema records the geometry it
    # was told to use; if the child lays out for a different width, every replayer wraps the output
    # mid-token. Same reasoning, and the same bug, as the kq driver's first pass.
    cols, rows = shutil.get_terminal_size((100, 34))
    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("bash", ["bash", "-lc", args.command])
    _winsize(fd, cols, rows)

    log = open(args.json_log, "a", encoding="utf-8") if args.json_log else None
    step = 0
    deadline = time.monotonic() + args.timeout
    pending_since: float | None = None
    rc = 0

    try:
        while step < len(steps):
            expect, send = steps[step]
            if time.monotonic() > deadline:
                sys.stderr.write(f"\nTIMEOUT waiting for step {step + 1}: {expect!r}\n")
                rc = 2
                break
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
                stream.feed(data)

            # Match against the rendered screen, not the raw byte stream: the wizard colours its
            # prompts, so the literal words are not contiguous in the bytes even though they are
            # contiguous on screen.
            text = "\n".join(screen.display)
            if expect in text:
                if pending_since is None:
                    pending_since = time.monotonic()
                elif time.monotonic() - pending_since >= args.read_pause:
                    for ch in send:
                        os.write(fd, ch.encode())
                        time.sleep(1.0 / args.type_cps)
                    os.write(fd, b"\r")
                    if log:
                        log.write(json.dumps({"step": step + 1, "expect": expect, "sent": send}) + "\n")
                        log.flush()
                    screen.reset()
                    step += 1
                    pending_since = None
                    deadline = time.monotonic() + args.timeout
        # drain whatever the command prints after its last answer
        end = time.monotonic() + 8.0
        while time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                break
            os.write(sys.stdout.fileno(), data)
    finally:
        if log:
            log.close()
        try:
            os.close(fd)
        except OSError:
            pass
        _, status = os.waitpid(pid, 0)

    if step < len(steps) and rc == 0:
        sys.stderr.write(f"\nENDED EARLY: answered {step}/{len(steps)} prompts; "
                         f"next expected {steps[step][0]!r}\n")
        rc = 3
    return rc or (os.waitstatus_to_exitcode(status) if status else 0)


if __name__ == "__main__":
    sys.exit(main())
