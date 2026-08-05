#!/usr/bin/env python3
"""Build the README demo cast from the *real* kq renderer.

Every panel, icon, colour and footer in the resulting recording is produced by
the shipping code in ``v4/packages/kube-q/kube_q/cli/renderer.py`` — nothing here
is a mock-up. The assistant's answer text is taken verbatim from a real recorded
troubleshooting session, so the content is real too.

The recording is *scripted and time-compressed*: a real model round-trip takes
several seconds, and the spinner beats here are shortened so the whole story fits
in ~19 seconds. No latency or cost claim is made anywhere from this asset.

Usage:
    python scripts/demo/build_demo_cast.py out.cast
    python scripts/demo/cast_to_gif.py out.cast .github/assets/kubeintellect-demo.gif
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "v4" / "packages" / "kube-q"))
os.environ.pop("NO_COLOR", None)

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402

from kube_q.cli import renderer  # noqa: E402
from kube_q.cli.theme import get_theme  # noqa: E402

COLS, ROWS = 100, 30

# Point the renderer's shared console at a capture buffer, forcing ANSI on.
_buf = io.StringIO()
renderer.console = Console(
    file=_buf, force_terminal=True, color_system="truecolor",
    width=COLS, theme=get_theme(), highlight=False,
)


def capture(fn, *a, **kw) -> str:
    """Run a real renderer function and return exactly the ANSI it emitted."""
    _buf.seek(0)
    _buf.truncate(0)
    fn(*a, **kw)
    return _buf.getvalue()


def markup(text: str) -> str:
    """Render Rich markup through the same themed console."""
    return capture(renderer.console.print, text)


events: list[list] = []
_t = 0.0


def emit(data: str, dt: float = 0.0) -> None:
    global _t
    _t += dt
    events.append([round(_t, 3), "o", data])


def emit_block(ansi: str, dt: float = 0.0) -> None:
    """Emit a rendered block, converting bare LF to CRLF for the terminal."""
    emit(ansi.replace("\n", "\r\n"), dt)


def type_out(text: str, cps: float = 42.0) -> None:
    """Type a line character-by-character, like a human at the prompt."""
    for chunk in text:
        emit(chunk, 1.0 / cps)
    emit("\r\n", 0.35)


def spinner(seconds: float, label: str = "KubeIntellect is thinking…") -> None:
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    step = 0.09
    for i in range(int(seconds / step)):
        emit(f"\r\033[2m{frames[i % len(frames)]}  {label}\033[0m\033[K", step)
    emit("\r\033[K", 0.05)


def build() -> None:
    # 1. Banner — the real logo art and tagline.
    emit("\r\n" + renderer._DEFAULT_LOGO_ART + "\r\n", 0.4)
    emit(f"\033[2m   {renderer._DEFAULT_TAGLINE}\033[0m\r\n\r\n", 0.25)

    # 2. Session header — the real Panel.fit from repl.py.
    emit_block(capture(renderer.console.print, Panel.fit(
        "[dim]API:[/dim] https://api.kubeintellect.com\n"
        "[dim]Conversation:[/dim] 03bcfbe0-d299-47b4-87c7-02d24a6e0169\n"
        "[dim]Type [yellow]/help[/yellow] for commands · "
        "[yellow]Enter[/yellow] to send · "
        "[yellow]Alt+Enter[/yellow] for newline[/dim]",
        border_style="dim cyan",
    )), 0.5)
    emit("\r\n", 0.6)

    # 3. Turn one — diagnose.
    emit(markup("[user]You:[/user] ").rstrip("\n"), 0.3)
    type_out("why is the backend-api deployment crashlooping in staging?")
    emit("\r\n", 0.2)
    spinner(2.2)

    emit_block(capture(renderer.render_plan, {"steps": [
        {"description": "List pods in namespace staging", "status": "done"},
        {"description": "Read backend-api container logs", "status": "done"},
        {"description": "Check services and endpoints in staging", "status": "running"},
    ]}), 0.35)
    emit("\r\n", 0.5)

    emit_block(markup(
        "[agent]KubeIntellect:[/agent]\n"
        "The backend-api pods are in [error]CrashLoopBackOff[/error]. "
        "Their logs all end with:\n"
        "\n"
        "  [error]ERROR: database connection refused[/error]\n"
        "\n"
        "There are no services currently defined in the staging namespace. This likely\n"
        "means the database service is missing, which is causing the backend API\n"
        "deployment to fail with \"database connection refused\" errors."
    ), 0.45)

    emit_block(capture(
        renderer.render_status_footer,
        kube_context="kind-kubeintellect", namespace="staging",
        total_tokens=4821, cost="$0.011",
    ), 0.3)
    emit("\r\n", 1.1)

    # 4. Turn two — act. The safety gate fires here.
    emit(markup("[user]You:[/user] ").rstrip("\n"), 0.3)
    type_out("scale the backend-api deployment to 3 replicas")
    emit("\r\n", 0.2)
    spinner(1.5)

    emit_block(capture(
        renderer.render_hitl_panel,
        "kubectl scale deployment/backend-api --replicas=3 -n staging",
    ), 0.4)
    emit("\r\n", 0.4)

    # The HITL> prompt: nothing runs until a human types /approve.
    emit("\033[1;33mHITL> \033[0m", 0.5)
    emit("", 2.6)  # hold the final frame on the gate


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "kubeintellect-demo.cast"
    build()
    with open(out, "w") as fh:
        fh.write(json.dumps({
            "version": 2, "width": COLS, "height": ROWS,
            "timestamp": 1785000000, "idle_time_limit": 2.0,
            "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
            "title": "KubeIntellect — diagnose, then ask before acting",
        }) + "\n")
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    print(f"wrote {out}: {len(events)} events, {events[-1][0]:.1f}s")


if __name__ == "__main__":
    main()
