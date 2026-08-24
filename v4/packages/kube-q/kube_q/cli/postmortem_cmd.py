"""`kq postmortem <session-id>` — a grounded incident postmortem.

Renders the server's flight-recorder postmortem: a seq-cited timeline, what
fired, what was investigated and tried, the outcome, and an audit-chain verdict.

Exit codes follow the same convention as `kq replay` and `kq export`, because all three
render the same verdict: 0 intact and complete · 1 fetch failed · 2 usage · 3 chain BROKEN ·
4 chain NOT VERIFIED · 5 intact but INCOMPLETE. This command used to return 0 in every one of
those cases, so `kq postmortem X > report.md && publish` could not tell a tamper-evident
report from a broken one.
"""
from __future__ import annotations

import sys
import uuid

from rich.console import Console
from rich.markdown import Markdown

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, explain, make_client


def _verdict_exit(body: dict, console: Console) -> int:
    """Map the postmortem's audit-chain verdict to `kq replay`'s exit codes.

    The banners are already in the rendered markdown above; these lines exist so the *reason*
    for a non-zero exit is visible next to the code, the way `kq replay` prints it. A server
    that predates the verdict fields sends neither `chain_verified` nor `chain_valid`, which is
    exactly the "not verified" case — fail closed on 4 rather than assume the older default.
    """
    if not body.get("chain_verified", False):
        console.print(
            "[red]✗ chain NOT VERIFIED[/red] — no records were read, so this is neither a "
            "statement that they are intact nor that they were altered."
        )
        return 4
    if not body.get("chain_valid", False):
        console.print(
            "[red]✗ CHAIN BROKEN[/red] — the recorded events may have been altered or "
            "truncated. Do not treat this postmortem as tamper-evident."
        )
        return 3
    lost = int(body.get("events_lost") or 0)
    if lost:
        gaps = body.get("gaps") or []
        console.print(
            f"[yellow]⚠ EPISODE INCOMPLETE[/yellow] — {lost} event(s) were never written "
            f"across {len(gaps)} gap(s). The timeline above is unaltered but it is not the "
            "whole sequence."
        )
        return 5
    return 0


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    episode_id = argv[0]

    cfg = load_config()
    console = Console()
    url = f"{cfg.url.rstrip('/')}/v1/episodes/{episode_id}/postmortem"
    headers = build_headers(cfg.api_key, "postmortem", str(uuid.uuid4()))
    try:
        with make_client(None, timeout=cfg.timeout) as client:
            response = client.get(url, headers=headers, params={"format": "markdown"})
            response.raise_for_status()
            body = response.json()
            console.print(Markdown(body.get("markdown", "")))
            return _verdict_exit(body, console)
    except Exception as exc:
        console.print(f"[red]Postmortem fetch failed:[/red] {explain(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
