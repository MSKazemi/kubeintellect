"""`run_helm` must not merge stderr into the result before deciding what the result is.

Measured 2026-08-24, against a fake helm: a **successful** `helm list -A -o json` on a cluster
with one release and an `Error: Kubernetes cluster unreachable` returned the *same string* —

    "[Protected] This release listing could not be parsed, so releases in protected
     namespaces could not be removed from it."

— because `output = stdout + stderr` handed `json.loads` a document with helm's routine
`WARNING: Kubernetes configuration file is group-readable` glued to the front of it. The release
and the error were both deleted, and what the model was told instead named *protection* as the
cause. `proc.returncode` reached the answer only when helm printed nothing at all, so every other
failure was returned as if it were the answer to the question.

Same class as `test_a_failed_command_is_not_an_empty_result.py` for `run_kubectl`: a surface that
lies to an operator gets questioned, a surface that lies to the model becomes a diagnosis.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from app.tools import helm_tool
from app.tools.helm_tool import run_helm

# helm prints this whenever ~/.kube/config is group-readable, which is the common case on a
# shared box. It is a warning about the *client*, not about the cluster.
GROUP_READABLE = (
    "WARNING: Kubernetes configuration file is group-readable. This is insecure. "
    "Location: /home/u/.kube/config\n"
)
LISTING_JSON = '[{"name":"shop","namespace":"default","revision":"1","status":"deployed"}]\n'
UNREACHABLE = (
    'Error: Kubernetes cluster unreachable: Get "https://10.0.0.1/version": '
    "dial tcp: i/o timeout\n"
)


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout, proc.stderr, proc.returncode = stdout, stderr, returncode
    return proc


def _run(command: str, **kw: object) -> str:
    with patch("subprocess.run", return_value=_proc(**kw)):  # type: ignore[arg-type]
        return run_helm.invoke({"command": command})


# ── The reproduction ───────────────────────────────────────────────────────────

def test_a_client_warning_does_not_destroy_a_successful_listing() -> None:
    out = _run("helm list -A -o json", stdout=LISTING_JSON, stderr=GROUP_READABLE)
    assert "shop" in out, out
    assert "could not be parsed" not in out, out


def test_the_healthy_listing_and_the_outage_are_not_the_same_answer() -> None:
    healthy = _run("helm list -A -o json", stdout=LISTING_JSON, stderr=GROUP_READABLE)
    outage = _run("helm list -A -o json", stdout="[]\n", stderr=UNREACHABLE, returncode=1)
    assert healthy != outage


def test_two_identical_runs_do_compare_equal() -> None:
    """Vacuity guard for the test above — the comparison is able to report sameness."""
    a = _run("helm list -A -o json", stdout=LISTING_JSON, stderr=GROUP_READABLE)
    b = _run("helm list -A -o json", stdout=LISTING_JSON, stderr=GROUP_READABLE)
    assert a == b


# ── A failure says it failed ───────────────────────────────────────────────────

@pytest.mark.parametrize("command,stdout,stderr", [
    ("helm list -A -o json", "[]\n", UNREACHABLE),
    ("helm list -A", "", 'Error: list: failed to list: secrets is forbidden: User "sa"\n'),
    ("helm status shop -n default", "", "Error: release: not found\n"),
    ("helm get values shop", "", "Error: release: not found\n"),
])
def test_a_nonzero_exit_is_reported_as_one(command: str, stdout: str, stderr: str) -> None:
    out = _run(command, stdout=stdout, stderr=stderr, returncode=1)
    assert out.startswith("[helm exited 1]"), out


def test_a_successful_run_does_not_claim_an_exit_code() -> None:
    """Vacuity guard in the other direction — the marker is not printed unconditionally."""
    out = _run("helm list -A", stdout="NAME\tNAMESPACE\nshop\tdefault\n")
    assert "[helm exited" not in out, out


def test_a_failure_with_a_silent_helm_names_the_silence() -> None:
    out = _run("helm status shop", returncode=1)
    assert "[helm exited 1]" in out
    assert "(helm wrote nothing to stderr)" in out, out


# ── Partial output on a failure is not evidence ────────────────────────────────

def test_output_printed_before_a_failure_is_labelled_as_not_evidence() -> None:
    out = _run("helm list -A -o json", stdout="[]\n", stderr=UNREACHABLE, returncode=1)
    assert "NOT evidence" in out, out
    assert "unreachable" in out, out


def test_a_successful_result_is_not_labelled_as_not_evidence() -> None:
    """Vacuity guard — the caveat is attached to failures, not to every answer."""
    out = _run("helm list -A", stdout="NAME\tNAMESPACE\nshop\tdefault\n")
    assert "NOT evidence" not in out, out


def test_a_zero_exit_warning_is_marked_as_a_warning_and_not_as_the_result() -> None:
    out = _run("helm list -A", stdout="NAME\tNAMESPACE\nshop\tdefault\n", stderr=GROUP_READABLE)
    assert "shop" in out
    assert "helm exited 0, so this is a warning" in out, out
    assert "group-readable" in out


def test_a_failure_with_no_output_does_not_claim_helm_produced_any() -> None:
    """An empty partial-output block is still a claim that helm printed something."""
    out = _run("helm status shop", stderr="Error: release: not found\n", returncode=1)
    assert "helm also produced" not in out, out


def test_a_client_warning_appears_only_inside_the_block_that_labels_it() -> None:
    """Marking the warning is not enough if an unmarked copy is also glued to the result.

    Killed a mutant that re-merged stderr into `stdout` *after* the filters had run: every
    label was still printed, and the warning was in the answer twice — once as a warning and
    once as though helm had listed it.
    """
    out = _run("helm list -A", stdout="NAME\tNAMESPACE\nshop\tdefault\n", stderr=GROUP_READABLE)
    assert out.count("group-readable") == 1, out
    before, _, _ = out.partition("[helm also wrote to stderr")
    assert "WARNING:" not in before, before


def test_an_error_never_reaches_the_partial_output_shown_after_a_failure() -> None:
    out = _run("helm list -A -o json", stdout="[]\n", stderr=UNREACHABLE, returncode=1)
    _, _, partial = out.partition("NOT evidence:")
    assert "unreachable" not in partial, partial
    assert partial.strip() == "[]", partial


@pytest.mark.parametrize("blank", ["", "   \n", "\t\n"])
def test_neither_filter_manufactures_output_from_nothing(blank: str) -> None:
    """The assumption that lets `run_helm` skip `run_kubectl`'s pre-filter capture.

    `run_kubectl` must record whether kubectl printed anything *before* its pipe emulator runs,
    because `_apply_pipes("")` returns "(no matching lines)" — text our own grep invented. If a
    helm filter ever starts doing the same, this fails and the capture has to come back.
    """
    assert helm_tool._filter_release_namespaces(blank).strip() == ""
    assert helm_tool._strip_blocked_kinds(blank).strip() == ""


def test_stderr_alone_on_a_zero_exit_is_still_returned() -> None:
    """`helm env`-style output on stderr must not become "(no output)"."""
    out = _run("helm env", stderr="HELM_BIN=/usr/bin/helm\n")
    assert "HELM_BIN" in out, out


def test_a_silent_success_still_says_so() -> None:
    assert _run("helm list -A") == "(no output)"


# ── The filters are never shown an error message ───────────────────────────────

def test_the_listing_filter_is_never_called_with_stderr() -> None:
    """A filter parses a listing. Handing it an error message is how the error got deleted."""
    seen: list[str] = []
    real = helm_tool._filter_release_namespaces

    def spy(output: str) -> str:
        seen.append(output)
        return real(output)

    with patch.object(helm_tool, "_filter_release_namespaces", spy):
        _run("helm list -A -o json", stdout="[]\n", stderr=UNREACHABLE, returncode=1)
        _run("helm list -A -o json", stdout=LISTING_JSON, stderr=GROUP_READABLE)

    assert seen, "the spy never ran — this test would pass against any implementation"
    for text in seen:
        assert "unreachable" not in text, text
        assert "WARNING:" not in text, text


def test_the_manifest_stripper_is_never_called_with_stderr() -> None:
    seen: list[str] = []
    real = helm_tool._strip_blocked_kinds

    def spy(output: str) -> str:
        seen.append(output)
        return real(output)

    with patch.object(helm_tool, "_strip_blocked_kinds", spy):
        _run("helm get manifest shop", stdout="kind: ConfigMap\n", stderr=UNREACHABLE,
             returncode=1)

    assert seen, "the spy never ran — this test would pass against any implementation"
    for text in seen:
        assert "unreachable" not in text, text


# ── The protections the filters exist for still work ───────────────────────────

def test_protected_namespaces_are_still_removed_from_a_json_listing() -> None:
    stdout = (
        '[{"name":"shop","namespace":"default"},'
        '{"name":"prom","namespace":"monitoring"}]\n'
    )
    out = _run("helm list -A -o json", stdout=stdout, stderr=GROUP_READABLE)
    assert "monitoring" not in out, out
    assert "shop" in out, out


def test_protected_namespaces_are_still_removed_from_a_table_listing() -> None:
    stdout = "NAME\tNAMESPACE\nshop\tdefault\nprom\tkube-system\n"
    out = _run("helm list -A", stdout=stdout, stderr=GROUP_READABLE)
    assert "kube-system" not in out, out
    assert "shop" in out, out
    assert "withheld" in out, out


def test_secrets_are_still_stripped_from_a_manifest() -> None:
    stdout = "kind: ConfigMap\nmetadata:\n  name: a\n---\nkind: Secret\ndata:\n  p: aGk=\n"
    out = _run("helm get manifest shop", stdout=stdout, stderr=GROUP_READABLE)
    assert "aGk=" not in out, out
    assert "ConfigMap" in out, out
