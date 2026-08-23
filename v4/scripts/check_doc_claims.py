#!/usr/bin/env python3
"""Doc-claims drift guard — verify numbered claims in docs match the code.

Several user docs hard-code counts that are really *derived from code*: the
number of shipped playbooks, the number of compiled detectors, the set of valid
LLM providers, and the number of ``KI_V5_*`` experimental flags. When code
changes (a playbook is added, a provider is enabled, a flag is introduced) these
numbers silently drift.

This script reads the **canonical values from the code** and asserts that each
documented claim still matches. It generates nothing and edits nothing — it only
reports drift and exits non-zero, so it is safe to run in CI (``make docs-check``)
and from a unit test.

Run:  uv run python scripts/check_doc_claims.py
"""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Repo layout: this file is v4/scripts/check_doc_claims.py
_V4 = Path(__file__).resolve().parent.parent
_DOCS = _V4 / "docs"
# Repo-root docs make the same derived claims and drift the same way. ROADMAP.md
# sat three playbooks behind for exactly as long as nothing checked it.
_ROOT = _V4.parent


# ── Canonical values, read straight from the code ────────────────────────────


@dataclass(frozen=True)
class Canonical:
    playbook_count: int
    detector_count: int
    providers: set[str]
    flag_count: int


def _canonical() -> Canonical:
    """Return the authoritative values the docs must agree with."""
    from app.agent.playbooks.loader import (
        list_playbooks,  # type: ignore[import-untyped]
    )
    from app.core.config import Settings  # type: ignore[import-untyped]
    from app.detectors.engine import load_detectors  # type: ignore[import-untyped]

    playbook_count = len(list(list_playbooks()))
    # Baseline compiled detectors (no promoted DB detectors) — this is what the
    # /v1/findings endpoint reports as "detectors" on a fresh install.
    detector_count = len(load_detectors())

    valid_match = re.search(r"valid\s*=\s*\{([^}]*)\}", inspect.getsource(Settings))
    providers = (
        {p.strip().strip("\"'") for p in valid_match.group(1).split(",") if p.strip()}
        if valid_match
        else set()
    )

    flag_count = sum(
        1
        for name in Settings.model_fields
        if name.startswith(("KI_V5_", "CORTEX_V5_"))
    )

    return Canonical(
        playbook_count=playbook_count,
        detector_count=detector_count,
        providers=providers,
        flag_count=flag_count,
    )


# ── Checks ───────────────────────────────────────────────────────────────────


_ROOT_PREFIX = "root:"


def _read(doc: str) -> str:
    """Read a doc, by default from ``v4/docs/``.

    A name prefixed ``root:`` resolves from the repo root instead, so root-level
    surfaces like ``ROADMAP.md`` can be checked alongside the v4 docs. The prefix
    is explicit on purpose — an implicit fallback would turn a typo in a docs
    filename into a confusing lookup somewhere else.
    """
    if doc.startswith(_ROOT_PREFIX):
        return (_ROOT / doc[len(_ROOT_PREFIX):]).read_text(encoding="utf-8")
    return (_DOCS / doc).read_text(encoding="utf-8")


def _write(doc: str, text: str) -> None:
    """Write a doc back, resolving the name exactly as :func:`_read` does."""
    if doc.startswith(_ROOT_PREFIX):
        (_ROOT / doc[len(_ROOT_PREFIX):]).write_text(text, encoding="utf-8")
    else:
        (_DOCS / doc).write_text(text, encoding="utf-8")


