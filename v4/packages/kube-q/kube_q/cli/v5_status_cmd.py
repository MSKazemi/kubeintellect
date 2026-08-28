"""`kq v5-status` — show the v5 trust-plane state (version, active flags, safety brakes).

Reads GET /v1/v5/status: which v5 slices are active, and whether the fail-closed brakes
(kill switch, change freeze) are engaged. Zero LLM tokens.

Also surfaces `set_but_unwired_flags` when non-empty: flags you turned on that no code reads, so
a setting with no effect is visible instead of being quietly absent from `active_flags`;
`degraded_experimental_flags`, the same outcome one level out — the code does read the flag, but
the subsystem it lives in is not running; and `unenforceable_guard_config`, the same idea for the
guard settings — a blocked-namespace entry that cannot match any namespace, or an autonomy
override the parser drops; and `autonomy_promotion`, the ADR-102 brake on the autonomous-write
path, whose row separates "the flag is on" from "the brake can actually act".

Usage:
  kq v5-status
"""
from __future__ import annotations

import uuid

from rich.console import Console
from rich.table import Table

from kube_q.core.config import load_config
from kube_q.core.transport import build_headers, explain, make_client

#: How `memory.chain.state` reads to an operator. The server's five states exist because none of
#: the four non-`intact` ones means "fine", and a CLI that collapsed them back into a tick or a
#: cross would undo the distinction the server was changed to make (2026-08-28).
_CHAIN_ROWS: dict[str, tuple[str, str]] = {
    "TAMPERED": (
        "red",
        "the stored memory-audit rows no longer hash to what they carry, or the chain is "
        "shorter than its own head anchor — treat memory-derived answers as untrusted",
    ),
    "unverified": (
        "yellow",
        "a check ran and could not conclude (the audit rows or the anchor were unreadable). "
        "This is NOT a tamper signal — nobody looked is not evidence of anything",
    ),
    "never-checked": (
        "yellow",
        "hardening is on and no verification has completed yet — nothing has asked this chain",
    ),
    "off": (
        "dim",
        "MEMORY_SECURITY_HARDENING is off, so no audit chain is being written and there is "
        "nothing to verify. Not a clean bill of health",
    ),
    "intact": ("green", "verified — nothing contradicted the stored memory-audit rows"),
}


def _add_chain_row(table: Table, chain: dict) -> None:
    """Render the memory audit chain's last recorded verdict, and how old it is.

    Always rendered when the server sent one, including `off` and `intact`. A tamper surface
    that appears only when it has something bad to say is one an operator cannot use to confirm
    anything: the absence of a row is indistinguishable from an older server that never had it.

    The age is not decoration. The server reports the LAST RECORDED verdict, never a fresh one —
    so `intact` without an age would read as "checked just now" when it may be a verdict from a
    verifier that stopped hours ago. `stale` says exactly that, and it is shown in red because a
    stopped verifier looks identical to one that keeps agreeing with itself.
    """
    state = str(chain.get("state") or "")
    if not state:
        return
    colour, note = _CHAIN_ROWS.get(state, ("yellow", "unrecognised state"))
    age = chain.get("age_s")
    when = "never checked" if age is None else f"last checked {float(age):.0f}s ago"
    if chain.get("stale"):
        when = f"[red]{when} — STALE, the verifier may have stopped[/red]"
    label = "memory_chain" if colour in ("green", "dim") else "[red]memory_chain[/red]"
    table.add_row(
        label,
        f"[{colour}]{state}[/{colour}] — {when}\n[dim]{note}[/dim]",
    )


