"""`rolled_back` is the one status that says no operator needs to look. It must be earned.

`execute_transactional` issued the rollback command and discarded its result: `return
ExecutionResult(ROLLED_BACK, …)` ran whatever came back. Measured 2026-08-24, a rollback that was

  * refused by KubeIntellect  (`[Protected] Access to namespace 'prod' is not permitted.`)
  * rejected by the API server (`[kubectl exited 1] Error from server (Forbidden): …`)
  * never delivered            (`[kubectl exited 1] The connection to the server … was refused`)
  * silent                     (`(no output)`)

all returned `rolled_back` — four ways to leave the cluster in the half-applied state this module
exists to prevent, while telling the caller and the audit trail that it had been restored.

The apply side already made these distinctions (`APPLY_REFUSED` vs `APPLY_FAILED`) and the verify
side already made them (`evaluated`); only the rollback, the last mutation and the least watched,
did not.
"""
from __future__ import annotations

import pytest
from app.tools.aci.postcondition import PostconditionResult
from app.tools.aci.transactional import (
    APPLY_REFUSED,
    COMMITTED,
    ROLLBACK_FAILED,
    ROLLBACK_REFUSED,
    ROLLBACK_UNCONFIRMED,
    ROLLED_BACK,
    VERIFY_FAILED_NO_ROLLBACK,
    VERIFY_INCONCLUSIVE,
    execute_transactional,
)

APPLY_OK = "deployment.apps/web configured\n"
ROLLBACK_OK = "deployment.apps/web rolled back\n"
REFUSAL = "[Protected] Access to namespace 'prod' is not permitted."
FORBIDDEN = "[kubectl exited 1] Error from server (Forbidden): deployments.apps is forbidden"
UNREACHABLE = "[kubectl exited 1] The connection to the server localhost:8080 was refused"


def _oracle(met: bool = False, evaluated: bool = True) -> object:
    return lambda: PostconditionResult(
        met=met, ready=0 if not met else 3, desired=3, detail="0/3 ready", evaluated=evaluated
    )


def _run(rollback_output: str, *, oracle=None, rollback_command: str | None = "rollout undo"):
    """Apply succeeds, the oracle fails, the rollback answers with `rollback_output`."""
    seen: list[str] = []

    def apply_fn(command: str) -> str:
        seen.append(command)
        return APPLY_OK if len(seen) == 1 else rollback_output

    result = execute_transactional(
        "set image deploy/web web=web:1.3",
        oracle or _oracle(),
        rollback_command=rollback_command,
        apply_fn=apply_fn,
    )
    return result, seen


# ── The four rollbacks that did not happen ─────────────────────────────────────

def test_a_refused_rollback_is_not_a_rollback() -> None:
    result, _ = _run(REFUSAL)
    assert result.status == ROLLBACK_REFUSED


@pytest.mark.parametrize("output", [FORBIDDEN, UNREACHABLE])
def test_a_rejected_rollback_is_not_a_rollback(output: str) -> None:
    result, _ = _run(output)
    assert result.status == ROLLBACK_FAILED


def test_a_silent_rollback_is_not_confirmed() -> None:
    """Nothing said it failed. Nothing said it ran. Those are different answers."""
    result, _ = _run("(no output)")
    assert result.status == ROLLBACK_UNCONFIRMED


@pytest.mark.parametrize("output", [REFUSAL, FORBIDDEN, UNREACHABLE, "(no output)"])
def test_none_of_them_claim_the_cluster_was_restored(output: str) -> None:
    result, _ = _run(output)
    assert result.status != ROLLED_BACK


# ── The one that did ───────────────────────────────────────────────────────────

def test_a_confirmed_rollback_is_still_reported_as_one() -> None:
    """Vacuity guard — the status is still reachable, and this is how it is earned."""
    result, seen = _run(ROLLBACK_OK)
    assert result.status == ROLLED_BACK
    assert seen == ["set image deploy/web web=web:1.3", "rollout undo"]


def test_the_rollback_output_is_always_carried_back() -> None:
    """Whatever the verdict, the operator gets the text the verdict was read from."""
    for output in (ROLLBACK_OK, REFUSAL, FORBIDDEN, UNREACHABLE):
        result, _ = _run(output)
        assert result.rollback_output == output.strip(), result.status


# ── The branches around it are untouched ───────────────────────────────────────

def test_a_met_postcondition_never_rolls_back() -> None:
    result, seen = _run(ROLLBACK_OK, oracle=_oracle(met=True))
    assert result.status == COMMITTED
    assert len(seen) == 1, "the rollback command must not have been issued"


def test_an_oracle_that_could_not_look_never_rolls_back() -> None:
    """The pass-that-fixed-it property: no mutation on a cluster we cannot read."""
    result, seen = _run(ROLLBACK_OK, oracle=_oracle(evaluated=False))
    assert result.status == VERIFY_INCONCLUSIVE
    assert len(seen) == 1, "the rollback command must not have been issued"


def test_a_failed_postcondition_with_no_rollback_command_still_escalates() -> None:
    result, seen = _run(ROLLBACK_OK, rollback_command=None)
    assert result.status == VERIFY_FAILED_NO_ROLLBACK
    assert len(seen) == 1


def test_a_refused_apply_never_reaches_the_rollback() -> None:
    """The original bug this module was written for, re-asserted from the new suite."""
    seen: list[str] = []

    def apply_fn(command: str) -> str:
        seen.append(command)
        return REFUSAL

    result = execute_transactional(
        "set image deploy/web web=web:1.3", _oracle(),
        rollback_command="rollout undo", apply_fn=apply_fn,
    )
    assert result.status == APPLY_REFUSED
    assert len(seen) == 1, "a refused apply must not be undone"


def test_every_rollback_status_is_distinct() -> None:
    """Non-vacuity: four names that resolved to the same string would prove nothing above."""
    names = {ROLLED_BACK, ROLLBACK_REFUSED, ROLLBACK_FAILED, ROLLBACK_UNCONFIRMED}
    assert len(names) == 4, names
