"""`kq detector` — teach the operator a new failure pattern in plain English (ADR-012).

  kq detector new "<plain-English failure description>"   compile + stage as shadow
  kq detector list [--status shadow|active|demoted]        the candidate queue
  kq detector shadow <name>                                what a shadow detector fired
  kq detector promote <name>                               shadow -> active (it can now act)
  kq detector reject <name>                                stop it firing

New detectors enter SHADOW mode: they observe and accrue precision but never act
until you promote them.

Exit codes:
  0  the operation succeeded (for `new`: the detector was staged in shadow)
  1  the request failed
  2  usage error
  3  the detector was REJECTED on its merits and nothing changed. Two ways to get it:
     `new` — the description would not compile into a stageable detector (the server
     answers 200 with staged=false and the compile errors, so the exit code is the only
     machine-readable signal that no detector was created); `promote`/`reject` — the
     server answered 409 because the predicate can never match an observation. Distinct
     from 1 on purpose: 1 is worth retrying and 3 never is.
"""
from __future__ import annotations

import json
import sys
import uuid

from rich.console import Console
from rich.table import Table

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, explain, make_client, server_detail


def _detail(response) -> str:
    """The server's own explanation for an error response, or its status line."""
    return server_detail(response) or f"HTTP {response.status_code}"


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
                staged = bool(data.get("staged"))
                if staged:
                    console.print(
                        f"[green]Staged shadow detector[/green] [cyan]{data['name']}[/cyan]"
                    )
                else:
                    console.print("[yellow]Not staged.[/yellow]")
                    for err in data.get("errors", []):
                        console.print(f"  [red]·[/red] {err}")
                console.print_json(json.dumps(data.get("compiled", {})))
                # A rejected description is a 200 from the server (it compiled *something*, it just
                # would not stage it), so the exit code is the only machine-readable signal that no
                # detector was created. Returning 0 here told `kq detector new … && …` that a
                # detector exists when none does — and the whole point of NL authoring is that the
                # author cannot read the compiled predicate to check.
                return 0 if staged else 3

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
                body = r.json()
                found = body.get("findings", [])
                # This count is what a reviewer promotes or rejects on, so it must never be
                # printed bare when it is not a measurement. The server distinguishes "not
                # loaded here" and "the ring overflowed" from "ran and stayed quiet"; printing
                # only the number collapsed all of them back into one line.
                caveats = []
                if body.get("watching") is False:
                    caveats.append("this process has not loaded it as a shadow detector")
                if (body.get("buffer") or {}).get("saturated"):
                    cap = body["buffer"].get("capacity")
                    caveats.append(
                        f"the {cap}-firing buffer is full, so older firings were dropped")
                console.print(f"[cyan]{rest[0]}[/cyan]: {len(found)} shadow firing(s)")
                if caveats:
                    console.print(
                        f"  [yellow]⚠[/yellow] not a measurement of precision — "
                        f"{' and '.join(caveats)}.")
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
                if r.status_code == 409:
                    # The server refuses to flip the row because the compiled predicate can
                    # never match an observation — the failure mode this project actually
                    # shipped once, where a stray space in an alternation made a detector a
                    # permanent no-op. It is the same *kind* of answer as `new`'s rejection:
                    # understood, refused on the merits, nothing changed. That is exit 3.
                    # It was exit 1, "the request failed", indistinguishable from the store
                    # being down — so a script's retry loop would retry a dead detector forever.
                    console.print(
                        f"[red]✗ {rest[0]} was NOT {action}d[/red] — {_detail(r)}")
                    console.print(
                        "[dim]Nothing changed. The detector was rejected on its merits, not the "
                        "request — retrying will not help.[/dim]")
                    return 3
                if r.status_code == 404:
                    # Says which thing is missing. "Detector command failed" reads as our side.
                    console.print(f"[red]No detector named '{rest[0]}'.[/red]")
                    return 1
                r.raise_for_status()
                console.print(f"[green]{rest[0]} → {r.json().get('status')}[/green]")
                return 0

            print(__doc__)
            return 2
    except Exception as exc:
        console.print(f"[red]Detector command failed:[/red] {explain(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
