#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# check-contributor-roster.py — the two contributor rosters name the same people.
#
# WHY THIS EXISTS
#
# Recognition lives on TWO surfaces in this repo:
#
#   * .all-contributorsrc — machine-readable, the All Contributors spec
#   * the "### Contributors" table in README.md — the one humans actually read
#
# Nothing compared them, and by 2026-08-22 they disagreed: the rc file listed 5
# people, the README credited 8, and @Chris7717 was on NEITHER despite a merged
# playbook (#114) and the report (#115) that found the .gitignore glob keeping
# .env.example out of every clone.
#
# That failed quietly for weeks, and quietly is the whole problem. Prose credit
# is what people see, so missing rc entries are invisible; and a person missing
# from both surfaces is invisible everywhere. A contributor never learns they
# were dropped — they just are. See #167.
#
# Like check-syntax-warnings.py, check-file-modes.sh and check-text-encoding.py,
# this is deliberately stdlib only: no virtualenv, no network, and independent
# of ruff and of the <0.16 pin in v4/pyproject.toml.
#
# WHAT IT CHECKS
#
#   1. Every login in .all-contributorsrc has a row in the README table.
#   2. Every @handle in the README table has an .all-contributorsrc entry.
#   3. Neither surface lists the same person twice.
#   4. The same person is spelled the same way on both (GitHub resolves logins
#      case-insensitively, so a case-only mismatch still links — it is drift
#      between two hand-maintained files, and it is how a later exact-match
#      script starts reporting a person who is right there).
#   5. NEITHER SURFACE IS EMPTY. A gate that reads nothing and compares two
#      empty sets passes forever while guarding nothing — this repo has already
#      lost a guard exactly that way (a `ruff --fix` rewrote a canary and the
#      suite still said green). An empty read is a failure here, not a pass.
#
# It does NOT check contribution *types*: whether someone credited for `doc`
# should also carry `test` is a judgement call, not drift.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# Anchored to the exact row shape the README table uses:
#     | **[@handle](https://github.com/handle)** | what they did |
# Anchoring rather than matching any @mention is the point: a handle named in
# the surrounding prose is not a row in the credit table.
ROW = re.compile(r"^\| \*\*\[@([^\]]+)\]", re.M)

# The table lives under this heading. Scoping to the section keeps a future
# table elsewhere in the README from silently joining the comparison.
HEADING = re.compile(r"^#{2,4}\s+Contributors\s*$", re.M)


def repo_root() -> str:
    return subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def readme_handles(text: str) -> list[str]:
    """The @handles in the README's Contributors table, in document order.

    Returns them WITH duplicates, so the caller can report a name listed twice.
    """
    start = HEADING.search(text)
    if start is None:
        return []
    section = text[start.end() :]
    # Stop at the next heading of the same or higher level, so only the
    # Contributors section is read.
    end = re.search(r"^#{1,4}\s+\S", section, re.M)
    if end is not None:
        section = section[: end.start()]
    return ROW.findall(section)


def rc_logins(raw: str) -> list[str]:
    """The logins in .all-contributorsrc, in file order, duplicates kept."""
    data = json.loads(raw)
    return [c["login"] for c in data.get("contributors", [])]


def duplicates(names: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for name in names:
        key = name.lower()
        if key in seen and name not in dupes:
            dupes.append(name)
        seen.add(key)
    return dupes


def compare(rc: list[str], readme: list[str]) -> list[str]:
    """Every way the two rosters can disagree, as printable lines."""
    problems: list[str] = []

    if not rc:
        problems.append(
            ".all-contributorsrc lists no contributors at all — the file is empty, "
            "unparseable, or its shape changed. This gate cannot guard two empty sets."
        )
    if not readme:
        problems.append(
            "the README Contributors table has no rows this gate can see — the heading "
            "or the row format changed. Fix ROW/HEADING in this script, or the gate is "
            "passing while guarding nothing."
        )
    if problems:
        return problems

    for surface, names in ((".all-contributorsrc", rc), ("README.md", readme)):
        for name in duplicates(names):
            problems.append(f"{surface} lists @{name} more than once")

    rc_by_key = {n.lower(): n for n in rc}
    readme_by_key = {n.lower(): n for n in readme}

    for key in sorted(rc_by_key.keys() - readme_by_key.keys()):
        problems.append(
            f"@{rc_by_key[key]} is in .all-contributorsrc but has no row in the README table"
        )
    for key in sorted(readme_by_key.keys() - rc_by_key.keys()):
        problems.append(
            f"@{readme_by_key[key]} has a README row but no .all-contributorsrc entry"
        )
    for key in sorted(rc_by_key.keys() & readme_by_key.keys()):
        if rc_by_key[key] != readme_by_key[key]:
            problems.append(
                f"spelled two ways: .all-contributorsrc has @{rc_by_key[key]}, "
                f"README.md has @{readme_by_key[key]}"
            )

    return problems


def main(argv: list[str]) -> int:
    if len(argv) == 2:
        rc_path, readme_path = Path(argv[0]), Path(argv[1])
    elif argv:
        print("usage: check-contributor-roster.py [<.all-contributorsrc> <README.md>]")
        return 2
    else:
        root = Path(repo_root())
        rc_path, readme_path = root / ".all-contributorsrc", root / "README.md"

    try:
        rc = rc_logins(rc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"roster FAILED — cannot read {rc_path}: {exc}")
        return 1
    try:
        readme = readme_handles(readme_path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"roster FAILED — cannot read {readme_path}: {exc}")
        return 1

    problems = compare(rc, readme)
    if problems:
        print(f"{len(problems)} contributor-roster problem(s):\n")
        for line in problems:
            print(f"  {line}")
        print(
            "\nBoth surfaces must name the same people: .all-contributorsrc is the"
            "\nmachine-readable record, the README table is what anyone actually reads."
            "\nSomeone missing from one is under-credited; missing from both is invisible."
            "\nSee #167."
        )
        return 1

    print(f"roster OK — .all-contributorsrc and the README table name the same {len(rc)} contributors")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
