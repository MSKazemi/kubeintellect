"""Gate: numbered doc claims stay in sync with the code.

Loads scripts/check_doc_claims.py and asserts it finds no drift. If someone adds
a playbook, enables a provider, or introduces a KI_V5_* flag without updating the
docs, this test fails with the exact mismatch.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_doc_claims.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _rel(path: Path) -> str:
    """Repo-relative when it can be — a planted temp file in a test is not under the root."""
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_doc_claims", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass can resolve cls.__module__.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_doc_claims_match_code():
    checker = _load_checker()
    errors = checker.run_checks()
    assert errors == [], "Doc-claims drift:\n" + "\n".join(errors)


def test_canonical_values_are_sane():
    """Guard the extractor itself — if these go to 0 the checker is silently broken."""
    checker = _load_checker()
    c = checker._canonical()
    assert c.playbook_count >= 18
    assert c.detector_count >= 1
    assert {"openai", "azure", "qwen", "anthropic"} <= c.providers
    assert c.flag_count >= 40


def test_fix_rewrites_only_the_digits(tmp_path, monkeypatch):
    """`--fix` must replace capture group 1 and nothing else.

    The naive implementation substitutes the whole match and silently eats the prose
    around the number, which would turn a drifted count into a mangled sentence.
    """
    checker = _load_checker()
    (tmp_path / "sample.md").write_text(
        "KubeIntellect ships **23 built-in playbooks** — deterministic.\n", encoding="utf-8"
    )
    monkeypatch.setattr(checker, "_DOCS", tmp_path)

    changed = checker._fix_number("sample.md", r"\*\*(\d+) built-in playbooks\*\*", 24)

    assert changed == 1
    assert (tmp_path / "sample.md").read_text(encoding="utf-8") == (
        "KubeIntellect ships **24 built-in playbooks** — deterministic.\n"
    )


def test_fix_is_idempotent_and_does_not_touch_an_already_correct_doc(tmp_path, monkeypatch):
    checker = _load_checker()
    doc = tmp_path / "sample.md"
    doc.write_text("ships **24 built-in playbooks** here.\n", encoding="utf-8")
    monkeypatch.setattr(checker, "_DOCS", tmp_path)
    before = doc.read_text(encoding="utf-8")

    changed = checker._fix_number("sample.md", r"\*\*(\d+) built-in playbooks\*\*", 24)

    assert changed == 0
    assert doc.read_text(encoding="utf-8") == before


def test_every_claim_pattern_still_matches_its_doc():
    """Guard the guard: a claim whose regex matches nothing has silently stopped guarding.

    Renaming a heading or rewording a sentence is enough to orphan a pattern, and a gate
    that checks zero occurrences passes forever without ever being wrong out loud.
    """
    import re

    checker = _load_checker()
    claims = checker._numeric_claims(checker._canonical())
    assert len(claims) >= 12, "claim table shrank — a doc surface stopped being guarded"

    dead = [
        f"{doc} /{pattern}/ ({label})"
        for doc, pattern, _expected, label in claims
        if not re.search(pattern, checker._read(doc))
    ]
    assert dead == [], "claim patterns that match nothing:\n" + "\n".join(dead)


class TestTheCountedSuiteIsTheOneThatShips:
    """The number this gate writes into AGENTS.md is read by people who cloned the repo.

    It was derived by collecting whatever `tests/` held on the machine running the gate.
    This tree carries test files that `.git/info/exclude` deliberately keeps private, plus
    anything not yet `git add`ed, so the published number was one no clone could reproduce
    and the gate failed for a contributor on a file they had never touched.
    """

    def test_a_file_the_repository_does_not_carry_is_not_counted(self, tmp_path):
        checker = _load_checker()
        root = Path(__file__).resolve().parent.parent
        phantom = root / "tests" / "test_zz_guard_phantom.py"
        phantom.write_text("def test_x(): pass\n", encoding="utf-8")
        try:
            on_disk_only = checker._files_on_disk_only(root, "tests", "test_*.py")
        finally:
            phantom.unlink(missing_ok=True)
        assert phantom in on_disk_only, (
            "an untracked test file was treated as part of the repository — the count this "
            "gate publishes would move with a stray file in someone's working directory"
        )

    def test_a_tracked_file_is_always_counted(self):
        checker = _load_checker()
        root = Path(__file__).resolve().parent.parent
        on_disk_only = checker._files_on_disk_only(root, "tests", "test_*.py")
        assert Path(__file__).resolve() not in on_disk_only, (
            "a tracked test file was about to be excluded from the count"
        )

    def test_an_unanswerable_question_counts_everything_rather_than_nothing(self, tmp_path):
        """No git, no index, no checkout — fall back to the full suite, never to zero.

        Returning "everything is untracked" here would make the collector ignore every file
        and report a confident 0: the vacuity this repo keeps re-learning.
        """
        checker = _load_checker()
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
        assert checker._files_on_disk_only(tmp_path, "tests", "test_*.py") == []


class TestTheFormatDriftNumberIsMeasured:
    """`make lint` runs `ruff format --check`; CI does not, and AGENTS.md sizes the gap.

    It said `~108` while the real figure was 116 — an ungated number in a file whose own policy
    is that derived numbers are gated. It is now derived, so it heals with `make docs-fix`.
    """

    def test_the_number_in_the_doc_is_the_measured_one(self):
        checker = _load_checker()
        measured = checker._format_drift_count()
        assert measured is not None, "ruff could not run — the claim would go unverified"
        agents = (Path(__file__).resolve().parents[2] / "AGENTS.md").read_text(encoding="utf-8")
        assert f"would reformat **{measured}** files" in agents

    def test_a_file_the_repository_does_not_carry_cannot_move_it(self):
        """The same property the suite count needed: a number in a doc describes the repo."""
        checker = _load_checker()
        root = Path(__file__).resolve().parent.parent
        phantom = root / "packages" / "ki-protocol" / "ki_protocol" / "zz_guard_phantom.py"
        before = checker._format_drift_count()
        phantom.write_text("x = {  'deliberately' :'unformatted'}\n", encoding="utf-8")
        try:
            after = checker._format_drift_count()
        finally:
            phantom.unlink(missing_ok=True)
        assert after == before, (
            "an untracked file changed the drift count published in AGENTS.md"
        )

    def test_an_unmeasurable_count_is_skipped_not_guessed(self, monkeypatch):
        """No ruff is "unknown", never "zero files would be reformatted".

        The first draft of this test patched `subprocess.run` outright — which also broke the
        `git ls-files` call that runs *first*, so the function returned early and the test passed
        without ever reaching the branch it claims to cover. Fail only the ruff invocation.
        """
        checker = _load_checker()
        real_run = checker.subprocess.run
        seen: list[bool] = []

        def _ruff_is_missing(cmd, *args, **kwargs):
            if any("ruff" in str(part) for part in cmd):
                seen.append(True)
                raise OSError("ruff is not available here")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(checker.subprocess, "run", _ruff_is_missing)
        assert checker._format_drift_count() is None
        assert seen, "the ruff branch was never reached — this test proves nothing"


class TestEveryCountClaimIsCovered:
    """The claim table is a list someone maintains; the docs are a population that grows.

    `check_doc_claims.py` holds `(doc, regex)` pairs, so it checks the claims somebody thought to
    add — not the claims that exist. Measured 2026-08-24 across 40 tracked docs: `faq.md` said
    **18 deterministic playbooks** on the "why not just use ChatGPT?" page while the library had
    reached 23, and `examples.md` stated the count correctly but unguarded. Neither file appeared
    anywhere in the checker.

    This test walks the population instead of a list, so a new count claim in any doc is checked
    from the moment it is written. It is deliberately a *different shape* of check from the one it
    backs up: a table entry can go missing, a population scan cannot.
    """

    # A changelog states what was true at a release. Rewriting those numbers would falsify
    # history, and `--fix` must never touch them — so they are excluded here by name, with the
    # reason, rather than by a pattern that would silently grow to cover live docs too.
    _HISTORICAL = {"changelog.md"}

    _COUNT_PATTERNS = (
        (re.compile(r"\b(\d{1,3})\s+(?:\w+[- ]){0,3}playbooks?\b"), "playbook"),
        (re.compile(r"playbooks?\s*\((\d{1,3})\)"), "playbook"),
        (re.compile(r"\b(\d{1,3})\s+(?:\w+[- ]){0,3}detectors?\b"), "detector"),
        (re.compile(r'"detectors":\s*(\d{1,3})'), "detector"),
    )

    @staticmethod
    def _population() -> list[Path]:
        """Tracked docs only — an untracked scratch file is not a surface anyone reads."""
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", "v4/docs/*.md", "ROADMAP.md", "README.md"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        return [_REPO_ROOT / name for name in proc.stdout.split("\0") if name]

    def _claims(self):
        canonical = _load_checker()._canonical()
        expected = {"playbook": canonical.playbook_count,
                    "detector": canonical.detector_count}
        for path in self._population():
            if path.name in self._HISTORICAL:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for pattern, kind in self._COUNT_PATTERNS:
                    for match in pattern.finditer(line):
                        yield path, lineno, kind, int(match.group(1)), expected[kind], line.strip()

    def test_no_doc_states_a_count_the_code_disagrees_with(self):
        wrong = [
            f"{_rel(path)}:{lineno} — {kind} count says {got}, code says {exp}\n"
            f"    {line[:120]}"
            for path, lineno, kind, got, exp, line in self._claims()
            if got != exp
        ]
        assert not wrong, (
            "A doc states a count the code disagrees with. If the checker does not know about it, "
            "add it to `_numeric_claims` so `make docs-fix` can heal it:\n  " + "\n  ".join(wrong)
        )

    def test_the_scan_reaches_the_docs_it_claims_to(self):
        """Vacuity guard: an empty population would make the assertion above trivially true."""
        population = self._population()
        assert len(population) >= 30, f"only {len(population)} docs scanned"
        names = {p.name for p in population}
        assert {"faq.md", "examples.md", "capabilities.md"} <= names

    def test_the_scan_actually_finds_claims(self):
        """And a population with no claims in it would be just as vacuous."""
        found = list(self._claims())
        assert len(found) >= 5, f"only {len(found)} count claims found across the docs"

    def test_a_wrong_count_in_a_live_doc_is_caught(self, tmp_path, monkeypatch):
        """Red-green against a new input rather than a revert: plant one and prove it fails."""
        planted = tmp_path / "faq.md"
        planted.write_text("KubeIntellect ships **7 deterministic playbooks** today.\n", encoding="utf-8")
        monkeypatch.setattr(type(self), "_population", staticmethod(lambda: [planted]))
        with pytest.raises(AssertionError, match="playbook count says 7"):
            self.test_no_doc_states_a_count_the_code_disagrees_with()

    def test_the_changelog_exclusion_is_narrow(self):
        """`changelog.md` is excluded by name. A pattern would eventually swallow a live doc."""
        assert self._HISTORICAL == {"changelog.md"}
        assert all(name.endswith(".md") for name in self._HISTORICAL)


class TestAPinnedImageTagIsAClaimToo:
    """`docker pull ghcr.io/…:2.3.1` is an instruction, and nothing checked it.

    Measured 2026-08-29 by pulling every tag the docs name. The README handed
    newcomers `2.2.0` — three releases old, and its own `/healthz` answers
    `"version":"0+unknown"` — while `deploy/alibaba.md` pinned `2.3.1` four times,
    an image that cannot start at all:

        $ docker run --rm --entrypoint python \
              ghcr.io/mskazemi/kubeintellect:2.3.1 -c "import uvicorn"
        ModuleNotFoundError: No module named 'uvicorn'

    That is #158, fixed 08-21 — after every image those tags point at was built.
    `2.4.1` starts and answers `/healthz` in 10 s. A pinned tag goes stale silently
    and by default, which is precisely the shape of claim this gate exists for.
    """

    def test_the_tree_reports_a_version_to_check_against(self):
        mod = _load_checker()
        version = mod._current_version()
        assert version is not None, "the server pyproject no longer yields a version"
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), version

    def test_no_documented_image_tag_is_stale(self):
        mod = _load_checker()
        assert mod._check_image_tags() == []

    def test_the_check_is_wired_into_the_gate(self):
        # A check nothing calls is a check that does not exist.
        source = _SCRIPT.read_text(encoding="utf-8")
        run_checks = source[source.index("def run_checks("):]
        assert "_check_image_tags()" in run_checks[:run_checks.index("\n\n\ndef ")]

    def test_a_stale_tag_in_a_live_doc_is_caught(self, tmp_path, monkeypatch):
        mod = _load_checker()
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "planted.md").write_text(
            "docker pull ghcr.io/mskazemi/kubeintellect:1.0.0\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("nothing pinned here\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_DOCS", docs)
        monkeypatch.setattr(mod, "_ROOT", tmp_path)
        monkeypatch.setattr(mod, "_V4", tmp_path / "absent")
        monkeypatch.setattr(mod, "_current_version", lambda: "2.4.1")

        errors = mod._check_image_tags()
        assert len(errors) == 1, errors
        assert "1.0.0" in errors[0] and "2.4.1" in errors[0]

    def test_the_chart_tag_is_checked_as_well_as_the_image(self, tmp_path, monkeypatch):
        # The chart is published to a different path under the same registry, and
        # `security.md` pins it in a `gh attestation verify` example a reader runs.
        mod = _load_checker()
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "planted.md").write_text(
            "oci://ghcr.io/mskazemi/charts/kubeintellect:1.0.0\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_DOCS", docs)
        monkeypatch.setattr(mod, "_ROOT", tmp_path)
        monkeypatch.setattr(mod, "_V4", tmp_path / "absent")
        monkeypatch.setattr(mod, "_current_version", lambda: "2.4.1")

        assert len(mod._check_image_tags()) == 1

    def test_the_changelog_keeps_its_history(self, tmp_path, monkeypatch):
        # History is supposed to name the version it happened to.
        mod = _load_checker()
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "CHANGELOG.md").write_text(
            "shipped ghcr.io/mskazemi/kubeintellect:1.0.0\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_DOCS", docs)
        monkeypatch.setattr(mod, "_ROOT", tmp_path)
        monkeypatch.setattr(mod, "_V4", tmp_path / "absent")
        monkeypatch.setattr(mod, "_current_version", lambda: "2.4.1")

        assert mod._check_image_tags() == []
