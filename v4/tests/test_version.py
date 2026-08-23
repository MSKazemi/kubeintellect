"""Version identity (ADR-019) — arm + semver + active experimental flags, and /healthz."""
from __future__ import annotations

from app.core import version as ver
from app.core.config import settings


def test_code_version_matches_package():
    v = ver.code_version()
    # importlib returns the installed pyproject version; must be a dotted SemVer, not the stale literal.
    assert v.count(".") >= 1 and v != "2.0.0"


def test_arm_is_ki_version():
    assert ver.arm() == settings.KI_VERSION


class TestActiveFlags:
    def test_all_off_is_empty(self, mocker):
        mocker.patch.object(settings, "CORTEX_V5_ENABLED", False)
        mocker.patch.object(settings, "KI_V5_HARNESS_FANOUT", False)
        mocker.patch.object(settings, "KI_V5_VERIFY_LADDER", False)
        # nothing experimental on ⇒ baseline (only asserts the ones we toggle are absent)
        flags = ver.active_experimental_flags()
        assert "CORTEX_V5_ENABLED" not in flags
        assert "KI_V5_HARNESS_FANOUT" not in flags

    def test_reports_only_true_bools(self, mocker):
        mocker.patch.object(settings, "CORTEX_V5_ENABLED", True)
        mocker.patch.object(settings, "KI_V5_HARNESS_FANOUT", True)
        mocker.patch.object(settings, "KI_V5_VERIFY_LADDER", False)
        flags = ver.active_experimental_flags()
        assert "CORTEX_V5_ENABLED" in flags
        assert "KI_V5_HARNESS_FANOUT" in flags
        assert "KI_V5_VERIFY_LADDER" not in flags        # off
        # tuning knobs (ints/floats/strings) are never reported as flags
        assert "KI_V5_HARNESS_MAX_SUBAGENTS" not in flags
        assert "KI_V5_RESPONDER_LEVEL" not in flags
        assert flags == sorted(flags)                    # deterministic ordering


class TestVersionInfoAndLine:
    def test_version_info_has_all_axes(self, mocker):
        mocker.patch.object(settings, "CORTEX_V5_ENABLED", True)
        info = ver.version_info()
        assert set(info) == {"arm", "semver", "experimental_flags", "set_but_unwired_flags"}
        assert "CORTEX_V5_ENABLED" in info["experimental_flags"]

    def test_line_baseline_vs_flagged(self, mocker):
        mocker.patch.object(settings, "CORTEX_V5_ENABLED", False)
        mocker.patch.object(settings, "KI_V5_HARNESS_FANOUT", False)
        mocker.patch.object(settings, "KI_V5_VERIFY_LADDER", False)
        mocker.patch.object(settings, "KI_V5_RESPONSIVENESS", False)
        mocker.patch.object(settings, "KI_V5_ESCALATION_BRIEFS", False)
        mocker.patch.object(settings, "KI_V5_RUNBOOK_SKILLS", False)
        # (other MEMORY_* may default on; only assert format, not emptiness)
        line = ver.version_line()
        assert line.startswith("KubeIntellect ") and "(" in line and "flags:" in line


def test_healthz_reports_version_identity():
    from app.api.v1.endpoints.health import router
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    app = FastAPI()
    app.include_router(router)
    body = TestClient(app).get("/healthz").json()
    assert body["status"] == "ok"
    assert body["version"] == ver.code_version() and body["version"] != "2.0.0"
    assert "arm" in body and isinstance(body["experimental_flags"], list)


class TestVersionInAContainer:
    """The published container could not say which release it was.

    The image installs dependencies only (`uv sync --no-install-workspace`) and copies the three
    module trees in flat, so there is no dist-info for `kubeintellect` anywhere in it. That is a
    deliberate design — modules resolve from the working directory — but it means
    `importlib.metadata.version()` raises and `code_version()` fell back to the constant
    `"0+unknown"`. `/healthz` therefore reported `0+unknown` on every published image, while this
    module's own docstring calls that field the thing that distinguishes v4.1 from v4.2.

    `docker-publish.yml` already passes `VERSION=$(git describe --tags --always)` as a build arg;
    it was only ever spent on an OCI label. The Dockerfile now also exports it as
    `KI_BUILD_VERSION`, so the degraded branch has a real answer instead of a placeholder.
    """

    def test_falls_back_to_the_build_stamp_when_not_installed(self, mocker):
        mocker.patch.object(ver, "_pkg_version", side_effect=ver.PackageNotFoundError)
        mocker.patch.object(settings, "KI_BUILD_VERSION", "v2.3.2")
        # the leading `v` is git-tag syntax, not part of the version
        assert ver.code_version() == "2.3.2"

    def test_still_reports_unknown_when_there_is_no_stamp_either(self, mocker):
        mocker.patch.object(ver, "_pkg_version", side_effect=ver.PackageNotFoundError)
        mocker.patch.object(settings, "KI_BUILD_VERSION", "")
        assert ver.code_version() == "0+unknown"

    def test_the_installed_package_still_wins_over_the_stamp(self, mocker):
        """The stamp is a fallback, never an override: a real install is the authority."""
        mocker.patch.object(ver, "_pkg_version", return_value="9.9.9")
        mocker.patch.object(settings, "KI_BUILD_VERSION", "v1.1.1")
        assert ver.code_version() == "9.9.9"
