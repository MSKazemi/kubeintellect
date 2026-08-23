"""Machine-checkable postconditions (v5 P3 TNR verification rung; A-CH-02-08).

A mitigation is only "successful" if a health ORACLE confirms it — the SREGym-style postcondition
that turns "I ran a fix" into "the fix worked". Read-only checks over live cluster state (safe:
never mutate), so this is the machine-checkable rung of the verification ladder that gates whether
a transactional mitigation commits or rolls back.

v0 covers deployment readiness (the most common post-fix oracle). The text parser is pure; the
cluster read goes through the run_kubectl seam and is injectable for tests.

**"Not ready" and "I could not look" are two different answers**, and this oracle used to give
the first one for both. The read goes through `run_kubectl`, which returns a string and discards
the exit code, so a refused read, an unreachable API server, or a missing kubectl binary all
arrive as text that contains no READY column — and the parser found no row, and the oracle
reported `met=False`. Measured 2026-08-20 with the real `run_kubectl`: a read of a protected
namespace answers `[Protected] Access to namespace 'kube-system' is not permitted`, and the oracle
turned that into *"deployment 'web' not found in 'prod'"* — a verdict about a namespace it never
looked at. Downstream, `execute_transactional` reads `met=False` as a failed mitigation and
**rolls back**, so an instrument outage became a live mutation. Hence `evaluated`: the oracle now
says when it has no observation at all, and the caller escalates instead of rolling back.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.tools.aci import kubectl_output as _out


@dataclass(frozen=True)
class PostconditionResult:
    met: bool
    ready: int
    desired: int
    detail: str = ""
    # False ⇒ the oracle never got an observation (refused read, unreachable cluster, no kubectl).
    # `met` is then meaningless and must not be read as "the mitigation failed".
    evaluated: bool = True


def parse_ready_column(get_output: str, name: str) -> tuple[int, int] | None:
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
        return PostconditionResult(False, 0, 0, f"read error: {exc}", evaluated=False)
    verdict = _out.classify_output(out)
    if verdict != _out.OK:
        first = next((ln.strip() for ln in out.splitlines() if ln.strip()), "(empty)")
        return PostconditionResult(
            False, 0, 0, f"could not read deployment {name!r} in {namespace!r}: {first[:200]}",
            evaluated=False,
        )
    parsed = parse_ready_column(out, name)
    if parsed is None:
        # The read succeeded and the row is not in it. That is an observation, not a blind spot.
        return PostconditionResult(False, 0, 0, f"deployment {name!r} not found in {namespace!r}")
    ready, desired = parsed
    met = desired > 0 and ready >= desired
    return PostconditionResult(met, ready, desired, f"{ready}/{desired} ready")
