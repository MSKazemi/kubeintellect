#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# check-syntax-warnings.py — compile every tracked Python file with
# SyntaxWarning promoted to an error.
#
# WHY THIS EXISTS
#
# Issue #63 was an invalid escape sequence (`"\d"` in a non-raw string) in
# coordinator.py. Python emits a SyntaxWarning for it — but nothing in this
# repository could fail on that warning:
#
#   * `ruff check` is pinned <0.16 and does not report it in the CI-linted scope;
#   * `mypy` does not compile source, so it never sees it;
#   * pytest *imports* the module, which does emit the warning once — but only
#     on a cold cache. Once a `.pyc` exists the compile step is skipped entirely
#     and the warning never reappears, so a green suite proves nothing here.
#
# That is how #63 survived to be reported by an outside contributor. The defect
# also matters more than a style nit: the same non-raw string was corrupting the
# jsonpath examples in the coordinator prompt, so a "harmless warning" was
# silently degrading agent behaviour.
#
# Like scripts/check-file-modes.sh, this guard is deliberately INDEPENDENT of
# ruff and of the <0.16 pin in v4/pyproject.toml: stdlib only, no virtualenv, no
# network, no dependency on how the ruff upgrade eventually lands.
#
# It uses compile() rather than `compileall`, because `compileall` writes .pyc
# files as a side effect. This check leaves the tree untouched.
#
# SCOPE
#
# Every tracked `*.py` file except the frozen generations v1/, v2/ and v3/,
# which are closed to changes under ADR-001/002 and are not built by CI. This
# matches scripts/check-file-modes.sh and the scope statement at the top of
# .github/workflows/ci.yml.
#
# INTERPRETER
#
# Run it on the NEWEST interpreter the project supports, because that is where
# new SyntaxWarnings appear first — CI runs it on 3.13, which is what
# v4/Dockerfile actually ships. It is still useful on 3.12; the version it used
# is printed so the output is never ambiguous about what was proven.
#
# USAGE
#
#   ./scripts/check-syntax-warnings.py            # scan the tracked tree, exit 1 if any
#   ./scripts/check-syntax-warnings.py a.py b.py  # scan just those files
#   make check-syntax                             # same as the first form
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import subprocess
import sys
import warnings

FROZEN_PREFIXES = ("v1/", "v2/", "v3/")


def repo_root() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def tracked_python_files(root: str | None = None) -> list[str]:
    """Tracked *.py paths, relative to the repo root, outside v1-v3.

    `git ls-files` is run WITH `cwd=root` deliberately. Run from a subdirectory
    it would both restrict the listing to that subtree and emit paths relative
    to it, so a gate invoked from `v4/` would silently check a subset — or, once
    joined against the root, nothing at all.

    `-z` is used so a path containing a newline cannot split into two bogus
    entries.
    """
    root = root if root is not None else repo_root()
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        capture_output=True,
        check=True,
        text=True,
        cwd=root,
    ).stdout
    paths = [p for p in out.split("\0") if p]
    return [p for p in paths if not p.startswith(FROZEN_PREFIXES)]


