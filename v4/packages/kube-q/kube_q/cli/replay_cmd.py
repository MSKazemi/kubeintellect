"""`kq replay <episode_id>` — replay a recorded episode from the flight recorder.

Fetches GET /v1/episodes/{id}/replay (KubeIntellect's durable, hash-chained
decision log) and renders the event sequence with a chain-integrity verdict.

Usage:
  kq replay <episode_id>          # episode_id == session_id (see /id in the REPL)
"""
from __future__ import annotations

import sys
import uuid

from rich.console import Console
from rich.table import Table

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, iter_sse, make_client

_SUMMARY_FIELDS = ("message", "command", "tool", "output", "error", "risk_level", "phase")


def _summarise(event: dict) -> str:
    parts = []
    for field in _SUMMARY_FIELDS:
        value = event.get(field)
        if value:
            text = str(value).replace("\n", " ")
            parts.append(f"{field}={text[:60]}")
    return "  ".join(parts)


def run(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv and argv[0] in ("-h", "--help") else 2

    episode_id = argv[0]
    cfg = load_config()
    console = Console()

    url = f"{cfg.url.rstrip('/')}/v1/episodes/{episode_id}/replay"
    headers = build_headers(cfg.api_key, episode_id, str(uuid.uuid4()))

    try:
        with make_client(None, timeout=cfg.timeout) as client:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code == 404:
                    console.print(f"[red]No recorded episode '{episode_id}'.[/red]")
                    return 1
                response.raise_for_status()

                meta: dict | None = None
                table = Table(title=f"Episode {episode_id}")
                table.add_column("#", justify="right", style="dim")
                table.add_column("type", style="cyan")
                table.add_column("summary")

                seq = 0
                for event in iter_sse(response):
                    if event.get("type") == "replay_meta":
                        meta = event
                        continue
                    table.add_row(str(seq), event.get("type", "?"), _summarise(event))
                    seq += 1

                console.print(table)
                if meta is not None:
                    verdict = (
                        "[green]✓ chain intact[/green]"
                        if meta.get("chain_valid")
                        else "[red]✗ CHAIN BROKEN — records may have been tampered with[/red]"
                    )
                    console.print(f"{meta.get('records', seq)} records · {verdict}")
                return 0 if (meta is None or meta.get("chain_valid")) else 3
    except Exception as exc:
        console.print(f"[red]Replay failed:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
