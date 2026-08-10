"""`kq detector` — teach the operator a new failure pattern in plain English (ADR-012).

  kq detector new "<plain-English failure description>"   compile + stage as shadow
  kq detector list [--status shadow|active|demoted]        the candidate queue
  kq detector shadow <name>                                what a shadow detector fired
  kq detector promote <name>                               shadow -> active (it can now act)
  kq detector reject <name>                                stop it firing

New detectors enter SHADOW mode: they observe and accrue precision but never act
until you promote them.
"""
from __future__ import annotations

import json
import sys
import uuid

from rich.console import Console
from rich.table import Table

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, make_client


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2

    sub = argv[0]
    rest = argv[1:]
    cfg = load_config()
    console = Console()
    base = f"{cfg.url.rstrip('/')}/v1/detectors"
    headers = build_headers(cfg.api_key, "detector", str(uuid.uuid4()))

    try:
        with make_client(None, timeout=cfg.timeout) as client:
            if sub == "new":
                if not rest:
                    print(__doc__)
                    return 2
                description = " ".join(rest)
                r = client.post(base, headers=headers, json={"description": description})
                r.raise_for_status()
                data = r.json()
                if data.get("staged"):
                    console.print(
                        f"[green]Staged shadow detector[/green] [cyan]{data['name']}[/cyan]"
                    )
                else:
                    console.print("[yellow]Not staged.[/yellow]")
                    for err in data.get("errors", []):
                        console.print(f"  [red]·[/red] {err}")
                console.print_json(json.dumps(data.get("compiled", {})))
                return 0

            if sub == "list":
                status = rest[1] if len(rest) == 2 and rest[0] == "--status" else None
                params = {"status": status} if status else {}
                r = client.get(base, headers=headers, params=params)
                r.raise_for_status()
                dets = r.json().get("detectors", [])
                if not dets:
                    console.print("[dim]No detectors.[/dim]")
                    return 0
                table = Table(title=f"Detectors ({len(dets)})")
                for col in ("name", "source", "status", "reviewed_by", "created_from"):
                    table.add_column(col)
                for d in dets:
                    table.add_row(
                        d.get("name", ""), d.get("source", ""), d.get("status", ""),
                        d.get("reviewed_by") or "", str(d.get("created_from") or "")[:40],
                    )
                console.print(table)
                return 0

            if sub == "shadow":
                if not rest:
                    print(__doc__)
                    return 2
                r = client.get(f"{base}/{rest[0]}/shadow-findings", headers=headers)
                r.raise_for_status()
                found = r.json().get("findings", [])
                console.print(f"[cyan]{rest[0]}[/cyan]: {len(found)} shadow firing(s)")
                for f in found[-20:]:
                    where = f"{f.get('namespace')}/{f.get('object')}"
                    console.print(f"  · {where} — {f.get('evidence', '')[:70]}")
                return 0

            if sub in ("promote", "reject"):
                if not rest:
                    print(__doc__)
                    return 2
                action = "promote" if sub == "promote" else "demote"
                r = client.post(f"{base}/{rest[0]}/{action}", headers=headers)
                r.raise_for_status()
                console.print(f"[green]{rest[0]} → {r.json().get('status')}[/green]")
                return 0

            print(__doc__)
            return 2
    except Exception as exc:
        console.print(f"[red]Detector command failed:[/red] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
