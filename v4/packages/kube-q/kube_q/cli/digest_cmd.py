"""`kq digest [--hours N]` — what the cluster organism did while you were away.

Renders the server's flight-recorder digest: detector findings, autonomous
investigations, rollback points.
"""
from __future__ import annotations

import sys
import uuid

from rich.console import Console
from rich.markdown import Markdown

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, make_client


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    hours = 24.0
    if len(argv) == 2 and argv[0] == "--hours":
        try:
            hours = float(argv[1])
        except ValueError:
            print(__doc__)
            return 2
    elif argv:
        print(__doc__)
        return 2

    cfg = load_config()
    console = Console()
    url = f"{cfg.url.rstrip('/')}/v1/digest"
    headers = build_headers(cfg.api_key, "digest", str(uuid.uuid4()))
    try:
        with make_client(None, timeout=cfg.timeout) as client:
            response = client.get(
                url, headers=headers, params={"hours": hours, "format": "markdown"}
            )
            response.raise_for_status()
            console.print(Markdown(response.json().get("markdown", "")))
            return 0
    except Exception as exc:
        console.print(f"[red]Digest fetch failed:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
