"""`kq export <session-id> [--format json|yaml] [--output PATH]` — export a diagnosis report.

Serializes the server's grounded postmortem (ADR-011) for one episode to JSON or
YAML, for archiving, attaching to a ticket, or feeding another tool.

The exported document is the *same* structure `kq postmortem` renders — a view
over the hash-chained decision_log. Nothing is synthesized here: if the recorder
has no events for the episode, this command exports nothing and says so, rather
than emitting a plausible-looking empty report.

Exit codes:
  0  exported, audit chain intact and the episode is complete
  1  fetch or write failed
  2  usage error
  3  exported, but the audit chain is BROKEN (matches `kq replay`)
  4  no recorded events for that episode — nothing exported
  5  exported, chain intact, but the episode is INCOMPLETE — the recorder lost events
     and recorded the loss (`recorder_gap`). Matches `kq replay`.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

from rich.console import Console

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, explain, make_client

_FORMATS = ("json", "yaml")


def _parse(argv: list[str]) -> tuple[str, str, str | None] | None:
    """Parse ``<session-id> [--format F] [--output P]``; None on a usage error."""
    episode_id: str | None = None
    fmt = "json"
    output: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--format", "-f"):
            if i + 1 >= len(argv) or argv[i + 1] not in _FORMATS:
                return None
            fmt = argv[i + 1]
            i += 2
        elif arg in ("--output", "-o"):
            if i + 1 >= len(argv):
                return None
            output = argv[i + 1]
            i += 2
        elif arg.startswith("-"):
            return None
        elif episode_id is None:
            episode_id = arg
            i += 1
        else:
            return None  # a second positional is always a mistake

    if not episode_id:
        return None
    return episode_id, fmt, output


def _serialize(report: dict, fmt: str) -> str:
    if fmt == "yaml":
        import yaml

        return yaml.safe_dump(report, sort_keys=False, default_flow_style=False)
    return json.dumps(report, indent=2, sort_keys=False, default=str)


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2

    parsed = _parse(argv)
    if parsed is None:
        print(__doc__)
        return 2
    episode_id, fmt, output = parsed

    cfg = load_config()
    console = Console()
    err = Console(stderr=True)
    url = f"{cfg.url.rstrip('/')}/v1/episodes/{episode_id}/postmortem"
    headers = build_headers(cfg.api_key, "export", str(uuid.uuid4()))

    try:
        with make_client(None, timeout=cfg.timeout) as client:
            response = client.get(url, headers=headers, params={"format": "json"})
            response.raise_for_status()
            report = response.json()
    except Exception as exc:
        err.print(f"[red]Export failed:[/red] {explain(exc)}")
        return 1

    # An episode with no recorded events yields a well-formed but empty postmortem.
    # Exporting that as though it were a report is how a fabricated-looking
    # artifact gets into a ticket — refuse instead.
    if not report.get("timeline"):
        err.print(
            f"[yellow]No recorded events for episode[/yellow] '{episode_id}' — nothing exported.\n"
            "Check the id with [bold]kq findings[/bold], or confirm the flight recorder is enabled."
        )
        return 4

    try:
        content = _serialize(report, fmt)
    except Exception as exc:  # pragma: no cover — serializer failure is not reachable in practice
        err.print(f"[red]Could not serialize the report as {fmt}:[/red] {exc}")
        return 1

    if output:
        try:
            out_path = Path(output).expanduser()
            if out_path.parent != Path(""):
                out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            err.print(f"[red]Could not write {output}:[/red] {exc}")
            return 1
        events = len(report.get("timeline", []))
        console.print(f"[green]✓[/green] Exported {events} recorded event(s) to {out_path}")
    else:
        # stdout stays machine-parseable — never route it through rich markup.
        sys.stdout.write(content if content.endswith("\n") else content + "\n")

    if not report.get("chain_valid"):
        err.print(
            "[red]⚠ AUDIT CHAIN BROKEN[/red] — the recorded events for this episode may "
            "have been altered. Treat this export as untrusted."
        )
        return 3
    lost = int(report.get("events_lost") or 0)
    if lost:
        # Unaltered is not the same as whole. An export that reads as complete when the
        # recorder dropped events is the failure this exit code exists to prevent.
        err.print(
            f"[yellow]⚠ EPISODE INCOMPLETE[/yellow] — {lost} event(s) were never written. "
            "Nothing exported here was altered, but this is not the full sequence."
        )
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
