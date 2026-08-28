"""Supply-chain provenance — what a user can prove about the artifact they installed (A13).

Until 2026-08-28 a KubeIntellect release shipped four kinds of artifact — a container image to
two registries, three PyPI distributions, a Helm chart and frozen `kq` binaries — and **none of
them carried anything an installer could check.** The release-binaries job wrote a `checksums.txt`
and uploaded it to the same GitHub release page as the tarballs it checksums, which is exactly the
protection it is not: it detects a corrupted download, and detects nothing about anyone who could
replace the tarball, because they could replace the checksums in the same breath.

So each publishing workflow now emits a **signed, keyless build attestation** (sigstore, GitHub
OIDC, logged to a transparency log) binding the artifact's digest to the commit, workflow and run
that produced it. Nothing in this module signs anything — signing happens in CI, where the OIDC
token exists. What lives here is the other half, and the half that was missing even in principle:
**the identity a verifier must pin to**, and the exact command that pins to it.

Why that belongs in the product rather than only in a doc page. A verification command is worth
only as much as the identity it names, and the identity is a *file path*: rename
`docker-publish.yml` and every published `--signer-workflow …` line silently names a workflow that
does not exist. Worse, dropping `--signer-workflow` still "verifies" — it accepts an attestation
from **any** workflow in the repository, so a careless or compromised job elsewhere in the same
repo would satisfy it. The commands are therefore generated from the same constants the workflows
are named by, and a test compares those constants against the workflow files on disk.

⚠️ **Why A13 is not green, dated 2026-08-28.**

1. *Attestation is not reproducibility.* A signed provenance says which run built the artifact and
   from which commit; it does not say the same source rebuilds bit-for-bit. Nothing here is a
   reproducible build, and `PyInstaller` output in particular is not byte-stable.
2. *No release has been signed yet.* These steps run on the next `v*` tag. Until one is pushed the
   commands below are correct and unexercised, and every artifact published to date carries no
   attestation at all — verifying an existing release will correctly fail to find one.
3. *No dependency-level provenance.* The SBOM lists what is in the image; nothing checks that each
   of those components was itself signed by whoever published it.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The one repository allowed to publish. Every workflow carries the matching
#: `if: github.repository == …` guard, because the private superset tracks the same `.github/`
#: tree and would otherwise mint a second, divergent set of signed artifacts.
REPO = "MSKazemi/kubeintellect"

#: GitHub's OIDC issuer. A verifier that does not pin this accepts a certificate from any issuer
#: the public Fulcio root trusts.
OIDC_ISSUER = "https://token.actions.githubusercontent.com"

GHCR_IMAGE = "ghcr.io/mskazemi/kubeintellect"
DOCKERHUB_IMAGE = "docker.io/kazemi/kubeintellect"
HELM_CHART = "ghcr.io/mskazemi/charts/kubeintellect"

#: The SPDX predicate type used by the SBOM attestation, so a verifier can ask for the SBOM
#: specifically rather than accepting whichever attestation happens to come back first.
SPDX_PREDICATE = "https://spdx.dev/Document"


@dataclass(frozen=True)
class SignedArtifact:
    """One class of released artifact and the workflow whose identity signs it."""

    key: str
    what: str
    #: Repo-relative path. This string IS half the verification identity — a test asserts the file
    #: exists and that it really contains an attestation step.
    workflow: str
    #: `{version}` is the tag without its leading `v`; `{repo_url}` is the https repo URL.
    command: str
    proves: str


SIGNED: tuple[SignedArtifact, ...] = (
    SignedArtifact(
        key="image",
        what="Container image — GHCR and Docker Hub (one build, one digest, pushed twice)",
        workflow=".github/workflows/docker-publish.yml",
        command=(
            f"gh attestation verify oci://{GHCR_IMAGE}:{{version}} --repo {REPO} \\\n"
            f"    --signer-workflow {REPO}/.github/workflows/docker-publish.yml\n"
            f"# and its SBOM, which is generated FROM the built image, not asserted about it:\n"
            f"gh attestation verify oci://{GHCR_IMAGE}:{{version}} --repo {REPO} \\\n"
            f"    --signer-workflow {REPO}/.github/workflows/docker-publish.yml \\\n"
            f"    --predicate-type {SPDX_PREDICATE}"
        ),
        proves=(
            "this exact image digest was built by that workflow from that commit. The Docker Hub "
            "copy is the same digest, so the same attestation covers it — substitute the "
            f"{DOCKERHUB_IMAGE} reference and the check still passes."
        ),
    ),
    SignedArtifact(
        key="pypi",
        what="PyPI distributions — kubeintellect, kube-q, ki-protocol",
        workflow=".github/workflows/publish.yml",
        command=(
            "pypi-attestations verify pypi \\\n"
            f"    --repository https://github.com/{REPO} \\\n"
            "    pypi:kubeintellect-{version}-py3-none-any.whl"
        ),
        proves=(
            "the wheel on PyPI was built and uploaded by that workflow (PEP 740). PyPI stores the "
            "attestation itself and shows it under the release's Verified details — `pip install` "
            "does not check it, so this is a deliberate step, not something you get for free."
        ),
    ),
    SignedArtifact(
        key="binaries",
        what="Frozen `kq` binaries attached to the GitHub release",
        workflow=".github/workflows/release-binaries.yml",
        command=(
            f"gh attestation verify kq_linux_amd64.tar.gz --repo {REPO} \\\n"
            f"    --signer-workflow {REPO}/.github/workflows/release-binaries.yml"
        ),
        proves=(
            "the tarball you downloaded is the one that build produced — which `checksums.txt` "
            "cannot tell you, because it is published on the same page by the same writer that "
            "would be replacing the tarball."
        ),
    ),
    SignedArtifact(
        key="chart",
        what="Helm chart — OCI, GHCR",
        workflow=".github/workflows/helm-publish.yml",
        command=(
            f"gh attestation verify oci://{HELM_CHART}:{{version}} --repo {REPO} \\\n"
            f"    --signer-workflow {REPO}/.github/workflows/helm-publish.yml"
        ),
        proves=(
            "the chart digest a cluster is about to install came from that workflow. The chart is "
            "the enterprise install path, so leaving it unsigned would have meant signing "
            "everything except the thing most operators actually run."
        ),
    ),
)

#: Distribution channels this project publishes to and does NOT attest, each with a dated reason.
#: An unexplained gap is what an attacker looks for, so a gap is either written down or closed.
NOT_ATTESTED: dict[str, str] = {
    "snap": (
        "2026-08-28: the Snap Store signs and distributes snaps under its own key and revision "
        "chain, and the build is uploaded to the store rather than published as a digest this "
        "workflow holds. A second, GitHub-issued attestation over those bytes would add a "
        "signature nobody checks rather than a guarantee — the store's review and revision "
        "history is the verification path on that channel."
    ),
    "homebrew-tap": (
        "2026-08-28: the tap formula points at the GitHub release tarball and pins its sha256, so "
        "what it installs is exactly the artifact release-binaries.yml attests. The formula is a "
        "recipe, not a build output; attesting it would attest the pointer, not the thing pointed "
        "at. `brew install` checks no attestation, so the honest statement is that tap users get "
        "checksum integrity, and the provenance lives on the release the formula resolves to."
    ),
    "krew-index": (
        "2026-08-28: the krew plugin manifest is submitted to the upstream krew-index repository, "
        "which is not ours to attest and whose own review is the control. It resolves to the same "
        "release tarballs, which are attested; the manifest is only a pointer to them."
    ),
    "huggingface-space": (
        "2026-08-28: the public demo Space is built by Hugging Face from a Dockerfile in their "
        "own runner, so no artifact of ours is produced or published there. It is a hosted demo "
        "with a readonly key, not a distribution channel — nothing is installed from it."
    ),
}


#: Publishing workflows that deliberately do NOT attest, mapped to the :data:`NOT_ATTESTED` entry
#: that explains it. Every workflow which fires on a `v*` tag must appear either here or as a
#: :data:`SIGNED` entry — a test enumerates the workflow directory and fails on anything in
#: neither, so adding a distribution channel forces the decision instead of inheriting silence.
UNATTESTED_WORKFLOWS: dict[str, str] = {
    ".github/workflows/snap.yml": "snap",
    ".github/workflows/krew.yml": "krew-index",
}


#: The first release tag whose build actually ran the attest steps — ``None`` while no release
#: has been signed at all.
#:
#: This exists because the claim `kubeintellect provenance` makes to a user has to be *derived*
#: from a recorded fact rather than written into a print statement, where nothing can check it and
#: it does not age. It did not age: the command opened with "Each artifact carries a keyless
#: sigstore attestation", unconditionally, while the attest steps were added *after* `v2.3.1` was
#: tagged. The four publishing workflows ran on that tag and succeeded — they simply had no attest
#: step yet — so `git show v2.3.1:.github/workflows/docker-publish.yml | grep -c attest` is `0`,
#: and GitHub's attestations API 404s the subresource for this repository where a repo that has
#: published one returns `{"attestations": []}`. Every command the CLI printed would fail.
#:
#: Set this to the first `v*` tag pushed after the attesting workflows exist. A test asserts the
#: current value, so flipping it is a deliberate act taken against a release that really is signed.
FIRST_ATTESTED_TAG: str | None = None


def _version_tuple(tag: str) -> tuple[int, ...]:
    """`v2.10.0` → `(2, 10, 0)`.

    Numeric, because as strings `"v2.10.0" < "v2.9.0"` — which would quietly report a signed
    release as unsigned for every minor version past the ninth.
    """
    core = tag.removeprefix("v").split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for piece in core.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def attestation_expected(tag: str) -> bool:
    """Whether a release at `tag` is expected to carry build attestations.

    False is the honest answer for every tag until the first signed release exists, and remains
    the honest answer for every tag published before it.
    """
    if FIRST_ATTESTED_TAG is None:
        return False
    return _version_tuple(tag) >= _version_tuple(FIRST_ATTESTED_TAG)


def signer_identity(workflow: str, tag: str) -> str:
    """The sigstore certificate SAN a release from `tag` will carry.

    `refs/tags/<tag>` is part of the identity, so an attestation minted by the same workflow on a
    branch push cannot satisfy a check written for a tag.
    """
    return f"https://github.com/{REPO}/{workflow}@refs/tags/{tag}"


def signer_workflow(workflow: str) -> str:
    """The `--signer-workflow` value for `gh attestation verify` (path, no ref)."""
    return f"{REPO}/{workflow}"


def verify_commands(tag: str) -> list[dict[str, str]]:
    """The exact commands a user runs to check one release, one entry per artifact class.

    `tag` is the release tag (`v2.3.1`); artifact references drop the leading `v`. That is a small
    difference and exactly the kind that makes a copied command fail for a reason nobody enjoys
    debugging, so it is applied here once rather than left to the reader.
    """
    version = tag.removeprefix("v")
    return [
        {
            "key": art.key,
            "what": art.what,
            "workflow": art.workflow,
            "command": art.command.format(version=version),
            "proves": art.proves,
            "identity": signer_identity(art.workflow, tag),
        }
        for art in SIGNED
    ]