def check_paths(
    paths: list[str], root: str = ""
) -> tuple[int, list[tuple[str, str]], list[str]]:
    """Compile each path; return (files checked, [(path, complaint), …], [skipped path, …]).

    Split out from main() so the gate can be exercised against fixture files in
    a test without touching the git index.

    `skipped` is returned rather than swallowed. Whether an unreadable path should
    FAIL the gate is a settled question — it must not, because a hook that passes a
    list including a just-deleted path would be turned red for doing the right thing
    (see test_an_explicit_file_list_is_still_the_callers_business). But that decision
    was about the exit code, not about silence: a gate that prints `syntax OK — 1
    file(s)` after being handed three is describing its own effort as if it were
    coverage. The caller chose the scope; the gate still has to say what it did with it.
    """
    failures: list[tuple[str, str]] = []
    skipped: list[str] = []
    checked = 0

    for path in paths:
        full = f"{root}/{path}" if root else path
        try:
            with open(full, "rb") as fh:
                source = fh.read()
        except OSError:
            # A tracked path that is not readable here (submodule, deleted in
            # the working tree) is not this gate's business to FAIL on — but it is
            # its business to report, or the printed count silently redefines
            # itself from "the scope" to "whatever happened to be readable".
            skipped.append(path)
            continue

        checked += 1
        with warnings.catch_warnings():
            # An error, not a filter: a warning we merely print is a warning
            # nobody reads, which is exactly how #63 survived.
            warnings.simplefilter("error", SyntaxWarning)
            try:
                compile(source, path, "exec")
            except SyntaxWarning as exc:
                failures.append((path, f"SyntaxWarning: {exc}"))
            except SyntaxError as exc:
                # A SyntaxWarning promoted to an error surfaces here, not above,
                # because the compiler raises it as a SyntaxError. Genuinely
                # unparseable source lands here too — strictly worse, and worth
                # reporting from the one gate that compiles every file rather
                # than leaving it to surface as a mystery import error.
                failures.append((path, f"SyntaxError: {exc}"))

    return checked, failures, skipped


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv:
        root = ""
        paths = argv
    else:
        try:
            root = repo_root()
            paths = tracked_python_files(root)
        except (OSError, subprocess.CalledProcessError):
            print("ERROR: not inside a git repository", file=sys.stderr)
            return 2

    checked, failures, skipped = check_paths(paths, root)
    version = ".".join(str(n) for n in sys.version_info[:3])

    if skipped:
        # stderr, always, in both forms. The exit code stays the caller's contract;
        # what changes is that the number in the OK line can no longer be mistaken
        # for the number of files the caller asked about.
        print(
            f"NOTE: {len(skipped)} of {len(paths)} path(s) could not be read and were "
            f"NOT compiled — they are not covered by the verdict below:",
            file=sys.stderr,
        )
        for path in skipped[:20]:
            print(f"  - {path}", file=sys.stderr)
        if len(skipped) > 20:
            print(f"  … and {len(skipped) - 20} more", file=sys.stderr)

    if not argv and checked == 0:
        # Sibling of the same defect in check-file-modes.sh: a gate that compiled
        # nothing is not a gate that passed. Reachable from a sparse or partial
        # checkout, and from an index that tracks no Python outside v1-v3. The
        # count was already printed, but printing "0" beside the word OK still
        # reads as a pass to anyone scanning CI output.
        print(
            "ERROR: found 0 tracked Python files outside v1-v3 — nothing was "
            "compiled.\nThis is not a pass. A sparse or partial checkout produces "
            "it; check out the full tree.",
            file=sys.stderr,
        )
        return 1
    scope = (
        f"{checked} file(s)"
        if argv
        else f"{checked} tracked Python files outside v1-v3"
    )
    shortfall = f" ({len(skipped)} of {len(paths)} skipped, see above)" if skipped else ""

    if not failures:
        if checked == 0:
            # Only reachable in the explicit-file form; the tracked form already
            # failed above. The contract says do not turn the caller red, so the
            # code stays 0 — but "OK" is not a word that may appear over a scan
            # that opened no files at all.
            print(
                f"nothing compiled — all {len(paths)} path(s) given were unreadable"
                "; this verdict covers no code",
                file=sys.stderr,
            )
            return 0
        print(
            f"syntax OK — {scope} compile with no SyntaxWarning "
            f"on Python {version}{shortfall}"
        )
        return 0

    print(
        f"ERROR: {len(failures)} file(s) do not compile cleanly on "
        f"Python {version}:",
        file=sys.stderr,
    )
    print(file=sys.stderr)
    for path, detail in failures:
        print(f"  {path}\n      {detail}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "The usual cause is an invalid escape sequence in a non-raw string "
        "(#63): write r\"\\d+\" rather than \"\\d+\".\n"
        "Do not silence these — the escape is also corrupting the string's "
        "actual value.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
