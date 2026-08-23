"""`kq findings` — list recent detector firings from the sensorium.

Findings are produced with zero LLM tokens by the compiled detector engine
(playbook `detect:` predicates evaluated against the watch stream).

Usage:
  kq findings [--limit N]
"""
from __future__ import annotations

import sys
import time
import uuid

from rich.console import Console
from rich.table import Table

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, explain, make_client


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    limit = 100
    if len(argv) == 2 and argv[0] == "--limit":
        try:
            limit = int(argv[1])
        except ValueError:
            print(__doc__)
            return 2
    elif argv:
        print(__doc__)
        return 2

    cfg = load_config()
    console = Console()
    url = f"{cfg.url.rstrip('/')}/v1/findings"
    headers = build_headers(cfg.api_key, "findings", str(uuid.uuid4()))

    try:
        with make_client(None, timeout=cfg.timeout) as client:
            response = client.get(url, headers=headers, params={"limit": limit})
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        console.print(f"[red]Findings fetch failed:[/red] {explain(exc)}")
        return 1

    state = data.get("sensorium")
    if state == "disabled":
        console.print("[yellow]Sensorium is disabled on this server.[/yellow]")
        return 0
    if state != "active":
        # No watch stream is connected, so an empty list is "not watching", not
        # "nothing wrong". Never print the reassuring green line here.
        console.print(
            f"[red]Sensorium is not watching[/red] (state: {state}) — "
            "an empty result here does NOT mean the cluster is healthy."
        )
        for st in data.get("streams", []):
            mark = "stopped" if st.get("stopped") else "reconnecting"
            console.print(
                f"  [yellow]•[/yellow] {st.get('name', '?')}: {mark}"
                f" — {st.get('last_error') or 'no reason recorded'}"
            )
        if not data.get("streams"):
            console.print("  [yellow]•[/yellow] no watch streams have started yet")

    # `predictive` is an independent claim from `sensorium`: the watch stream can be
    # connected while Prometheus is unreachable, in which case no anticipatory
    # (ADR-010) detector *could* have fired. Blind is not clear.
    predictive = data.get("predictive")
    if predictive == "blind":
        console.print(
            "[red]Predictive detection is blind[/red] — Prometheus could not be queried, "
            "so no predicted finding could have fired."
        )
        console.print(
            f"  [yellow]•[/yellow] {data.get('predictive_error') or 'no reason recorded'}"
        )

    findings = data.get("findings", [])
    if not findings:
        if state == "active" and predictive != "blind":
            console.print(
                f"[green]No findings[/green] · {data.get('detectors', '?')} detectors watching"
            )
        elif state == "active":
            # Watching, but half-blind — say what is and is not covered.
            console.print(
                f"[yellow]No findings[/yellow] · {data.get('detectors', '?')} detectors watching, "
                "but predictive detection is blind — this is not an all-clear."
            )
        return 0

    table = Table(title=f"Findings ({len(findings)})")
    table.add_column("fired", style="dim")
    table.add_column("playbook", style="cyan")
    table.add_column("namespace")
    table.add_column("object")
    table.add_column("evidence")
    for f in findings:
        fired = time.strftime("%H:%M:%S", time.localtime(f.get("fired_at", 0)))
        evidence = str(f.get("evidence", ""))[:70]
        # Predicted (anticipatory, ADR-010) findings are flagged inline so the
        # table keeps its column layout.
        if f.get("severity") == "predicted":
            eta = f.get("eta_minutes")
            eta_txt = f" ~{eta:.0f}m" if eta is not None else ""
            evidence = f"[magenta]⚠ predicted{eta_txt}[/magenta] " + evidence
        table.add_row(
            fired,
            f.get("playbook", "?"),
            f.get("namespace", ""),
            f.get("object", ""),
            evidence,
        )
    console.print(table)
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
