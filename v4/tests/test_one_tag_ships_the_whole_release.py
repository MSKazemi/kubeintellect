"""T54 — one `v*` tag ships a *complete* release, or says which channel it does not reach.

A release here is not one artifact. A tag fans out to a container image on two registries, three
PyPI distributions, a Helm chart, four frozen `kq` binaries and, downstream of those, a krew
plugin manifest — published by five independent workflows that never speak to each other. The
failure mode that costs a version is not a red build; it is a release that is *partly* green:
four channels updated, one silently left on the previous version, and a release page that looks
finished.

Until 2026-08-28 the pipeline had exactly that shape, in one place. `release-binaries.yml`
finishes with `gh release upload <tag>`, which requires a release that already exists — and
nothing in CI created one. The written procedure was to push the tag and then run
`gh release create` by hand fast enough to beat the build to its upload step. Lose that race and
the binaries job fails *after* the image, the chart and PyPI have already published, so the
version exists everywhere except the page people download from. Win it, and the release is
published while the archives are still building, which fires `release: released` at krew before
its four assets exist — krew therefore failed on every release by construction and was re-run by
hand afterwards.

The fix is ordering, not a new workflow: create the release as a draft, attach the archives,
publish last. So the claims gated here are:

* every distribution channel is either produced by the tag or written down as manual, with a
  dated reason — and the channel list is the same one the supply-chain module verifies, because
  two independent lists of "where we publish" is how a channel goes quiet unnoticed;
* nothing uploads to a release it did not first ensure exists;
* the release is published only *after* its assets are attached, so the downstream `released`
  consumer sees a complete release;
* a release created by hand is left alone — this job does not get to publish someone's draft;
* krew keeps its own guard anyway, because ordering enforced in one repo is not a proof about
  an event GitHub delivers.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from app.core.supply_chain import NOT_ATTESTED, SIGNED

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

#: PyYAML parses a bare `on:` key as the boolean True (YAML 1.1), so triggers are read from there.
ON = True


@dataclass(frozen=True)
class Channel:
    """One place a user can install KubeIntellect from, and how a release reaches it."""

    key: str
    what: str
    #: `"tag"` — a `v*` tag push runs the workflow that publishes it.
    #: `"release"` — runs on the GitHub release, which the tag now creates and publishes.
    #: `"manual"` — a human must act; `reason` says why that is the honest choice.
    kind: str
    workflow: str | None
    reason: str = ""


CHANNELS: tuple[Channel, ...] = (
    Channel("image", "Container image on GHCR and Docker Hub", "tag", "docker-publish.yml"),
    Channel("pypi", "kubeintellect, kube-q, ki-protocol on PyPI", "tag", "publish.yml"),
    Channel("binaries", "kq_{os}_{arch}.tar.gz on the GitHub release", "tag",
            "release-binaries.yml"),
    Channel(
        "chart",
        "Helm chart, OCI on GHCR — Artifact Hub indexes that same repository on its own schedule,"
        " so it is a listing over this channel rather than a separate artifact",
        "tag",
        "helm-publish.yml",
    ),
    Channel("krew-index", "kubectl kq — PR to kubernetes-sigs/krew-index", "release", "krew.yml"),
    Channel(
        "snap",
        "kubeintellect snap on the Snap Store",
        "manual",
        "snap.yml",
        reason=(
            "2026-08-28: publishing needs a SNAPCRAFT_STORE_CREDENTIALS export, and its publish "
            "job requires an explicit workflow_dispatch channel on main. Firing it from a tag "
            "would add a channel that no-ops whenever that credential has expired — a silent "
            "partial release, which is the failure this file exists to prevent. Every push to "
            "main still builds and smoke-tests the snap, so the store is at most one deliberate "
            "dispatch behind, and the dispatch names the channel it releases to."
        ),
    ),
    Channel(
        "homebrew-tap",
        "brew install from MSKazemi/homebrew-kube-q",
        "manual",
        None,
        reason=(
            "2026-08-28: the tap is a separate repository and nothing here writes to it. The "
            "formula pins the sha256 of a release tarball, so it cannot be bumped before the "
            "release this workflow creates exists. Automating it needs a cross-repo token — a "
            "credential decision, not a wiring one — and it is not held today."
        ),
    ),
    Channel(
        "huggingface-space",
        "the public demo Space",
        "manual",
        None,
        reason=(
            "2026-08-28: Hugging Face builds the Space from its own repository; no artifact of "
            "this release is published there. It is a hosted demo, not an install path, so a "
            "release that does not touch it is not a partial release."
        ),
    ),
)

BY_KEY = {c.key: c for c in CHANNELS}


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _tag_triggered(doc: dict) -> bool:
    push = (doc.get(ON) or {}).get("push") or {}
    return any(t.startswith("v") for t in (push.get("tags") or []))


def _steps(doc: dict, job: str) -> list[dict]:
    return doc["jobs"][job]["steps"]


def _index(steps: list[dict], name: str) -> int:
    for i, step in enumerate(steps):
        if step.get("name") == name:
            return i
    raise AssertionError(f"no step named {name!r}; have {[s.get('name') for s in steps]}")


class TestEveryChannelIsAccountedFor:
    def test_the_channel_list_is_the_supply_chain_list(self):
        """Two lists of where we publish is how a channel goes quiet without anyone noticing."""
        attested = {a.key for a in SIGNED} | set(NOT_ATTESTED)
        assert set(BY_KEY) == attested, (
            "a distribution channel is known to one module and not the other: "
            f"{set(BY_KEY) ^ attested}"
        )

    def test_every_channel_is_tag_produced_or_written_down(self):
        for channel in CHANNELS:
            assert channel.kind in {"tag", "release", "manual"}, channel.key
            if channel.kind == "manual":
                assert channel.reason.startswith("2026-"), (
                    f"{channel.key} is not produced by a tag and carries no dated reason"
                )
                assert len(channel.reason) > 120, f"{channel.key}: a reason, not a label"
            else:
                assert channel.workflow, f"{channel.key} claims to be automatic with no workflow"

    @pytest.mark.parametrize("channel", [c for c in CHANNELS if c.kind == "tag"])
    def test_an_automatic_channel_really_fires_on_a_tag(self, channel: Channel):
        assert _tag_triggered(_workflow(channel.workflow)), (
            f"{channel.workflow} is listed as tag-produced but has no v* tag trigger"
        )

    def test_a_manual_channel_is_not_quietly_tag_triggered(self):
        """A channel documented as manual that *does* fire would make the reason a lie."""
        snap = BY_KEY["snap"]
        assert not _tag_triggered(_workflow(snap.workflow))

    def test_the_downstream_channel_waits_for_the_release_event(self):
        release = (_workflow("krew.yml").get(ON) or {}).get("release") or {}
        assert release.get("types") == ["released"]


class TestNothingUploadsToAReleaseThatMayNotExist:
    def test_every_upload_is_preceded_by_a_create_in_the_same_job(self):
        """The generic form of the defect: `gh release upload` presumes a release."""
        found = 0
        for path in sorted(WORKFLOWS.glob("*.yml")):
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            for job_name, job in (doc.get("jobs") or {}).items():
                runs = [str(s.get("run") or "") for s in (job.get("steps") or [])]
                uploads = [i for i, r in enumerate(runs) if "gh release upload" in r]
                if not uploads:
                    continue
                found += len(uploads)
                creates = [i for i, r in enumerate(runs) if "gh release create" in r]
                assert creates and min(creates) < min(uploads), (
                    f"{path.name}:{job_name} uploads to a release nothing in the job creates"
                )
        assert found, "no `gh release upload` found at all — this test stopped testing anything"

    def test_creating_is_idempotent(self):
        steps = _steps(_workflow("release-binaries.yml"), "release")
        step = steps[_index(steps, "Create the draft release")]
        run = step["run"]
        assert "gh release view" in run, "it must look before it creates"
        assert "created=false" in run and "created=true" in run
        assert step.get("id") == "create"


class TestTheReleaseIsPublishedOnlyWhenItIsComplete:
    @pytest.fixture
    def steps(self) -> list[dict]:
        return _steps(_workflow("release-binaries.yml"), "release")

    def test_draft_then_upload_then_publish(self, steps):
        create = _index(steps, "Create the draft release")
        upload = _index(steps, "Upload to the release")
        publish = _index(steps, "Publish the release")
        assert create < upload < publish, (
            "the order IS the fix: publishing before the assets are attached fires `released` "
            "at krew with nothing to hash"
        )

    def test_it_is_created_as_a_draft(self, steps):
        assert "--draft" in steps[_index(steps, "Create the draft release")]["run"]

    def test_publishing_undrafts_that_same_tag(self, steps):
        run = steps[_index(steps, "Publish the release")]["run"]
        assert "--draft=false" in run
        assert "GITHUB_REF_NAME" in run

    def test_a_release_made_by_hand_is_left_alone(self, steps):
        """An existing release may be a deliberate draft or prerelease. Not this job's call."""
        assert steps[_index(steps, "Publish the release")]["if"] == (
            "steps.create.outputs.created == 'true'"
        )

    def test_the_attestation_still_covers_the_uploaded_archives(self, steps):
        """A13's guarantee must not be reordered out of the pipeline by this change."""
        assert _index(steps, "Attest the binaries") < _index(steps, "Upload to the release")

    def test_the_job_can_write_releases(self):
        perms = _workflow("release-binaries.yml")["jobs"]["release"]["permissions"]
        assert perms.get("contents") == "write"


class TestTheDownstreamGuardSurvives:
    def test_krew_still_checks_all_four_archives(self):
        run = "\n".join(
            str(s.get("run") or "") for s in _steps(_workflow("krew.yml"), "submit")
        )
        for asset in ("kq_linux_amd64", "kq_linux_arm64", "kq_darwin_amd64", "kq_darwin_arm64"):
            assert f"{asset}.tar.gz" in run
        assert "::error::" in run, "the guard must fail legibly, not just exit non-zero"

    def test_the_ordering_is_written_down_where_it_is_relied_on(self):
        """The consumer of an event has to be told what now guarantees its ordering."""
        header = (WORKFLOWS / "krew.yml").read_text(encoding="utf-8")
        assert "publishes it last" in header
        assert "release-binaries.yml" in header
