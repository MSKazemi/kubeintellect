"""`kq preference` — view and manage the agent's memory of your preferences.

KubeIntellect remembers how you like to operate (explicit + behaviour-inferred)
and recalls it across sessions. This command manages the explicit ones.

Usage:
  kq preference list [--user U]
  kq preference set <key> <value> [--user U]
  kq preference forget <key> [--user U]

Examples:
  kq preference set default_namespace payments
  kq preference set remediation dry-run-first
  kq preference list
"""
from __future__ import annotations

import sys
import uuid

from rich.console import Console
from rich.table import Table

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, explain, make_client


def _peek_user(argv: list[str]) -> tuple[list[str], str]:
    """Pull an optional --user U out of argv; return (remaining, user)."""
    user = "default"
    out: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--user" and i + 1 < len(argv):
            user = argv[i + 1]
            i += 2
        elif argv[i].startswith("--user="):
            user = argv[i].split("=", 1)[1]
            i += 1
        else:
            out.append(argv[i])
            i += 1
    return out, user


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2

    argv, user = _peek_user(argv)
    sub, rest = argv[0], argv[1:]
    cfg = load_config()
    console = Console()
    base = f"{cfg.url.rstrip('/')}/v1/preferences"
    headers = build_headers(cfg.api_key, "preferences", str(uuid.uuid4()))

    try:
        with make_client(None, timeout=cfg.timeout) as client:
            if sub == "list":
                r = client.get(base, headers=headers, params={"user": user})
                r.raise_for_status()
                return _print_list(console, r.json())
            if sub == "set":
                if len(rest) < 2:
                    print(__doc__)
                    return 2
                key, value = rest[0], " ".join(rest[1:])
                r = client.put(base, headers=headers,
                               json={"user": user, "key": key, "value": value})
                r.raise_for_status()
                console.print(f"[green]remembered[/green] {key} = {value}")
                return 0
            if sub == "forget":
                if len(rest) != 1:
                    print(__doc__)
                    return 2
                r = client.delete(f"{base}/{rest[0]}", headers=headers, params={"user": user})
                r.raise_for_status()
                console.print(f"[green]forgotten[/green] {rest[0]}")
                return 0
    except Exception as exc:
        console.print(f"[red]preference command failed:[/red] {explain(exc)}")
        return 1

    print(__doc__)
    return 2


def _print_list(console: Console, data: dict) -> int:
    prefs = data.get("preferences", [])
    if not prefs:
        console.print(f"[dim]No preferences remembered for user '{data.get('user')}'.[/dim]")
        return 0
    table = Table(title=f"Operator preferences · {data.get('user')}")
    table.add_column("key", style="cyan")
    table.add_column("value")
    table.add_column("source", style="dim")
    table.add_column("confidence", justify="right")
    for p in prefs:
        src = p.get("source", "explicit")
        conf = "" if src == "explicit" else f"{float(p.get('confidence', 0)):.0%}"
        table.add_row(p.get("key", ""), str(p.get("value", "")), src, conf)
    console.print(table)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
