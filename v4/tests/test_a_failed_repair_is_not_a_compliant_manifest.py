"""An empty fix-PR has two causes, and only one of them means the manifest is fine.

`propose_fix` fails safe by returning the ORIGINAL manifest — correct, because a corrupted
manifest is far worse than none. But returning the original was the *whole* answer, so a repair
that never ran was byte-identical to a repair that found nothing to change. Measured 2026-08-24
end to end, an LLM exception, an empty response and a refusal in prose all reached `open_pr` as:

    "no change to propose (fix is a no-op)"

— the sentence a compliant manifest earns. On a security misconfig path that reads as *nothing to
do* while the violation stands untouched.

The same run surfaced a second defect on the same path, in the other direction: `_strip_fences`
ends in `.strip()`, so a model that **echoed the manifest back** — its way of saying nothing needs
changing — returned one trailing newline shorter than it went in. That is a real diff (one line
removed, the same line added), `is_noop` was False, and `open_pr` **pushed a branch and opened a
pull request** titled as a security fix whose entire content was a missing newline.
"""
from __future__ import annotations

import pytest
from app.tools.aci.fix_pr import make_fix_pr, unified_diff
from app.tools.aci.gitops import open_pr
from app.tools.aci.repair import propose_fix

MANIFEST = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "metadata:\n"
    "  name: web\n"
    "spec:\n"
    "  containers:\n"
    "  - name: c\n"
    "    securityContext:\n"
    "      privileged: true\n"
)
FIXED = MANIFEST.replace("privileged: true", "privileged: false")
VIOLATION = "CIS 5.2.1: container must not run privileged"


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


class _LLM:
    def __init__(self, content: str) -> None:
        self.content = content

    async def ainvoke(self, _messages: object) -> _Reply:
        return _Reply(self.content)


class _Boom:
    async def ainvoke(self, _messages: object) -> _Reply:
        raise RuntimeError("rate limit exceeded")


def _never_runs(_argv: list[str]) -> tuple[int, str]:
    raise AssertionError("a command was run for a PR that should not have been opened")


async def _pipeline(llm: object, runner=None):
    """repair → package → open, the way the fix-PR flow actually composes."""
    proposal = await propose_fix(MANIFEST, VIOLATION, llm=llm)
    pr = make_fix_pr(
        "pod.yaml", MANIFEST, proposal.manifest,
        title="fix: drop privileged", rationale=VIOLATION,
        repair_failed_reason="" if proposal.repaired else proposal.reason,
    )
    return proposal, pr, open_pr(pr, repo_dir="/repo", branch="fix/x",
                                 runner=runner or _never_runs)


# ── A repair that never ran ────────────────────────────────────────────────────

@pytest.mark.parametrize("llm,fragment", [
    (_Boom(), "raised"),
    (_LLM(""), "empty response"),
    (_LLM("I cannot help with that."), "not a manifest"),
])
async def test_a_failed_repair_says_the_violation_is_unresolved(llm, fragment: str) -> None:
    proposal, pr, result = await _pipeline(llm)
    assert proposal.repaired is False
    assert fragment in proposal.reason, proposal.reason
    assert pr.is_noop is True
    assert result.pushed is False
    assert "UNRESOLVED" in result.detail, result.detail
    assert fragment in result.detail, result.detail


@pytest.mark.parametrize("llm", [_Boom(), _LLM(""), _LLM("I cannot help with that.")])
async def test_a_failed_repair_never_claims_the_manifest_is_fine(llm) -> None:
    _, _, result = await _pipeline(llm)
    assert "needed no edit" not in result.detail, result.detail


async def test_a_failed_repair_still_returns_the_original_manifest_untouched() -> None:
    """The fail-safe property that was already right, re-asserted so the fix cannot lose it."""
    proposal, _, _ = await _pipeline(_Boom())
    assert proposal.manifest == MANIFEST


# ── A repair that ran and found nothing ────────────────────────────────────────

async def test_an_echoed_manifest_is_a_no_op_and_says_why() -> None:
    proposal, pr, result = await _pipeline(_LLM(MANIFEST))
    assert proposal.repaired is True, "the model answered; it just had no edit to make"
    assert pr.is_noop is True
    assert result.pushed is False
    assert "needed no edit" in result.detail, result.detail
    assert "UNRESOLVED" not in result.detail, result.detail


async def test_an_echoed_manifest_does_not_open_a_pull_request() -> None:
    """The outward-facing half: `_never_runs` fails the test if any command is issued."""
    _, _, result = await _pipeline(_LLM(MANIFEST))
    assert result.pushed is False and result.pr_opened is False


def test_a_trailing_newline_is_not_a_change() -> None:
    assert unified_diff("pod.yaml", MANIFEST, MANIFEST.rstrip("\n")) == ""
    assert unified_diff("pod.yaml", MANIFEST.rstrip("\n"), MANIFEST) == ""
    assert make_fix_pr("pod.yaml", MANIFEST, MANIFEST.strip(),
                       title="t", rationale="r").is_noop is True


def test_a_real_change_is_still_a_change() -> None:
    """Vacuity guard — normalising trailing newlines did not flatten every diff to empty."""
    diff = unified_diff("pod.yaml", MANIFEST, FIXED)
    assert "privileged: false" in diff
    assert make_fix_pr("pod.yaml", MANIFEST, FIXED, title="t", rationale="r").is_noop is False


def test_a_change_that_only_adds_a_final_line_is_still_a_change() -> None:
    """The normaliser collapses trailing newlines, not trailing content."""
    assert unified_diff("pod.yaml", MANIFEST, MANIFEST + "  restartPolicy: Never\n") != ""


# ── A repair that ran and fixed it ─────────────────────────────────────────────

async def test_a_real_fix_opens_the_pull_request() -> None:
    """Vacuity guard in the other direction — the flow still reaches a PR."""
    issued: list[list[str]] = []

    def runner(argv: list[str]) -> tuple[int, str]:
        issued.append(argv)
        return 0, "https://example.invalid/pr/1"

    proposal, pr, result = await _pipeline(_LLM(FIXED), runner=runner)
    assert proposal.repaired is True
    assert pr.is_noop is False
    assert result.pushed is True and result.pr_opened is True
    assert [a[0] for a in issued] == ["git", "gh"]


async def test_the_reason_is_empty_when_a_fix_was_produced() -> None:
    proposal, pr, _ = await _pipeline(_LLM(FIXED), runner=lambda a: (0, "url"))
    assert proposal.reason == ""
    assert pr.repair_failed_reason == ""
