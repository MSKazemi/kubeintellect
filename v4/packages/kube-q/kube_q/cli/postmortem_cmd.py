"""`kq postmortem <session-id>` — a grounded incident postmortem.

Renders the server's flight-recorder postmortem: a seq-cited timeline, what
fired, what was investigated and tried, the outcome, and an audit-chain verdict.
"""
from __future__ import annotations

import sys
import uuid

from rich.console import Console
from rich.markdown import Markdown

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, make_client


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
            console.print(Markdown(response.json().get("markdown", "")))
            return 0
    except Exception as exc:
        console.print(f"[red]Postmortem fetch failed:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
