"""`kq replay <episode_id>` — replay a recorded episode from the flight recorder.

Fetches GET /v1/episodes/{id}/replay (KubeIntellect's durable, hash-chained
decision log) and renders the event sequence with a chain-integrity verdict.

Usage:
  kq replay <episode_id>          # episode_id == session_id (see /id in the REPL)

Exit codes:
  0  rendered; chain intact and complete
  1  no such episode, or the request failed
  3  rendered; chain BROKEN — records may have been tampered with
  4  rendered; chain NOT VERIFIED — the integrity verdict never arrived (fail-closed:
     unverified is not the same as intact, and must not be reported as success)
  5  rendered; chain intact but the episode is INCOMPLETE — the recorder lost events and
     said so in the chain (`recorder_gap`). Intact means nothing was altered; it never
     meant nothing is missing.
"""
from __future__ import annotations

import sys
import uuid

from rich.console import Console
from rich.table import Table

from ki_protocol.record import GAP_KIND, summarise_record

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, iter_sse, make_client

# `ki_protocol.record.GAP_KIND` — the recorder's own record of what it lost.
_GAP_TYPE = GAP_KIND


def _summarise(event: dict) -> str:
    """One line for one recorded row, using the *shared* summariser.

    This used to be a list of seven top-level field names. A `finding` payload shares none
    of them, and a `findings:<cluster>` episode is nothing but findings — so `kq replay`
    rendered a table of blank summaries for detector firings that had certainly fired.
    """
    return summarise_record(str(event.get("type", "")), event).replace("\n", " ")


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
                gaps: list[dict] = []
                for event in iter_sse(response):
                    if event.get("type") == "replay_meta":
                        meta = event
                        continue
                    if event.get("type") == _GAP_TYPE:
                        gaps.append(event)
                    table.add_row(str(seq), event.get("type", "?"), _summarise(event))
                    seq += 1

                console.print(table)
                if meta is None:
                    # FAIL CLOSED. The verdict is the whole point of this command: the flight
                    # recorder is hash-chained so tampering is *detectable*, and `replay` is how you
                    # detect it. The meta frame is normally the first thing the server sends, but
                    # `iter_sse` drops any frame it cannot parse (`except JSONDecodeError: pass`),
                    # and an older or proxied server may not send one at all — in which case we have
                    # rendered records we never verified. Returning 0 there told every wrapping
                    # script "chain intact" and printed nothing to suggest otherwise.
                    console.print(
                        "[red]✗ chain NOT VERIFIED[/red] — the integrity verdict was missing from "
                        "the stream. The records above are unverified; do not treat this as proof "
                        "they are untampered."
                    )
                    return 4
                verdict = (
                    "[green]✓ chain intact[/green]"
                    if meta.get("chain_valid")
                    else "[red]✗ CHAIN BROKEN — records may have been tampered with[/red]"
                )
                console.print(f"{meta.get('records', seq)} records · {verdict}")
                if not meta.get("chain_valid"):
                    return 3
                if gaps:
                    # An intact chain proves nothing was altered. It does not prove nothing
                    # is missing, and the recorder is fire-and-forget — so it writes its own
                    # losses into the chain. Reporting success here would turn a hole in the
                    # record into a clean bill of health.
                    lost = sum(int(g.get("dropped") or 0) for g in gaps)
                    console.print(
                        f"[yellow]⚠ EPISODE INCOMPLETE[/yellow] — {lost} event(s) were never "
                        f"written across {len(gaps)} gap(s). The records above are unaltered "
                        "but they are not the whole sequence."
                    )
                    for g in gaps:
                        console.print(f"  [dim]· {g.get('dropped', '?')} lost — "
                                      f"{g.get('reason', 'cause not recorded')}[/dim]")
                    return 5
                return 0
    except Exception as exc:
        console.print(f"[red]Replay failed:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
