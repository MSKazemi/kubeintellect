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
import sys
from dataclasses import dataclass
from pathlib import Path

# Repo layout: this file is v4/scripts/check_doc_claims.py
_V4 = Path(__file__).resolve().parent.parent
_DOCS = _V4 / "docs"


# ── Canonical values, read straight from the code ────────────────────────────


@dataclass(frozen=True)
class Canonical:
    playbook_count: int
    detector_count: int
    providers: set[str]
    flag_count: int


def _canonical() -> Canonical:
    """Return the authoritative values the docs must agree with."""
    from app.agent.playbooks.loader import list_playbooks  # type: ignore[import-untyped]
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
        if name.startswith("KI_V5_") or name.startswith("CORTEX_V5_")
    )

    return Canonical(
        playbook_count=playbook_count,
        detector_count=detector_count,
        providers=providers,
        flag_count=flag_count,
    )


# ── Checks ───────────────────────────────────────────────────────────────────


def _read(doc: str) -> str:
    return (_DOCS / doc).read_text(encoding="utf-8")


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


def run_checks() -> list[str]:
    c = _canonical()
    errors: list[str] = []

    pc = c.playbook_count
    errors += _check_number(
        "agent-behaviors.md", r"Playbooks shipped \((\d+)\)", pc, "playbook count"
    )
    errors += _check_number(
        "agent-behaviors.md", r"Of the (\d+) shipped playbooks", pc, "playbook count"
    )
    errors += _check_number(
        "capabilities.md", r"the (\d+) most common failures", pc, "playbook count"
    )
    errors += _check_number(
        "capabilities.md", r"\*\*(\d+) built-in playbooks\*\*", pc, "playbook count"
    )
    errors += _check_number(
        "glossary.md", r"(\d+) ship by default", pc, "playbook count"
    )
    errors += _check_number(
        "architecture.md", r"\((\d+) playbooks\)", pc, "playbook count"
    )

    dc = c.detector_count
    errors += _check_number(
        "agent-behaviors.md", r"\*\*(\d+) compile to detectors\*\*", dc, "detector count"
    )
    errors += _check_number(
        "api-reference.md", r'"detectors":\s*(\d+)', dc, "detector count"
    )

    fc = c.flag_count
    errors += _check_number(
        "v5-experimental-flags.md", r"_(\d+) flags", fc, "v5 flag count"
    )

    # Providers: configuration.md documents the pipe-separated valid set.
    providers = c.providers
    config_text = _read("configuration.md")
    prov_match = re.search(r"LLM_PROVIDER=\w+\s+#\s*([\w |]+)", config_text)
    if not prov_match:
        errors.append("FAIL configuration.md: could not find the LLM_PROVIDER options comment")
    else:
        documented = {p.strip() for p in prov_match.group(1).split("|") if p.strip()}
        if documented != providers:
            errors.append(
                f"FAIL configuration.md: providers {sorted(documented)} != "
                f"code {sorted(providers)}"
            )

    return errors


def main() -> int:
    errors = run_checks()
    if errors:
        print("Doc-claims drift detected:\n")
        for e in errors:
            print(f"  {e}")
        print("\nUpdate the docs (or the code) so numbered claims agree.")
        return 1
    print("Doc claims match the code (playbooks, detectors, providers, v5 flags).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
