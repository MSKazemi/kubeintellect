"""A13 — a released artifact now carries proof of which build produced it.

Before 2026-08-28 nothing published by this project could be checked by whoever installed it. The
`kq` release job wrote a `checksums.txt` and uploaded it to the **same release page** as the
tarballs it checksums — which detects a corrupted download and nothing else, because anyone able
to replace a tarball can replace the checksums beside it in the same breath.

The claims verified here are the ones a signature is worth nothing without:

* every workflow that publishes on a `v*` tag either attests, or is written down as not attesting
  with a dated reason — a new distribution channel cannot inherit silence;
* the attesting jobs hold the two permissions the attestation step needs, so the release does not
  discover that at the last step of a tag build;
* the attestation binds a **digest**, never a tag, because a tag is a mutable pointer;
* the published verification command pins the *signer workflow*, since without it the check
  accepts an attestation from any workflow in the repository;
* every action used by those workflows is pinned to a commit SHA, because a floating tag on the
  action that mints your provenance makes the provenance only as good as whoever can move it.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
import yaml

from app.core import supply_chain
from app.core.supply_chain import (
    NOT_ATTESTED,
    OIDC_ISSUER,
    REPO,
    SIGNED,
    UNATTESTED_WORKFLOWS,
    signer_identity,
    signer_workflow,
    verify_commands,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

_SHA = re.compile(r"^[0-9a-f]{40}$")


def _text(workflow: str) -> str:
    return (REPO_ROOT / workflow).read_text(encoding="utf-8")


def _parsed(workflow: str) -> dict:
    return yaml.safe_load(_text(workflow))


def _tag_triggered() -> list[Path]:
    """Workflow files that fire on a `v*` tag — i.e. the ones that publish a release."""
    out = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        # PyYAML reads the bare key `on:` as the boolean True.
        triggers = data.get("on") or data.get(True) or {}
        tags = ((triggers.get("push") or {}).get("tags")) or []
        if any(str(t).startswith("v") for t in tags):
            out.append(path)
    return out


def _gh_invocations(command: str) -> list[str]:
    """Split a printed command block into individual `gh attestation verify` invocations,
    rejoining shell line continuations first."""
    joined = command.replace("\\\n", " ")
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if line.startswith("gh attestation verify"):
            out.append(" ".join(line.split()))
    return out


def _job_permissions(workflow: str, job: str) -> dict:
    return _parsed(workflow)["jobs"][job].get("permissions") or {}


class TestEveryPublishingWorkflowIsAccountedFor:
    def test_the_workflow_directory_is_where_this_test_thinks_it_is(self):
        """Guards the rest of the file: a wrong root would make every check vacuously pass."""
        assert WORKFLOW_DIR.is_dir()
        assert (WORKFLOW_DIR / "docker-publish.yml").exists()

    def test_every_workflow_named_by_the_module_exists(self):
        """The workflow path IS the verification identity — a rename silently invalidates it."""
        for art in SIGNED:
            assert (REPO_ROOT / art.workflow).exists(), f"{art.key}: {art.workflow} is gone"
        for path in UNATTESTED_WORKFLOWS:
            assert (REPO_ROOT / path).exists(), path

    def test_every_tag_triggered_workflow_either_attests_or_says_why(self):
        signed = {art.workflow for art in SIGNED}
        for path in _tag_triggered():
            rel = str(path.relative_to(REPO_ROOT))
            assert rel in signed or rel in UNATTESTED_WORKFLOWS, (
                f"{rel} publishes on a v* tag but neither attests nor appears in "
                f"UNATTESTED_WORKFLOWS. Decide which, and write the reason down."
            )

    def test_the_exemptions_point_at_a_real_reason(self):
        for path, key in UNATTESTED_WORKFLOWS.items():
            assert key in NOT_ATTESTED, f"{path} claims reason '{key}', which does not exist"

    def test_all_four_artifact_classes_are_covered(self):
        assert {art.key for art in SIGNED} == {"image", "pypi", "binaries", "chart"}


class TestTheWorkflowsActuallyAttest:
    @pytest.mark.parametrize("key,job,marker", [
        ("image", "publish", "actions/attest-build-provenance"),
        ("binaries", "release", "actions/attest-build-provenance"),
        ("chart", "publish", "actions/attest-build-provenance"),
        ("pypi", "publish", "attestations: true"),
    ])
    def test_the_step_is_there(self, key, job, marker):
        art = next(a for a in SIGNED if a.key == key)
        assert marker in _text(art.workflow), f"{art.workflow} does not {marker}"

    @pytest.mark.parametrize("key,job", [
        ("image", "publish"), ("binaries", "release"), ("chart", "publish"),
    ])
    def test_the_job_holds_the_permissions_the_step_needs(self, key, job):
        """Without these two the attestation fails at the LAST step of a tag build — after the
        artifact is already published, which is the worst moment to find out."""
        art = next(a for a in SIGNED if a.key == key)
        perms = _job_permissions(art.workflow, job)
        assert perms.get("id-token") == "write", f"{art.workflow}:{job} lacks id-token: write"
        assert perms.get("attestations") == "write", f"{art.workflow}:{job} lacks attestations"

    def test_pypi_trusted_publishing_still_has_its_oidc_permission(self):
        assert _job_permissions(".github/workflows/publish.yml", "publish").get("id-token") == \
            "write"

    def test_the_image_sbom_is_generated_from_the_built_image(self):
        """An SBOM derived from the lockfile describes what was meant to be installed; the CVE
        that matters is usually in a base-image package no manifest mentions."""
        text = _text(".github/workflows/docker-publish.yml")
        assert "anchore/sbom-action" in text
        assert "actions/attest-sbom" in text
        assert "image: ${{ env.GHCR_IMAGE }}@${{ steps.build.outputs.digest }}" in text

    def test_the_binaries_job_still_publishes_checksums(self):
        """The attestation replaces what checksums.txt was being asked to prove, not the file —
        it is still the right tool for a truncated download."""
        text = _text(".github/workflows/release-binaries.yml")
        assert "checksums.txt" in text
        assert "actions/attest-build-provenance" in text


class TestItBindsADigestNotATag:
    def test_the_image_attestation_uses_the_build_digest(self):
        text = _text(".github/workflows/docker-publish.yml")
        assert "subject-digest: ${{ steps.build.outputs.digest }}" in text

    def test_the_chart_attestation_uses_the_pushed_digest(self):
        text = _text(".github/workflows/helm-publish.yml")
        assert "subject-digest: ${{ steps.push.outputs.digest }}" in text

    def test_the_chart_job_refuses_to_attest_an_unknown_digest(self):
        """`helm push` printing no Digest line must stop the release, not attest a blank."""
        text = _text(".github/workflows/helm-publish.yml")
        assert "refusing to attest an unknown digest" in text
        assert 'if [ -z "${digest}" ]' in text

    def test_a_dry_run_signs_nothing(self):
        """Nothing was pushed, so there is no published digest — and a transparency-log entry
        for an artifact that does not exist is worse than no entry."""
        text = _text(".github/workflows/docker-publish.yml")
        block = text[text.index("Attest build provenance"):]
        assert "if: ${{ !inputs.dry_run }}" in block[:400]


class TestTheVerifierIsPinnedToTheRightSigner:
    def test_the_identity_is_the_sigstore_san_format(self):
        assert signer_identity(".github/workflows/publish.yml", "v9.9.9") == (
            "https://github.com/MSKazemi/kubeintellect"
            "/.github/workflows/publish.yml@refs/tags/v9.9.9"
        )

    def test_the_identity_is_bound_to_the_tag(self):
        """An attestation minted by the same workflow on a branch push must not satisfy it."""
        a = signer_identity(".github/workflows/publish.yml", "v1.0.0")
        b = signer_identity(".github/workflows/publish.yml", "v1.0.1")
        assert a != b and a.endswith("refs/tags/v1.0.0")

    def test_every_gh_command_pins_the_signer_workflow(self):
        """Without --signer-workflow the check accepts an attestation from ANY workflow in the
        repo, which is a different and much weaker claim than the one the docs make.

        Checked per invocation, not per command block: an entry that prints two `gh` lines and
        pins only the first would satisfy a substring check while shipping one unpinned check.
        """
        found = 0
        for entry in verify_commands("v2.3.1"):
            for invocation in _gh_invocations(entry["command"]):
                found += 1
                assert "--signer-workflow" in invocation, f"{entry['key']}: {invocation}"
                assert f"--repo {REPO}" in invocation, f"{entry['key']}: {invocation}"
        assert found >= 4, f"only {found} gh invocations found — the parser missed some"

    def test_the_signer_workflow_argument_names_a_file_that_exists(self):
        for art in SIGNED:
            value = signer_workflow(art.workflow)
            assert value.startswith(REPO + "/")
            assert (REPO_ROOT / value[len(REPO) + 1:]).exists()

    def test_the_version_drops_the_v_but_the_identity_keeps_it(self):
        """A copied command that leaves the `v` in an image tag fails for a boring reason."""
        image = next(e for e in verify_commands("v2.3.1") if e["key"] == "image")
        assert ":2.3.1" in image["command"] and ":v2.3.1" not in image["command"]
        assert image["identity"].endswith("refs/tags/v2.3.1")

    def test_the_pypi_check_is_not_a_gh_command(self):
        """PEP 740 attestations live on PyPI, not in GitHub's attestation store — telling a user
        to run `gh attestation verify` on a wheel would send them to a check that cannot pass."""
        pypi = next(e for e in verify_commands("v2.3.1") if e["key"] == "pypi")
        assert pypi["command"].startswith("pypi-attestations verify pypi")
        assert "gh attestation" not in pypi["command"]

    def test_the_issuer_is_githubs(self):
        assert OIDC_ISSUER == "https://token.actions.githubusercontent.com"


class TestTheSigningPipelineIsItselfPinned:
    def test_every_action_is_pinned_to_a_commit_sha(self):
        """A floating tag on the action that mints your provenance makes the provenance only as
        trustworthy as whoever can move that tag."""
        floating = []
        for path in sorted(WORKFLOW_DIR.glob("*.yml")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if not stripped.startswith(("uses:", "- uses:")):
                    continue
                ref = stripped.split("uses:", 1)[1].split("#")[0].strip()
                if ref.startswith("./"):
                    continue
                if "@" not in ref or not _SHA.match(ref.split("@")[-1]):
                    floating.append(f"{path.name}:{lineno} {ref}")
        assert not floating, "unpinned action reference(s): " + ", ".join(floating)


class TestAnOperatorCanActuallyRunIt:
    def test_the_cli_has_the_command(self):
        from app import cli
        assert '"provenance"' in inspect.getsource(cli.main)
        assert hasattr(cli, "cmd_provenance")

    def test_the_command_prints_what_is_not_attested_too(self):
        from app import cli
        assert "NOT_ATTESTED" in inspect.getsource(cli.cmd_provenance)

    def test_the_security_doc_carries_the_section(self):
        text = (REPO_ROOT / "v4" / "docs" / "security.md").read_text(encoding="utf-8")
        assert "gh attestation verify" in text
        assert "--signer-workflow" in text
        assert "Supply chain" in text

    def test_the_cli_reference_documents_it(self):
        text = (REPO_ROOT / "v4" / "docs" / "cli-reference.md").read_text(encoding="utf-8")
        assert "kubeintellect provenance" in text


class TestWhatIsNotProvenIsWrittenDown:
    def test_the_module_says_attestation_is_not_reproducibility(self):
        doc = " ".join((supply_chain.__doc__ or "").split())
        assert "not say the same source rebuilds bit-for-bit" in doc
        assert "No release has been signed yet" in doc
        assert "2026-08-28" in doc

    def test_every_unattested_channel_carries_a_dated_reason(self):
        for channel, why in NOT_ATTESTED.items():
            assert why.startswith("2026-"), channel
            assert len(why) > 80, f"{channel}: a one-line reason is not a reason"
