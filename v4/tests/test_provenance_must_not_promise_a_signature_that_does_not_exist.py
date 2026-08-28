"""A13 — `provenance` must not assert a signature the release does not carry.

`test_what_you_installed_can_be_traced_to_its_build.py` proves, in 28 tests, that the publishing
workflows are *written* right: the attest step is there, its permissions are there, the digest it
binds is the build digest, the printed commands pin a `--signer-workflow` that names a file which
exists. Every one of those is a statement about the repository as it stands today.

None of them is a statement about the release the command actually names, and that is where it
was wrong. `cmd_provenance` opened with, unconditionally and in the present indicative:

    Each artifact carries a keyless sigstore attestation minted by the workflow that built it

with `--tag` defaulting to this build's own version. Measured 2026-08-28: the attest steps were
added in `8b8046d`, *after* `v2.3.1` was tagged — `git show v2.3.1:.github/workflows/
docker-publish.yml | grep -c attest` is `0` — and GitHub's attestations API 404s the whole
subresource for this repository, where a repo that has published one returns `{"attestations":[]}`.
The four publish workflows did run on the tag and did succeed; they simply had no attest step yet.

So the bare `kubeintellect provenance` — no arguments, the overwhelmingly common invocation —
told every user that the only released version was signed, and then handed them four commands
that must fail. The module's own docstring says the opposite, accurately: *"every artifact
published to date carries no attestation at all — verifying an existing release will correctly
fail to find one."* The true statement was in a docstring the maintainer reads and the false one
was on the screen the user reads.

The fix is not wording. It is that the claim now has to be *derived* from a recorded fact —
`FIRST_ATTESTED_TAG` — so that it cannot drift from reality again: while it is `None` the command
says nothing is signed, and it starts saying otherwise only when a signed release exists to
point at.
"""
from __future__ import annotations

import argparse

import pytest

from app.core import supply_chain as sc


class TestTheRecordedFact:
    """`FIRST_ATTESTED_TAG` is the single place the claim comes from."""

    def test_the_module_records_whether_any_release_is_attested(self):
        assert hasattr(sc, "FIRST_ATTESTED_TAG"), (
            "the claim must derive from a recorded fact, not from prose in a print statement"
        )

    def test_no_release_is_attested_yet(self):
        # Measured 2026-08-28. When the first signed release is cut this flips to that tag, and
        # this test is the thing that makes flipping it a deliberate act.
        assert sc.FIRST_ATTESTED_TAG is None

    def test_an_unsigned_project_expects_no_attestation_for_any_tag(self):
        assert sc.attestation_expected("v2.3.1") is False
        assert sc.attestation_expected("v99.0.0") is False

    def test_a_release_before_the_first_attested_one_is_still_unattested(self, monkeypatch):
        monkeypatch.setattr(sc, "FIRST_ATTESTED_TAG", "v2.4.0")
        assert sc.attestation_expected("v2.3.1") is False
        assert sc.attestation_expected("v2.4.0") is True
        assert sc.attestation_expected("v2.4.1") is True

    def test_versions_compare_numerically_not_as_strings(self, monkeypatch):
        # "v2.10.0" < "v2.9.0" as strings, and that would silently mark a signed release unsigned.
        monkeypatch.setattr(sc, "FIRST_ATTESTED_TAG", "v2.9.0")
        assert sc.attestation_expected("v2.10.0") is True

    def test_the_leading_v_is_optional_on_both_sides(self, monkeypatch):
        monkeypatch.setattr(sc, "FIRST_ATTESTED_TAG", "v2.4.0")
        assert sc.attestation_expected("2.4.0") is True


def _run_provenance(tag: str, capsys) -> str:
    from app import cli

    cli.cmd_provenance(argparse.Namespace(tag=tag))
    return capsys.readouterr().out


