"""Two read verbs use a non-zero exit to *answer*. Wrapping those as failures is a lie.

`run_kubectl` was taught on 2026-08-24 to state a non-zero exit and to caveat anything printed
alongside it: *"it may be partial, and absence from it is NOT evidence"*. Correct for a command
that failed — but `kubectl diff` **exits 1 when it finds differences** (0 = none, >1 = the tool
failed) and `kubectl auth can-i` **exits 1 when the answer is "no"**. Both are documented, both
are the ordinary outcome, and in both cases stdout carries the authoritative result.

Measured before the fix: a complete diff came back as `[kubectl exited 1] (kubectl wrote nothing
to stderr)` with the diff itself demoted into the not-evidence block, and `can-i` was asymmetric —
a clean `yes` against a `no` the agent is told not to trust. An agent reasoning about its own
permissions cannot use an answer it has been warned about.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from app.tools.aci import kubectl_output as out
from app.tools.kubectl_tool import _exit_is_an_answer, run_kubectl

DIFF = ("--- LIVE\n+++ MERGED\n@@ -12,7 +12,7 @@\n"
        "-        image: web:1.2.0\n+        image: web:1.3.0\n")
CAVEAT = "NOT evidence"


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout, proc.stderr, proc.returncode = stdout, stderr, returncode
    return proc


def _kubectl(command: str, **kw: object) -> str:
    with patch("subprocess.run", return_value=_proc(**kw)):  # type: ignore[arg-type]
        return run_kubectl.invoke({"command": command})


# ── kubectl diff ───────────────────────────────────────────────────────────────

def test_a_diff_that_found_differences_is_returned_as_the_diff() -> None:
    answer = _kubectl("diff -f -", stdout=DIFF, returncode=1)
    assert answer == DIFF
    assert "[kubectl exited" not in answer
    assert CAVEAT not in answer


def test_a_diff_that_found_differences_classifies_as_a_reading() -> None:
    assert out.classify_output(_kubectl("diff -f -", stdout=DIFF, returncode=1)) == out.OK


def test_a_diff_with_no_differences_is_still_silence() -> None:
    assert _kubectl("diff -f -", returncode=0) == "(no output)"


def test_a_diff_that_actually_failed_is_still_an_error() -> None:
    """Vacuity guard — the exemption is exit 1 only, exactly as kubectl documents it."""
    answer = _kubectl("diff -f -", stderr="error: unable to parse STDIN", returncode=2)
    assert answer.startswith("[kubectl exited 2]"), answer


# ── kubectl auth can-i ─────────────────────────────────────────────────────────

def test_a_denied_can_i_is_returned_as_the_word_no() -> None:
    answer = _kubectl("auth can-i get secrets -n prod", stdout="no\n", returncode=1)
    assert answer.strip() == "no"
    assert "[kubectl exited" not in answer
    assert CAVEAT not in answer


def test_can_i_answers_are_symmetric() -> None:
    """The point of the fix: `no` must be as usable an answer as `yes`."""
    yes = _kubectl("auth can-i get pods -n prod", stdout="yes\n")
    no = _kubectl("auth can-i get secrets -n prod", stdout="no\n", returncode=1)
    for answer in (yes, no):
        assert CAVEAT not in answer, answer
        assert out.classify_output(answer) == out.OK
    assert yes.strip() == "yes"
    assert no.strip() == "no"


def test_a_can_i_that_actually_failed_still_surfaces_the_error() -> None:
    """Exit 1 with nothing on stdout is not the answer "no" — kubectl never printed one."""
    answer = _kubectl("auth can-i get pods -n prod",
                      stderr="error: You must be logged in to the server", returncode=1)
    assert "You must be logged in" in answer
    assert out.classify_output(answer) == out.FAILED


def test_a_can_i_exit_above_one_is_still_an_error() -> None:
    """Exit 1 means "no". Any other non-zero code is the tool failing, not an answer."""
    answer = _kubectl("auth can-i get pods -n prod", stderr="error: bad flag", returncode=2)
    assert answer.startswith("[kubectl exited 2]"), answer


def test_auth_whoami_is_not_covered_by_the_can_i_exemption() -> None:
    """Scoped to the subcommand: `auth whoami` has no answer-by-exit-code contract."""
    answer = _kubectl("auth whoami", stderr="error: not authenticated", returncode=1)
    assert answer.startswith("[kubectl exited 1]"), answer


# ── Everything else still reports its failure ──────────────────────────────────

@pytest.mark.parametrize("command", [
    "get pods -n prod",
    "describe deployment web -n prod",
    "logs web -n prod",
    "top pods -n prod",
    "events -n prod",
    "wait --for=condition=Ready pod/web -n prod",
])
def test_every_other_read_verb_still_reports_a_nonzero_exit(command: str) -> None:
    answer = _kubectl(command, stderr="Error from server (Forbidden): nope", returncode=1)
    assert answer.startswith("[kubectl exited 1]"), answer
    assert out.classify_output(answer) == out.FAILED


def test_a_forbidden_get_that_printed_rows_still_carries_the_caveat() -> None:
    """Vacuity guard — the not-evidence caveat did not simply disappear from the tool."""
    answer = _kubectl("get pods -A", stdout="NAME  READY\nweb   1/1\n",
                      stderr="Error from server (Forbidden): nope", returncode=1)
    assert CAVEAT in answer, answer


# ── The predicate itself ───────────────────────────────────────────────────────

@pytest.mark.parametrize("verb,args,code,expected", [
    # `args` is the full token list, `kubectl` included — `_operand_index` skips the global
    # flags starting at index 1, so a list that omits it reads the wrong token as the operand.
    ("diff", ["kubectl", "diff", "-f", "-"], 1, True),
    ("diff", ["kubectl", "diff", "-f", "-"], 2, False),
    ("diff", ["kubectl", "diff", "-f", "-"], 0, False),
    ("auth", ["kubectl", "auth", "can-i", "get", "pods"], 1, True),
    ("auth", ["kubectl", "auth", "can-i", "get", "pods"], 2, False),
    ("auth", ["kubectl", "auth", "whoami"], 1, False),
    ("get", ["kubectl", "get", "pods"], 1, False),
    ("logs", ["kubectl", "logs", "web"], 1, False),
])
def test_the_predicate_covers_exactly_these_two_contracts(
    verb: str, args: list[str], code: int, expected: bool
) -> None:
    assert _exit_is_an_answer(verb, args, code) is expected


def test_the_flag_first_spelling_of_can_i_is_still_recognised() -> None:
    """`kubectl -n prod auth can-i …` — the operand parser skips flags, so this must hold."""
    assert _exit_is_an_answer(
        "auth", ["kubectl", "-n", "prod", "auth", "can-i", "get", "pods"], 1) is True
