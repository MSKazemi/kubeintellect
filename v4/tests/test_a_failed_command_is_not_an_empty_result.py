"""`kubectl` failed and the agent was told the cluster was empty.

Two independent paths in `run_kubectl` turned a *failure* into an *answer*, both measured
2026-08-24 by driving `subprocess.run`:

1. **The pipe emulator ran over stderr.** `output = proc.stdout or proc.stderr` merged the two,
   then `| grep` filtered the result. An RBAC `Forbidden` piped through `grep Running` returned
   `(no matching lines)` — byte-for-byte identical to a successful listing with nothing running.
   A real shell never pipes stderr, so this was not even faithful emulation; the error hint the
   module had just attached ("Insufficient RBAC permissions") was filtered away with it.

2. **A partial failure dropped stderr entirely.** `stdout or stderr` keeps stdout whenever it has
   anything, so `kubectl get pods -A` with one namespace forbidden returned a complete-looking
   listing and no sign that a namespace had been denied.

Both land on the evidence path the grounded-diagnosis claim rests on: the model reads the tool
result as observation. "I looked and found nothing" and "I was not allowed to look" must never
serialize to the same string.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

FORBIDDEN = ('Error from server (Forbidden): pods is forbidden: User "sa:ki" cannot list '
             'resource "pods" in API group "" in the namespace "prod"')


def _call(command: str, *, stdout: str = "", stderr: str = "", rc: int = 0) -> str:
    from app.tools.kubectl_tool import run_kubectl

    proc = MagicMock()
    proc.stdout, proc.stderr, proc.returncode = stdout, stderr, rc
    with patch("subprocess.run", return_value=proc):
        return run_kubectl.invoke({"command": command, "stdin": None})


# ── 1. the piped-away error ────────────────────────────────────────────────────

def test_a_failure_piped_through_grep_still_reports_the_failure():
    out = _call("kubectl get pods -n prod | grep Running", stderr=FORBIDDEN + "\n", rc=1)
    assert "Forbidden" in out
    assert "no matching lines" not in out


def test_the_two_answers_are_no_longer_the_same_string():
    """The property that was broken, stated directly: 'denied' and 'nothing matched' must not
    serialize identically."""
    denied = _call("kubectl get pods -n prod | grep Running", stderr=FORBIDDEN + "\n", rc=1)
    empty = _call("kubectl get pods -n prod | grep Running", stdout="nginx  Pending\n", rc=0)
    assert denied != empty


def test_the_error_hint_survives_the_pipe():
    """The hint is the part that tells a non-expert what to do; it was the first thing grep ate."""
    out = _call("kubectl get pods -n prod | grep Running", stderr=FORBIDDEN + "\n", rc=1)
    assert "RBAC" in out


# ── 2. the dropped stderr ──────────────────────────────────────────────────────

def test_a_partial_failure_reports_both_halves():
    out = _call("kubectl get pods -A", stdout="default  nginx  Running\n",
                stderr=FORBIDDEN + "\n", rc=1)
    assert "Forbidden" in out          # the half that used to vanish
    assert "nginx" in out              # …without losing the half that worked


def test_a_partial_listing_is_labelled_as_possibly_partial():
    """Showing both halves is not enough: the surviving rows must not read as a full census."""
    out = _call("kubectl get pods -A", stdout="default  nginx  Running\n",
                stderr=FORBIDDEN + "\n", rc=1)
    assert "NOT evidence" in out


def test_the_exit_code_is_stated_not_implied():
    """Without it the model has to infer failure from the wording of a message it did not write."""
    out = _call("kubectl get pods -n prod", stderr=FORBIDDEN + "\n", rc=1)
    assert "[kubectl exited 1]" in out


def test_a_failure_with_no_stderr_at_all_still_says_it_failed():
    """A bare exit code with an empty message reads as a blank tool result; say which of the two
    silences it is."""
    out = _call("kubectl get pods -n prod", stdout="", stderr="", rc=7)
    assert "[kubectl exited 7]" in out
    assert "nothing to stderr" in out
    assert "(no output)" not in out


# ── vacuity guards: the success path is untouched ──────────────────────────────

def test_a_successful_command_is_returned_verbatim():
    """Without this, prefixing every result would pass every assertion above."""
    out = _call("kubectl get pods -n default", stdout="nginx  1/1  Running\n", rc=0)
    assert out == "nginx  1/1  Running\n"
    assert "kubectl exited" not in out


def test_a_successful_grep_with_no_match_still_says_so():
    """`(no matching lines)` is the right answer here and must not be replaced by an error."""
    out = _call("kubectl get pods -n default | grep Running", stdout="nginx  Pending\n", rc=0)
    assert "no matching lines" in out
    assert "kubectl exited" not in out


def test_a_successful_grep_still_filters():
    out = _call("kubectl get pods -A | grep prod",
                stdout="prod  api  Running\ndev  web  Running\n", rc=0)
    assert "prod" in out and "dev" not in out


def test_a_warning_on_stderr_with_a_zero_exit_is_not_an_error():
    """kubectl writes deprecation warnings to stderr and exits 0. That is not a failure."""
    out = _call("kubectl get pods -n default", stdout="", stderr="Warning: v1beta1 deprecated\n")
    assert "kubectl exited" not in out
    assert "deprecated" in out


# ── the ordering the fix depends on ───────────────────────────────────────────

def test_the_namespace_filter_never_sees_an_error_message(monkeypatch):
    """The blocked-namespace filters parse a listing. Feeding them an error message is how a
    filter starts editing prose it does not understand — asserted on what they are *called with*,
    because the composed answer reads raw stderr and would look identical either way."""
    from app.tools import kubectl_tool

    seen: list[str] = []
    for name in ("_filter_namespace_output", "_filter_all_namespaces_output"):
        original = getattr(kubectl_tool, name)
        def spy(verb, args, text, _orig=original):
            seen.append(text)
            return _orig(verb, args, text)
        monkeypatch.setattr(kubectl_tool, name, spy)

    out = _call("kubectl get namespaces", stderr="Error from server: nope\n", rc=1)
    assert "Error from server: nope" in out
    assert seen, "the filters were not called at all — this test would prove nothing"
    assert not any("Error from server" in text for text in seen), seen


def test_a_zero_exit_warning_is_never_piped():
    """A real shell does not feed stderr to grep. With stdout empty the pipe sees nothing, so a
    stderr warning must not be able to *match* and become the answer."""
    out = _call("kubectl get pods -n default | grep deprecated",
                stdout="", stderr="Warning: v1beta1 deprecated\n", rc=0)
    assert "no matching lines" in out
    assert "deprecated" not in out


@pytest.mark.parametrize("rc", [0, 1])
def test_the_output_cap_still_applies_on_both_paths(rc):
    big = "x" * 20_000
    out = _call("kubectl get pods -A", stdout=big, stderr="boom\n" if rc else "", rc=rc)
    assert "truncated" in out
    assert len(out) < 20_000
