"""Transactional, oracle-verified mitigation (v5 P3, A-CH-02-08 / STRATUS-style TNR).

Apply → verify a machine-checkable postcondition → **roll back automatically if the oracle fails**.
A mitigation either commits (postcondition holds) or leaves the cluster as it was (rolled back) —
never a half-applied change nobody verified. This is the executor the trust plane invokes AFTER a
mutation is authorized (chokepoint decision == "auto") and server-side dry-run validated.

The apply seam and the postcondition oracle are injectable, so the transaction logic is unit-
testable with no cluster; the live apply goes through run_kubectl with hitl_bypass (the autonomous
path — reachable only for chokepoint-authorized, reversible classes).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from app.tools.aci.postcondition import PostconditionResult

# apply(command) -> combined output. Injectable; default runs the real (auto-approved) mutation.
ApplyFn = Callable[[str], str]
# postcondition() -> the health-oracle verdict after the change.
PostconditionFn = Callable[[], PostconditionResult]

COMMITTED = "committed"
ROLLED_BACK = "rolled_back"
APPLY_FAILED = "apply_failed"
VERIFY_FAILED_NO_ROLLBACK = "verify_failed_no_rollback"

_ERR = ("error", "exit=1", "not found", "forbidden", "invalid")


def _ok(output: str) -> bool:
    low = output.lower()
    return not any(e in low for e in _ERR)


def _default_apply(command: str) -> str:
    from app.tools.kubectl_tool import run_kubectl
    # hitl_bypass: the autonomous execution path (only chokepoint-authorized commands reach here).
    return run_kubectl.invoke({"command": command}, config={"configurable": {"hitl_bypass": True}})


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    postcondition: Optional[PostconditionResult] = None
    apply_output: str = ""
    rollback_output: str = ""


def execute_transactional(
    command: str,
    postcondition: PostconditionFn,
    *,
    rollback_command: Optional[str] = None,
    apply_fn: Optional[ApplyFn] = None,
) -> ExecutionResult:
    """Apply ``command``, verify the ``postcondition`` oracle, roll back on failure.

    - apply fails ⇒ APPLY_FAILED (nothing to roll back).
    - postcondition holds ⇒ COMMITTED.
    - postcondition fails + rollback_command ⇒ run it ⇒ ROLLED_BACK.
    - postcondition fails + no rollback_command ⇒ VERIFY_FAILED_NO_ROLLBACK (escalate).
    """
    apply_fn = apply_fn or _default_apply
    out = apply_fn(command)
    if not _ok(out):
        return ExecutionResult(APPLY_FAILED, apply_output=out.strip()[:2000])

    verdict = postcondition()
    if verdict.met:
        return ExecutionResult(COMMITTED, verdict, out.strip()[:2000])

    if rollback_command is None:
        return ExecutionResult(VERIFY_FAILED_NO_ROLLBACK, verdict, out.strip()[:2000])
    rb = apply_fn(rollback_command)
    return ExecutionResult(ROLLED_BACK, verdict, out.strip()[:2000], rb.strip()[:2000])