def _collect_count(rootdir: Path) -> int | None:
    """Number of tests pytest *collects* under *rootdir* — collection only, nothing runs.

    Returns ``None`` if collection could not be performed at all (no venv, pytest missing),
    so a developer without the workspace installed still gets the rest of the checks rather
    than a hard crash. A collection *error* is different from an absent pytest and is
    reported by the caller.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/"],
            cwd=rootdir,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+) tests? collected", proc.stdout)
    if not m:
        # pytest prints "N/M tests collected" when deselecting; accept that form too.
        m = re.search(r"(\d+)/\d+ tests? collected", proc.stdout)
    return int(m.group(1)) if m else None


# (doc, pattern, expected, label). Capture group 1 of *pattern* is the number.
_Claim = tuple[str, str, int, str]


def _test_count_claims() -> tuple[list[_Claim], list[str]]:
    """AGENTS.md tells agents how many tests to expect; keep that honest.

    This number drifts silently — every added test makes the doc a little more wrong, and a
    wrong count is worse than none because an agent uses it to decide whether its run was
    complete. It was 990 in the doc while the suite was actually 1031.

    Returns the resolvable claims plus one note per suite that could not be collected, so a
    developer without the workspace installed still gets every other check rather than a
    hard crash. A collection *error* is different from an absent pytest, and only the latter
    is tolerated here.
    """
    claims: list[_Claim] = []
    notes: list[str] = []
    for label, rootdir, pattern in (
        ("server suite", _V4, r"Server suite \((\d+) tests\)"),
        ("kq CLI suite", _V4 / "packages" / "kube-q", r"kq CLI suite \((\d+) tests\)"),
    ):
        actual = _collect_count(rootdir)
        if actual is None:
            notes.append(
                f"SKIP {label}: could not collect tests under {rootdir} "
                "(no venv?) — count not verified"
            )
            continue
        claims.append(("root:AGENTS.md", pattern, actual, f"{label} test count"))
    return claims, notes


def _numeric_claims(c: Canonical) -> list[_Claim]:
    """Every "<doc> says <number>" claim — the auto-fixable subset.

    Held as data, not as a sequence of calls, so ``--check`` and ``--fix`` walk the
    identical list. A claim only one of them knew about is exactly a claim that can drift.
    """
    pc, dc, fc = c.playbook_count, c.detector_count, c.flag_count
    return [
        ("agent-behaviors.md", r"Playbooks shipped \((\d+)\)", pc, "playbook count"),
        ("agent-behaviors.md", r"Of the (\d+) shipped playbooks", pc, "playbook count"),
        ("capabilities.md", r"the (\d+) most common failures", pc, "playbook count"),
        ("capabilities.md", r"\*\*(\d+) built-in playbooks\*\*", pc, "playbook count"),
        ("glossary.md", r"(\d+) ship by default", pc, "playbook count"),
        ("architecture.md", r"\((\d+) playbooks\)", pc, "playbook count"),
        # Repo-root ROADMAP.md — the surface a first-time visitor reads, and the one that
        # had drifted furthest (it claimed 20 when the library was 23) because nothing
        # checked it.
        ("root:ROADMAP.md", r"\*\*(\d+) declarative failure playbooks\*\*", pc, "playbook count"),
        ("root:ROADMAP.md", r"playbook library\*\* beyond (\d+)", pc, "playbook count"),
        ("agent-behaviors.md", r"\*\*(\d+) compile to detectors\*\*", dc, "detector count"),
        ("api-reference.md", r'"detectors":\s*(\d+)', dc, "detector count"),
        ("root:ROADMAP.md", r"(\d+) of which compile", dc, "detector count"),
        ("v5-experimental-flags.md", r"_(\d+) flags", fc, "v5 flag count"),
    ]


def _check_number(doc: str, pattern: str, expected: int, label: str) -> list[str]:
    """Every capture-group-1 number matched by *pattern* in *doc* must equal *expected*."""
    text = _read(doc)
    found = re.findall(pattern, text)
    if not found:
        return [f"FAIL {doc}: no claim matched /{pattern}/ for {label}"]
    errors = []
    for value in found:
        if int(value) != expected:
            errors.append(
                f"FAIL {doc}: {label} says {value}, code says {expected} (/{pattern}/)"
            )
    return errors


def _fix_number(doc: str, pattern: str, expected: int) -> int:
    """Rewrite capture group 1 of every *pattern* match in *doc* to *expected*.

    Only the digits of group 1 are replaced, never the whole match, so surrounding prose
    ("Playbooks shipped (23)") survives verbatim. Returns how many occurrences changed.
    """
    text = _read(doc)
    changed = 0

    def _sub(m: "re.Match[str]") -> str:
        nonlocal changed
        if int(m.group(1)) == expected:
            return m.group(0)
        changed += 1
        whole, base = m.group(0), m.start(0)
        return whole[: m.start(1) - base] + str(expected) + whole[m.end(1) - base :]

    new = re.sub(pattern, _sub, text)
    if changed:
        _write(doc, new)
    return changed


def _check_providers(c: Canonical) -> list[str]:
    """configuration.md documents the pipe-separated valid set. Not auto-fixable: a
    provider appearing or vanishing is a code change a human should look at."""
    config_text = _read("configuration.md")
    prov_match = re.search(r"LLM_PROVIDER=\w+\s+#\s*([\w |]+)", config_text)
    if not prov_match:
        return ["FAIL configuration.md: could not find the LLM_PROVIDER options comment"]
    documented = {p.strip() for p in prov_match.group(1).split("|") if p.strip()}
    if documented != c.providers:
        return [
            f"FAIL configuration.md: providers {sorted(documented)} != "
            f"code {sorted(c.providers)}"
        ]
    return []


def run_checks() -> list[str]:
    c = _canonical()
    errors: list[str] = []
    for doc, pattern, expected, label in _numeric_claims(c):
        errors += _check_number(doc, pattern, expected, label)
    errors += _check_providers(c)
    test_claims, notes = _test_count_claims()
    errors += notes
    for doc, pattern, expected, label in test_claims:
        errors += _check_number(doc, pattern, expected, label)
    return errors


def fix_claims() -> tuple[int, list[str]]:
    """Rewrite every drifted numeric claim to match the code. Returns (count, notes)."""
    c = _canonical()
    test_claims, notes = _test_count_claims()
    fixed = 0
    for doc, pattern, expected, label in _numeric_claims(c) + test_claims:
        n = _fix_number(doc, pattern, expected)
        if n:
            fixed += n
            notes.append(f"fixed {doc}: {label} -> {expected} ({n} occurrence(s))")
    return fixed, notes


def main() -> int:
    if "--fix" in sys.argv[1:]:
        fixed, notes = fix_claims()
        for n in notes:
            print(f"  {n}")
        print(f"\n{fixed} numbered claim(s) rewritten.")
        errors = run_checks()
        if errors:
            print("\nStill drifting — these need a human:\n")
            for e in errors:
                print(f"  {e}")
            return 1
        print("Doc claims now match the code.")
        return 0

    errors = run_checks()
    if errors:
        print("Doc-claims drift detected:\n")
        for e in errors:
            print(f"  {e}")
        print(
            "\nUpdate the docs (or the code) so numbered claims agree — "
            "`make docs-fix` (from v4/) rewrites the numbered ones for you."
        )
        return 1
    print(
        "Doc claims match the code "
        "(playbooks, detectors, providers, v5 flags, test counts)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
