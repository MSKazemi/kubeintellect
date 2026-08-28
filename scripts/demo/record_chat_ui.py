#!/usr/bin/env python3
"""Record the KubeIntellect chat interface answering a real fault on a real cluster.

The eight `kq` casts under `casts-kq/` show the CLI.  They do not show the surface most
people meet first — the browser chat UI in `deploy/huggingface-space/app.py`, which is what
the public Hugging Face Space serves.  This records that surface, driving the *same*
incident the CLI casts use (`payments-api` crash-looping in `shop`) so the demo follows one
fault across both surfaces instead of showing two unrelated sessions.

Why Playwright and not a screen recorder: `x11grab` captures the physical display, so it
records the lock screen whenever the workstation is locked — a 4-minute capture on
2026-08-28 came back as nothing but a clock.  Playwright drives its own browser and records
from the renderer, so the result does not depend on what is on the monitor, and the viewport
is pinned the way `asciinema`'s `100x34` is pinned for the casts.

    KI_CHAT_URL=http://127.0.0.1:7861 python3 scripts/demo/record_chat_ui.py

Prerequisites — the script does not provision any of them, but it refuses to leave a
recording that misrepresents them, failing loudly if the answers come back wrong:
  * the KubeIntellect server reachable and healthy,
  * `deploy/huggingface-space/app.py` running against it,
  * a Kubernetes cluster carrying the injected faults (`scripts/demo/inject_faults.sh`).

The API key the UI holds decides what the recording can honestly show.  The page's own
footer states the demo key holds the `readonly` role, so run it with a **readonly** key:
with an operator key that sentence on screen becomes false, and the second turn shows a
human-approval prompt rather than the RBAC refusal this script waits for.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import time

from playwright.sync_api import Page, sync_playwright

URL = os.environ.get("KI_CHAT_URL", "http://127.0.0.1:7861")
OUT = pathlib.Path(os.environ.get("KI_CHAT_OUT", pathlib.Path(__file__).parent / "chat-ui"))

# Pinned the way the casts pin 100x34 — a re-record must be frame-comparable with the last.
VIEWPORT = {"width": 1280, "height": 800}

# The first question is verbatim the one in `transcripts-kq/01-crashloop.txt`, so the chat
# section and the CLI section of the video are the same incident.
FAULT_QUESTION = "why is payments-api crash-looping?"
# The second turn exists to reach the refusal.  Asking for a write only gets a proposal
# back; the RBAC denial is emitted when the agent is told to go ahead and actually calls
# `run_kubectl`.
WRITE_REQUEST = "scale the nginx deployment in the default namespace to 3 replicas"
CONFIRM = "yes, go ahead and run it"


# Gradio renders this while a response is still streaming.  Text-stability alone is not
# enough: the coordinator can sit for ten seconds inside "Analyzing your request" without
# painting anything, and a quiet-period check reads that pause as a finished answer.
GENERATING = "Loading content"


def settle(page: Page, quiet: float = 3.0, timeout: float = 240.0) -> str:
    """Wait until the response is no longer generating *and* the text has stopped growing.

    Streaming arrives token by token, so "the answer is finished" is not an event this UI
    exposes.  Returns the final text so the caller can assert on what was actually shown
    rather than on what was expected.
    """
    deadline = time.time() + timeout
    last, stable_since = "", time.time()
    while time.time() < deadline:
        text = page.inner_text("body")
        if text != last:
            last, stable_since = text, time.time()
        elif GENERATING not in text and time.time() - stable_since >= quiet:
            return last
        page.wait_for_timeout(400)
    raise TimeoutError(f"the answer never settled within {timeout:.0f}s")


TURNS: list[dict[str, object]] = []


def ask(page: Page, question: str, *, quiet: float = 3.0) -> str:
    """Type a question, send it, and wait for the answer — timing the wait.

    The wait is measured and written to the manifest so a latency claim rests on a number
    rather than on a stopwatch held to the video, whose length also covers typing, reading
    and scrolling.  Measured 2026-08-28: 15.3 s for the diagnosis, 3.1 s and 5.9 s for the
    two short turns.
    """
    box = page.get_by_placeholder("e.g. why is my api-server pod crashlooping?")
    box.click()
    box.type(question, delay=45)          # typed, not pasted — this is a demo, not a test
    page.wait_for_timeout(600)
    started = time.monotonic()
    box.press("Enter")
    page.mouse.move(8, 8)                 # park: Gradio shows a hover toolbar under the cursor
    answer = settle(page, quiet=quiet)
    TURNS.append({"question": question,
                  "answer_seconds": round(time.monotonic() - started - quiet, 1)})
    return answer


_CLICK_BY_LABEL = """(label) => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().replace(/^[^A-Za-z_]+/, '');
    const order = ['summary', 'button', '[role="button"]', 'div', 'span'];
    for (const sel of order) {
        const hit = [...document.querySelectorAll(sel)]
            .filter(e => norm(e.textContent) === label && e.getClientRects().length);
        if (hit.length) { hit[hit.length - 1].click(); return true; }
    }
    return false;
}"""


def expand(page: Page, label: str) -> None:
    """Open one collapsible activity block and let it paint.

    The page promises "expand the grey blocks in any answer to see the real kubectl calls
    behind it".  Only the tool blocks carry a body; the status blocks (`Targeting …`,
    `Investigating …`) expand to nothing, which is why this is called by name.

    Matched in the page rather than with `get_by_text`, because the visible label carries a
    leading emoji (`🛠 run_kubectl`) that an exact-text locator will not match and a
    substring locator matches twice — `run_kubectl` is also a prefix of `run_kubectl
    output`.
    """
    if not page.evaluate(_CLICK_BY_LABEL, label):
        raise LookupError(f"no clickable block labelled {label!r}")
    page.mouse.move(8, 8)
    page.wait_for_timeout(1200)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(OUT / ".video"),
            record_video_size=VIEWPORT,
            device_scale_factor=1,
        )
        page = context.new_page()
        video = page.video
        started = time.monotonic()
        # Saved in `finally`: a run that reaches the second turn and then fails on a
        # selector has already spent two coordinator turns against a live cluster, and
        # throwing the footage away makes the next attempt start from nothing.
        try:
            page.goto(URL, wait_until="networkidle")
            page.wait_for_timeout(2500)

            answer = ask(page, FAULT_QUESTION)
            if "DATABASE_URL" not in answer:
                print("FAIL: the diagnosis did not name DATABASE_URL — is the fault injected?",
                      file=sys.stderr)
                print(answer[-1500:], file=sys.stderr)
                return 1
            page.wait_for_timeout(2000)

            # Walk the answer the way a reader would, not cutting straight to the end.
            for _ in range(6):
                page.mouse.wheel(0, 260)
                page.wait_for_timeout(700)
            page.wait_for_timeout(1500)

            ask(page, WRITE_REQUEST)
            page.wait_for_timeout(1500)
            refusal = ask(page, CONFIRM)
            if "read-only" not in refusal:
                print("FAIL: the write was not refused — is the UI holding an operator key?",
                      file=sys.stderr)
                print(refusal[-1500:], file=sys.stderr)
                return 1

            # The refusal is the point of the second turn: show the command and the denial.
            expand(page, "run_kubectl")
            expand(page, "run_kubectl output")
            page.mouse.wheel(0, 260)
            page.wait_for_timeout(3500)
        finally:
            wall = round(time.monotonic() - started, 1)
            context.close()
            browser.close()
            src = pathlib.Path(video.path())
            dst = OUT / "chat-ui-crashloop.webm"
            src.replace(dst)
            manifest = OUT / "chat-ui-crashloop.json"
            manifest.write_text(json.dumps({
                "recorded_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url": URL,
                "viewport": VIEWPORT,
                "api_key_role": "readonly",
                "wall_seconds": wall,
                "turns": TURNS,
                "note": ("Playback runs at wall-clock rate — the file length matches "
                         "wall_seconds. answer_seconds is measured from pressing Enter "
                         "to the answer settling; quote that, not a stopwatch on the "
                         "video, which also carries typing and scrolling."),
            }, indent=2) + "\n", encoding="utf-8")
            print(f"{dst}  {dst.stat().st_size / 1e6:.1f} MB  "
                  f"viewport {VIEWPORT['width']}x{VIEWPORT['height']}  wall {wall}s")
            print(f"{manifest}  turns={[t['answer_seconds'] for t in TURNS]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