class TestWhatTheUserIsTold:

    def test_it_does_not_claim_an_unattested_release_is_signed(self, capsys):
        out = _run_provenance("v2.3.1", capsys)
        assert "Each artifact carries" not in out, (
            "v2.3.1 carries no attestation — the attest steps postdate the tag"
        )

    def test_it_says_plainly_that_this_release_is_not_signed(self, capsys):
        # Bound to the tag on purpose. Asserting a bare "no attestation" passed before the fix
        # existed, by matching the homebrew-tap exemption's "`brew install` checks no attestation"
        # — a sentence about a different channel, in a section about something else entirely.
        out = _run_provenance("v2.3.1", capsys).lower()
        assert "v2.3.1 is not signed" in out

    def test_it_warns_that_the_commands_below_will_fail(self, capsys):
        # A user who runs them anyway must already know the failure is expected, not a tampered
        # artifact — the one reading of a failed verification that would be genuinely alarming.
        out = _run_provenance("v2.3.1", capsys).lower()
        assert "fail" in out

    def test_it_still_prints_the_commands(self, capsys):
        # The commands are correct and unexercised, not wrong. Withholding them would lose the
        # only documentation of the identity a verifier must pin to.
        out = _run_provenance("v2.3.1", capsys)
        assert "gh attestation verify" in out
        assert "--signer-workflow" in out

    def test_it_makes_the_affirmative_claim_once_a_release_is_signed(self, capsys, monkeypatch):
        monkeypatch.setattr(sc, "FIRST_ATTESTED_TAG", "v2.3.0")
        out = _run_provenance("v2.3.1", capsys)
        assert "Each artifact carries" in out
        assert "v2.3.1 is not signed" not in out.lower()

    def test_an_older_release_stays_honest_after_the_first_signed_one(self, capsys, monkeypatch):
        monkeypatch.setattr(sc, "FIRST_ATTESTED_TAG", "v2.4.0")
        out = _run_provenance("v2.3.1", capsys)
        assert "Each artifact carries" not in out

    @pytest.mark.parametrize("tag", ["v2.3.1", "2.3.1"])
    def test_the_warning_does_not_depend_on_how_the_tag_was_typed(self, tag, capsys):
        out = _run_provenance(tag, capsys).lower()
        assert "v2.3.1 is not signed" in out


class TestTheDefaultInvocation:
    """`kubeintellect provenance` with no arguments is the case that was wrong."""

    def test_the_bare_command_names_this_builds_own_version(self, capsys):
        # Driven through `main` rather than a parser factory, because the parser is built inside
        # `main` — and because the bare invocation IS the defect, so it is what deserves the test.
        from app import cli

        cli.main(["provenance"])
        assert f"v{cli.__version__}" in capsys.readouterr().out

    def test_the_bare_command_does_not_promise_a_signature(self, capsys):
        from app import cli

        cli.main(["provenance"])
        out = capsys.readouterr().out
        assert "Each artifact carries" not in out
        assert f"v{cli.__version__} is not signed" in out.lower()

    def test_the_default_version_is_one_that_carries_no_attestation(self):
        from app import cli

        assert sc.attestation_expected(f"v{cli.__version__}") is False, (
            "if this fails, a signed release exists — set FIRST_ATTESTED_TAG and update this test"
        )


class TestTheDocSaysTheSameThing:
    """docs/security.md § 8 made the identical claim, on the public surface.

    It opened with "Every release artifact carries a signed build attestation" and put the
    correction three subsections down under *What this does not prove* — a heading someone
    scanning for "how do I verify" skips, and which is about the limits of attestation rather
    than its absence. Tying the doc to `FIRST_ATTESTED_TAG` means signing the first release
    fails this test until the page is updated too.
    """

    @staticmethod
    def _doc() -> str:
        from pathlib import Path

        p = Path(__file__).resolve().parents[1] / "docs" / "security.md"
        assert p.is_file(), p
        return p.read_text(encoding="utf-8")

    def test_it_does_not_claim_every_artifact_is_signed(self):
        assert "Every release artifact carries" not in self._doc()

    def test_it_warns_while_nothing_is_signed(self):
        if sc.FIRST_ATTESTED_TAG is not None:
            pytest.skip("a signed release exists; the warning should have been removed")
        doc = self._doc()
        assert "No published release is signed yet" in doc, (
            "security.md § 8 must carry the warning for as long as FIRST_ATTESTED_TAG is None"
        )

    def test_the_warning_is_at_the_top_of_the_section_not_buried(self):
        doc = self._doc()
        start = doc.index("## 8. Supply chain")
        warning = doc.index("No published release is signed yet")
        first_command = doc.index("gh attestation verify", start)
        assert start < warning < first_command, (
            "the reader must meet the warning before the commands it qualifies"
        )
