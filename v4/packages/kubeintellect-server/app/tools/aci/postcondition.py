"""Machine-checkable postconditions (v5 P3 TNR verification rung; A-CH-02-08).

A mitigation is only "successful" if a health ORACLE confirms it — the SREGym-style postcondition
that turns "I ran a fix" into "the fix worked". Read-only checks over live cluster state (safe:
never mutate), so this is the machine-checkable rung of the verification ladder that gates whether
a transactional mitigation commits or rolls back.

v0 covers deployment readiness (the most common post-fix oracle). The text parser is pure; the
cluster read goes through the run_kubectl seam and is injectable for tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PostconditionResult:
    met: bool
    ready: int
    desired: int
    detail: str = ""


def parse_ready_column(get_output: str, name: str) -> Optional[tuple[int, int]]:
    """Parse (ready, desired) from a `kubectl get deployment` table row for ``name``.

    The READY column is "ready/desired" (e.g. "3/3"). Returns None if the row/column is absent.
    """
    for line in get_output.splitlines():
        cols = line.split()
        if len(cols) >= 2 and cols[0] == name and "/" in cols[1]:
            r, _, d = cols[1].partition("/")
            if r.isdigit() and d.isdigit():
                return int(r), int(d)
    return None


def deployment_ready(name: str, namespace: str, *, _runner=None) -> PostconditionResult:
    """Health oracle: is deployment ``name`` fully ready (readyReplicas == desired, desired > 0)?"""
    if _runner is None:
        from app.tools.kubectl_tool import run_kubectl
        def _runner(cmd: str) -> str:
            return run_kubectl.invoke({"command": cmd})
    try:
        out = _runner(f"get deployment {name} -n {namespace}")
    except Exception as exc:
        return PostconditionResult(False, 0, 0, f"read error: {exc}")
    parsed = parse_ready_column(out, name)
    if parsed is None:
        return PostconditionResult(False, 0, 0, f"deployment {name!r} not found in {namespace!r}")
    ready, desired = parsed
    met = desired > 0 and ready >= desired
    return PostconditionResult(met, ready, desired, f"{ready}/{desired} ready")