def run(argv: list[str]) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv:
        print(__doc__)
        return 2

    cfg = load_config()
    console = Console()
    url = f"{cfg.url.rstrip('/')}/v1/v5/status"
    headers = build_headers(cfg.api_key, "v5-status", str(uuid.uuid4()))

    try:
        with make_client(None, timeout=cfg.timeout) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001 — surface any transport/HTTP error to the user
        console.print(f"[red]v5-status error: {explain(exc)}[/red]")
        return 1

    table = Table(title="KubeIntellect v5 — trust plane")
    table.add_column("field", style="cyan")
    table.add_column("value")
    table.add_row("arm", str(data.get("arm")))
    table.add_row("version", str(data.get("version")))
    table.add_row("cortex_v5_enabled", str(data.get("cortex_v5_enabled")))
    kill = data.get("kill_switch_engaged")
    table.add_row("kill_switch_engaged", f"[red]{kill}[/red]" if kill else str(kill))
    table.add_row("change_freeze", str(data.get("change_freeze")))
    table.add_row("spend_cap_usd", str(data.get("spend_cap_usd")))
    flags = data.get("active_flags") or []
    table.add_row("active_flags", "\n".join(flags) if flags else "(none — v4 baseline)")
    # Flags the operator set that no code reads. Rendered only when non-empty, and in red, because
    # the whole point is that `active_flags` above will NOT mention them: without this row the
    # operator sets a switch, sees "(none — v4 baseline)", and learns nothing. The server stopped
    # reporting these as active on 2026-08-19; a CLI that then dropped the field would just move
    # the same misinformation from a wrong answer to a missing one.
    unwired = data.get("set_but_unwired_flags") or []
    if unwired:
        table.add_row(
            "[red]set_but_unwired_flags[/red]",
            "[red]" + "\n".join(unwired) + "[/red]\n"
            "[dim]declared but read by no code — these settings have no effect[/dim]",
        )
    # Flags that ARE wired and still do nothing, because their subsystem is down. These stay in
    # `active_flags` above on purpose (that list is rollout identity and must not flap), so
    # without this row the two facts never meet: the operator reads "active" and is wrong.
    degraded = data.get("degraded_experimental_flags") or []
    if degraded:
        memory = data.get("memory") or {}
        why = memory.get("reason") or "no reason recorded"
        state = memory.get("state") or "unknown"
        table.add_row(
            "[red]degraded_experimental_flags[/red]",
            "[red]" + "\n".join(degraded) + "[/red]\n"
            f"[dim]read by code, but the memory hierarchy is {state} ({why}) — "
            "these settings change nothing while it is down[/dim]",
        )
    # The hierarchy's own state, always shown: it is what `degraded_experimental_flags` above
    # means, and a healthy row is the only thing that makes an empty degraded list evidence.
    memory = data.get("memory") or {}
    if memory:
        state = str(memory.get("state", "unknown"))
        dropped = memory.get("observations_dropped", 0)
        if memory.get("enabled"):
            table.add_row("memory", f"{state}")
        else:
            table.add_row(
                "[red]memory[/red]",
                f"[red]{state}[/red] — {memory.get('reason') or 'no reason recorded'}\n"
                f"[dim]{dropped} observation(s) dropped since this process started[/dim]",
            )
        _add_chain_row(table, memory.get("chain") or {})
    # Guard settings that parse cleanly and protect nothing — the same idea one level down.
    # `KUBECTL_BLOCKED_NAMESPACES` is the security control an operator is most likely to get
    # subtly wrong (a glob, a slash), and every parser for it discards silently.
    guard = data.get("unenforceable_guard_config") or []
    if guard:
        table.add_row(
            "[red]unenforceable_guard_config[/red]",
            "[red]" + "\n".join(guard) + "[/red]\n[dim]these guard entries are configured but "
            "cannot match anything — the protection you think you have is not in force[/dim]",
        )
    # The fourth brake on the autonomous-write path (ADR-102). Always shown when the flag is on,
    # because the two failure modes are quiet ones: a flag named *statistical promotion* reported
    # under `active_flags` reads as "rungs are being earned here" — the direction row says
    # otherwise — and a deployment that set the flag without an outcome store has the brake
    # enabled and not operating, which is a brake reported as on that is not in the write path.
    promotion = data.get("autonomy_promotion") or {}
    if promotion.get("enabled"):
        revoked = promotion.get("authority_revoked")
        operating = promotion.get("operating")
        reason = promotion.get("reason") or "no reason recorded"
        if not operating:
            table.add_row(
                "[red]autonomy_promotion[/red]",
                f"[red]enabled but NOT operating[/red] — {reason}\n"
                "[dim]A3 auto-fix is governed by the allowlist alone[/dim]",
            )
        elif revoked:
            table.add_row(
                "[red]autonomy_promotion[/red]",
                f"[red]A3 auto-fix REVOKED[/red] for {promotion.get('action_class')}\n"
                f"[dim]{reason} — {promotion.get('samples')} sample(s) in the window[/dim]",
            )
        else:
            table.add_row(
                "autonomy_promotion",
                f"holding ({promotion.get('samples')} sample(s) in the window)\n"
                f"[dim]revoke-only: this brake can close the A3 gate, never open it[/dim]",
            )
    console.print(table)
    return 0
