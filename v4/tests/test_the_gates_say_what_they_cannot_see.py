"""T54(b) — the local gate and CI are one gate, and what only CI can see is written down.

`make setup` exists so a contributor can prove their branch before pushing it. That promise is
only as good as the *scope* of the commands it runs, and scope drifts silently: CI widened its
`ruff check` to `packages/kube-q/`, `tests/` and `scripts/` on 2026-08-24, and
`scripts/dev-setup.sh` went on running the two-package form while printing *"Gate 1/8 — ruff
check (this IS the CI lint gate)"*. A setup script that says "lint clean" about a narrower
command than the one that will reject the PR is worse than no setup script — it converts a real
failure into a surprise, and every test file added since that day (this repository adds them
constantly) sat outside the check the author was told they had run.

The second half is the reverse blind spot. `v4/scripts/check_doc_claims.py` recollects every
documented count — the two suite totals, playbooks, detectors, providers, v5 flags, CLI exit
codes — and until 2026-08-28 it ran in **no CI job at all**. Adding one playbook moves six of
those numbers, so the numbers the README, AGENTS.md and the docs assert were enforced purely by
someone remembering. It now rides inside the existing **Lint (ruff)** job: branch protection
matches required checks *by name*, so a new job name would be a check `main` does not require
and every open PR would sit unmergeable until the settings caught up (#167).

What is left is honest rather than fixed: CI runs 10 jobs that expand to 15 named checks, and a
laptop can reproduce six of them. The other nine are enumerated below **with a reason**, because
"all gates green" said about a subset is the same false all-clear this file is about.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
SETUP = ROOT / "scripts" / "dev-setup.sh"
RECORD = ROOT / ".github" / "required-checks.yml"
COMPARATOR = ROOT / "scripts" / "check-required-checks.py"
AGENTS = ROOT / "AGENTS.md"
MAKEFILE = ROOT / "Makefile"

#: How many gates `dev-setup.sh` runs, and therefore what every doc surface must say.
LOCAL_GATES = 9

#: CI check names a laptop cannot reproduce, each with a dated reason. A check that is neither
#: covered locally nor listed here fails the coverage test — silence is the thing being removed.
CI_ONLY: dict[str, str] = {
    "Tests (v2 · frozen)": (
        "2026-08-28: runnable locally, but dev-setup.sh installs only the v4 workspace; v2 and "
        "v3 are frozen generations with their own dependency sets and a contributor changing v4 "
        "has no reason to install them."
    ),
    "Tests (v3 · frozen)": (
        "2026-08-28: same as v2 — a separate frozen workspace with its own dependency set, "
        "which dev-setup.sh does not install."
    ),
    "Tests (server · py3.13)": (
        "2026-08-28: needs a second interpreter. The project block is explicit that a difference "
        "between 3.12 and 3.13 IS the bug, so this is a real gate, not a nicety."
    ),
    "Tests (kube-q CLI · py3.13)": (
        "2026-08-28: needs a second interpreter, as with the server suite above; the two are "
        "separate check names because the two `tests` packages cannot share one invocation."
    ),
    "Tests (server · py3.14)": (
        "2026-08-28: forward-looking and `continue-on-error`, so it cannot block a merge. It is "
        "listed here so a red one is read as advisory rather than chased as a gate failure."
    ),
    "Tests (kube-q CLI · py3.14)": (
        "2026-08-28: forward-looking and `continue-on-error`, as with the server suite above — "
        "listed so a red one is read as advisory rather than chased as a gate failure."
    ),
    "Install smoke test": (
        "2026-08-28: builds the workspace distributions and installs them into a *clean* "
        "environment. Reproducing it on a laptop that already has the editable install proves "
        "nothing — the point is the absence of the dev tree."
    ),
    "Web (lint + build)": "2026-08-28: needs Node and the web lockfile; not part of the Python gate.",
    "Container image (build + serve)": (
        "2026-08-28: needs Docker and a Postgres container. This is the job that would have "
        "caught a published image that could not start, so it is deliberately not optional in "
        "CI — it is only impractical to require of every contributor's laptop."
    ),
}

#: CI check name -> the local command that reproduces it.
LOCALLY_COVERED: dict[str, str] = {
    "Lint (ruff)": "dev-setup gate 1 (+ gate 5, which rides in this CI job)",
    "Types (mypy)": "dev-setup gate 2",
    "Tests (server)": "dev-setup gate 3",
    "Tests (kube-q CLI)": "dev-setup gate 4",
    "File modes": "dev-setup gate 6 / make check-modes",
    "Syntax warnings": "dev-setup gates 7-9 / make check-syntax, check-encoding, check-roster",
}


def _ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _check_names() -> list[str]:
    """Every check name a PR shows, expanding each matrix `include` into its own name."""
    names: list[str] = []
    for job in _ci()["jobs"].values():
        template = job["name"]
        include = ((job.get("strategy") or {}).get("matrix") or {}).get("include")
        if not include:
            names.append(template)
            continue
        for entry in include:
            name = template
            for key, value in entry.items():
                name = name.replace("${{ matrix.%s }}" % key, str(value))
            names.append(name)
    return names


def _ruff_scope(text: str) -> set[str]:
    """The paths a `ruff check` invocation names, from YAML or shell, continuations rejoined."""
    joined = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).replace("\\\n", " ")
    body = joined.split("uv run ruff check", 1)[1]
    body = re.split(r";|\bthen\b|\n\s*\n", body)[0]
    return {tok for tok in body.split() if "/" in tok}


class TestTheLocalGateIsTheCiGate:
    def test_the_ruff_scope_matches_exactly(self):
        """The defect: dev-setup linted two packages while calling itself the CI lint gate."""
        ci_step = next(
            s for s in _ci()["jobs"]["lint"]["steps"] if s.get("name") == "ruff check"
        )
        assert _ruff_scope(ci_step["run"]) == _ruff_scope(SETUP.read_text(encoding="utf-8")), (
            "scripts/dev-setup.sh and ci.yml lint different paths — a green setup run would not "
            "mean a green PR"
        )

    def test_the_scope_still_covers_the_paths_that_were_missing(self):
        ci_step = next(
            s for s in _ci()["jobs"]["lint"]["steps"] if s.get("name") == "ruff check"
        )
        scope = _ruff_scope(ci_step["run"])
        for path in ("packages/kube-q/", "tests/", "scripts/"):
            assert path in scope, f"{path} unlinted again"

    def test_the_doc_claims_checker_runs_in_ci(self):
        """It ran nowhere in CI until 2026-08-28, so every documented count was unenforced."""
        runs = " ".join(
            str(s.get("run") or "")
            for job in _ci()["jobs"].values()
            for s in (job.get("steps") or [])
        )
        assert "check_doc_claims.py" in runs

    def test_it_rides_in_an_existing_job_rather_than_adding_a_required_check(self):
        """A new job name is a check `main` does not require — every open PR would stall (#167)."""
        names = [
            s.get("name")
            for s in _ci()["jobs"]["lint"]["steps"]
            if "check_doc_claims.py" in str(s.get("run") or "")
        ]
        assert names, "the doc-claims step is not in the lint job"

    def test_the_setup_script_runs_it_too(self):
        assert "check_doc_claims.py" in SETUP.read_text(encoding="utf-8")


class TestEveryCiCheckIsCoveredOrWrittenDown:
    def test_the_two_sets_partition_ci(self):
        names = set(_check_names())
        accounted = set(LOCALLY_COVERED) | set(CI_ONLY)
        assert names == accounted, (
            "a CI check is in neither the locally-covered map nor the written-down list: "
            f"{names ^ accounted}"
        )

    @pytest.mark.parametrize("name,reason", sorted(CI_ONLY.items()))
    def test_a_ci_only_check_carries_a_dated_reason(self, name: str, reason: str):
        assert reason.startswith("2026-"), name
        assert len(reason) > 60, f"{name}: a reason, not a label"

    def test_the_advisory_jobs_really_are_advisory(self):
        """Calling py3.14 advisory is only honest while it cannot block a merge."""
        assert _ci()["jobs"]["test-candidate"].get("continue-on-error") is True
        for job in ("test", "test-next"):
            assert not _ci()["jobs"][job].get("continue-on-error"), f"{job} is a real gate"

    def test_the_frozen_suites_are_gated_somewhere(self):
        """v1 is frozen and untested by design; v2/v3 are frozen but still run in CI."""
        assert {"Tests (v2 · frozen)", "Tests (v3 · frozen)"} <= set(_check_names())


class TestTheDocSurfacesAgreeOnHowManyGatesThereAre:
    def test_the_setup_script_numbers_them_consistently(self):
        gates = re.findall(r'say "Gate (\d+)/(\d+) —', SETUP.read_text(encoding="utf-8"))
        assert [int(n) for n, _ in gates] == list(range(1, LOCAL_GATES + 1))
        assert {int(total) for _, total in gates} == {LOCAL_GATES}

    def test_agents_md_and_the_makefile_say_the_same_number(self):
        word = {6: "six", 8: "eight", 9: "nine"}[LOCAL_GATES]
        assert f"all {word}" in AGENTS.read_text(encoding="utf-8")
        assert f"the {word} locally-runnable CI gates" in MAKEFILE.read_text(encoding="utf-8")

    def test_agents_md_lists_every_gate(self):
        text = AGENTS.read_text(encoding="utf-8")
        for n in range(1, LOCAL_GATES + 1):
            assert f"# {n}. " in text, f"gate {n} is not documented in AGENTS.md"


class TestTheRequiredChecksAreRecorded:
    """T54(c). `ci.yml` produces 15 checks and `main` requires 9, and nothing joined the two
    lists: a job could be added, renamed or left out of the required set and every PR would keep
    merging green. Branch protection lives in the repository settings and only an admin can
    change it, so what is enforceable here is that the record and CI cannot drift apart quietly
    — and that a check nobody requires is written down as a decision instead of a silence.
    """

    @pytest.fixture
    def record(self) -> dict:
        return yaml.safe_load(RECORD.read_text(encoding="utf-8"))

    def test_the_record_partitions_ci_exactly(self, record):
        accounted = set(record["required"]) | set(record["not_required"])
        assert accounted == set(_check_names()), (
            "a CI check is required-or-not by accident: " f"{accounted ^ set(_check_names())}"
        )

    @pytest.mark.parametrize("name", sorted(yaml.safe_load(RECORD.read_text(encoding="utf-8"))["not_required"]))
    def test_a_check_nobody_requires_carries_a_dated_reason(self, name: str, record):
        reason = str(record["not_required"][name])
        assert reason.strip().startswith("2026-"), name
        assert len(reason) > 60, f"{name}: a reason, not a label"

    def test_an_undecided_gap_is_marked_as_undecided(self, record):
        """Three of these are not choices anyone made; recording them as choices would be a lie."""
        open_questions = {
            n for n, r in record["not_required"].items() if "NOT a deliberate decision" in str(r)
        }
        assert "Container image (build + serve)" in open_questions, (
            "the only check that proves the published image starts is unrequired; that is a gap, "
            "not a policy"
        )
        assert len(open_questions) >= 3

    def test_the_settings_that_change_what_green_means_are_pinned(self, record):
        """`strict: false` is why two individually green PRs once turned main red."""
        assert set(record["settings"]) == {"strict", "enforce_admins"}
        assert all(isinstance(v, bool) for v in record["settings"].values())

    def test_the_comparator_exists_and_is_runnable(self):
        assert COMPARATOR.exists()
        assert COMPARATOR.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")
        assert COMPARATOR.stat().st_mode & 0o111, "the file-mode gate wants shebang iff executable"

    def test_it_is_not_wired_into_ci(self, record):
        """It needs a repo-scoped token; a workflow reading its own protection to make a report
        would be a credential added for nothing."""
        runs = " ".join(
            str(s.get("run") or "")
            for job in _ci()["jobs"].values()
            for s in (job.get("steps") or [])
        )
        assert "check-required-checks.py" not in runs
        assert "check-required" in (ROOT / "Makefile").read_text(encoding="utf-8")

