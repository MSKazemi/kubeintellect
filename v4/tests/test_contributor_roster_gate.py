"""Gate: the two contributor rosters name the same people.

Exercises scripts/check-contributor-roster.py, the guard added for #167 — the
rc file listed 5 people, the README credited 8, and @Chris7717 was on neither.

The red cases carry the weight. A roster comparison that can only pass is worth
nothing, and the specific way this one could rot is by reading *nothing* from
one surface and cheerfully comparing two empty sets, so that has a test of its
own.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "check-contributor-roster.py"

_README_TEMPLATE = """\
# Project

### Contributors

| Person | What they did |
| --- | --- |
{rows}

### Something else

| **[@notacontributor](https://github.com/notacontributor)** | a row outside the table |
"""


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_contributor_roster", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _readme(*handles: str) -> str:
    rows = "\n".join(
        f"| **[@{h}](https://github.com/{h})** | did a thing |" for h in handles
    )
    return _README_TEMPLATE.format(rows=rows)


def _rc(*logins: str) -> str:
    return json.dumps({"contributors": [{"login": lg, "contributions": ["code"]} for lg in logins]})


def test_real_rosters_agree():
    """The live surfaces, which is the thing the CI job actually asserts."""
    checker = _load_checker()
    rc = checker.rc_logins((_REPO_ROOT / ".all-contributorsrc").read_text(encoding="utf-8"))
    readme = checker.readme_handles((_REPO_ROOT / "README.md").read_text(encoding="utf-8"))

    # A vacuous comparison would pass for free — this is the failure mode #167
    # is about, so assert both surfaces were actually read.
    assert len(rc) >= 9, f"suspiciously few rc entries: {len(rc)}"
    assert len(readme) >= 9, f"suspiciously few README rows: {len(readme)}"
    assert checker.compare(rc, readme) == []


def test_missing_from_readme_is_reported_by_name():
    checker = _load_checker()
    problems = checker.compare(["alice", "bob"], ["alice"])
    assert len(problems) == 1
    assert "@bob" in problems[0]
    assert "no row in the README table" in problems[0]


def test_missing_from_rc_is_reported_by_name():
    checker = _load_checker()
    problems = checker.compare(["alice"], ["alice", "bob"])
    assert len(problems) == 1
    assert "@bob" in problems[0]
    assert "no .all-contributorsrc entry" in problems[0]


def test_the_historical_drift_would_have_been_caught():
    """The exact 2026-08-22 state: rc had 5, the README had 8, Chris7717 neither."""
    checker = _load_checker()
    rc = ["hariomlohardev", "AdvaitVarhade", "shaurya703", "uuzzrm", "ybayraktarb"]
    readme = [*rc, "floze-the-genius", "Priyanshu608", "AshSgDe29071999"]

    problems = checker.compare(rc, readme)
    assert len(problems) == 3
    assert all("no .all-contributorsrc entry" in p for p in problems)
    # Chris7717 was on neither surface, so no comparison could have found him.
    # That is a limit of this gate, recorded here so nobody assumes otherwise.
    assert not any("Chris7717" in p for p in problems)


def test_an_empty_surface_fails_rather_than_passing_vacuously():
    checker = _load_checker()
    assert checker.compare([], []) != []
    assert any("empty" in p or "no contributors" in p for p in checker.compare([], ["alice"]))
    assert any("guarding nothing" in p for p in checker.compare(["alice"], []))


def test_case_only_drift_is_reported():
    checker = _load_checker()
    problems = checker.compare(["Alice"], ["alice"])
    assert len(problems) == 1
    assert "spelled two ways" in problems[0]


def test_a_person_listed_twice_is_reported():
    checker = _load_checker()
    problems = checker.compare(["alice", "alice"], ["alice"])
    assert any("more than once" in p for p in problems)


def test_only_the_contributors_section_is_read():
    """A table row elsewhere in the README is not a credit row."""
    checker = _load_checker()
    handles = checker.readme_handles(_readme("alice", "bob"))
    assert handles == ["alice", "bob"]


def test_a_renamed_heading_fails_instead_of_reading_nothing():
    checker = _load_checker()
    text = _readme("alice").replace("### Contributors", "### Credits")
    assert checker.readme_handles(text) == []
    assert any("guarding nothing" in p for p in checker.compare(["alice"], []))
