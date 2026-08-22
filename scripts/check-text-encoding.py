#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# check-text-encoding.py — every text-mode read/write names its encoding.
#
# WHY THIS EXISTS
#
# `Path.read_text()`, `Path.write_text()` and `open()` in text mode decode and
# encode using the PLATFORM DEFAULT, which is not UTF-8 on Windows (CP1252,
# CP936) or under the POSIX `C` locale. Any non-ASCII byte then raises
# UnicodeDecodeError.
#
# Issue #136 was exactly that: the playbook loader read YAML containing
# em-dashes with a bare read_text(), and on a non-UTF-8 locale the per-file
# handler in _load_all() silently DROPPED the playbook. #138 fixed that call.
# #156 found the same bug class still live at 22 more sites, several of them
# read/modify/write cycles on the user's own config and .env files — where a
# decode failure is not a crash on read but mangled content written back.
#
# A one-off fix does not hold a bug class. This gate does.
#
# Like check-syntax-warnings.py and check-file-modes.sh, it is deliberately
# INDEPENDENT of ruff and of the <0.16 pin in v4/pyproject.toml: stdlib only, no
# virtualenv, no network. Ruff's PLW1514 covers this rule, but it is not in the
# CI-linted scope today and turning it on repo-wide would also light up v1-v3.
#
# WHAT IT DOES NOT FLAG
#
#   * binary mode — `open(p, "rb")`, `mode="wb"`. There is no encoding there.
#   * a call already passing `encoding=`, whatever the value.
#   * a call passing `**kwargs`, where the encoding may be supplied at runtime.
#   * an encoding passed POSITIONALLY — `p.read_text("utf-8")`.
#   * `open` on a module that has no text encoding to name — `webbrowser.open(url)`,
#     `zipfile.ZipFile(z).open(m)` — and the compression modules' binary default,
#     `gzip.open(p)`. An explicit `gzip.open(p, "rt")` IS still flagged.
#
# It DOES flag a source file that is not valid UTF-8, rather than skipping it: a
# file the gate cannot read is a file the gate is not guarding.
#
# NOT COVERED, and tracked separately: `subprocess.run(..., text=True)` decodes
# with the locale encoding too. Same bug class, different call shape.
#
# SCOPE
#
# Every tracked `*.py` file except the frozen generations v1/, v2/ and v3/,
# which are closed to changes under ADR-001/002 and are not built by CI. This
# matches check-syntax-warnings.py and the scope statement in ci.yml.
#
# USAGE
#
#   ./scripts/check-text-encoding.py            # scan the tracked tree, exit 1 if any
#   ./scripts/check-text-encoding.py a.py b.py  # scan just those files
#   make check-encoding                         # same as the first form
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import ast
import subprocess
import sys

FROZEN_PREFIXES = ("v1/", "v2/", "v3/")
TEXT_IO_NAMES = ("read_text", "write_text", "open")

# `<module>.open(...)` calls that are not text-file opens at all. Their signatures
# have no text `encoding` parameter, so the remediation this gate prints would
# raise TypeError if a contributor followed it — the one thing a gate must never
# do. Matched on the receiver name, which is how these modules are always called.
NON_TEXT_OPEN_OWNERS = frozenset({"webbrowser", "os", "zipfile", "tarfile", "shelve", "dbm"})

# These do take an encoding, but their open() defaults to BINARY, unlike the
# builtin. Flagging a call that names no mode would therefore be a false positive;
# an explicit text mode ("rt"/"wt") is the real thing and is still flagged.
BINARY_DEFAULT_OPEN_OWNERS = frozenset({"gzip", "bz2", "lzma"})

# Where `encoding` sits when passed POSITIONALLY. Passing it that way satisfies the
# rule just as well, and telling the author to add the keyword would be a
# duplicate-argument TypeError:
#     Path.read_text(encoding, errors, newline)            -> 0
#     Path.write_text(data, encoding, errors, newline)     -> 1
#     Path.open(mode, buffering, encoding, ...)            -> 2
#     open(file, mode, buffering, encoding, ...)           -> 3   (builtin, bare name)
_POSITIONAL_ENCODING_INDEX = {"read_text": 0, "write_text": 1, "open": 2}
_BUILTIN_OPEN_ENCODING_INDEX = 3


def repo_root() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def tracked_python_files(root: str | None = None) -> list[str]:
    """Tracked *.py paths, relative to the repo root, outside v1-v3.

    `cwd=root` and `-z` for the same reasons as check-syntax-warnings.py: run
    from a subdirectory the listing would silently narrow, and a path containing
    a newline would split into two bogus entries.
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


def _is_binary_mode(call: ast.Call) -> bool:
    """True when the call opens in binary mode, positionally or by keyword."""
    if 1 < len(call.args):
        mode = call.args[1]
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
            return True
    for keyword in call.keywords:
        if keyword.arg != "mode":
            continue
        mode = keyword.value
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
            return True
    return False


def _receiver_name(call: ast.Call) -> str | None:
    """The module a method call is ultimately made on, when it is a plain name.

    One level of construction is resolved, so `zipfile.ZipFile(z).open(m)` reports
    `zipfile`: that handle is binary and takes no encoding either. `Path(p).open()`
    resolves to nothing and stays in scope, which is the point.
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    receiver = func.value
    if isinstance(receiver, ast.Name):
        return receiver.id
    if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Attribute):
        inner = receiver.func.value
        if isinstance(inner, ast.Name):
            return inner.id
    return None


def _mode_literal(call: ast.Call) -> str | None:
    """The `open()` mode string, positional or keyword; None if absent or dynamic."""
    if 1 < len(call.args):
        mode = call.args[1]
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return mode.value
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                return mode.value
    return None


def _passes_encoding_positionally(call: ast.Call, name: str) -> bool:
    """True when `encoding` was supplied without the keyword."""
    if name == "open" and isinstance(call.func, ast.Name):
        index = _BUILTIN_OPEN_ENCODING_INDEX
    else:
        index = _POSITIONAL_ENCODING_INDEX[name]
    return index < len(call.args)


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def offenders(source: str, path: str) -> list[tuple[str, int, str]]:
    """Every text-mode call in `source` that does not name an encoding."""
    found: list[tuple[str, int, str]] = []
    tree = ast.parse(source, path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name not in TEXT_IO_NAMES:
            continue
        if any(keyword.arg == "encoding" for keyword in node.keywords):
            continue
        # `**kwargs` may carry it; flagging that would push the fix toward
        # duplicating an argument the caller already forwards.
        if any(keyword.arg is None for keyword in node.keywords):
            continue
        if _passes_encoding_positionally(node, name):
            continue
        if "open" == name:
            owner = _receiver_name(node)
            if owner in NON_TEXT_OPEN_OWNERS:
                continue
            mode = _mode_literal(node)
            if owner in BINARY_DEFAULT_OPEN_OWNERS and (mode is None or "b" in mode):
                continue
            if _is_binary_mode(node):
                continue
        found.append((path, node.lineno, name))
    return found


def check_paths(paths: list[str], root: str = "") -> tuple[int, list[tuple[str, int, str]]]:
    """Scan each path; return (files checked, offenders).

    Split out from main() so the gate can be exercised against fixture files in
    a test without touching the git index.
    """
    failures: list[tuple[str, int, str]] = []
    checked = 0
    for path in paths:
        full = f"{root}/{path}" if root else path
        try:
            with open(full, "rb") as handle:
                source = handle.read().decode("utf-8")
        except OSError:
            # A tracked path that is not readable here (submodule, deleted in
            # the working tree) is not this gate's business.
            continue
        except UnicodeDecodeError:
            # Not decodable as UTF-8 at all — a PEP 263 `# -*- coding: latin-1 -*-`
            # source, say. Skipping it silently is how a gate stops guarding
            # without anyone noticing (see the UP045 autofix incident), and the
            # file is itself an instance of the class this gate exists to catch.
            checked += 1
            failures.append((path, 0, "source is not valid UTF-8"))
            continue
        checked += 1
        try:
            failures.extend(offenders(source, path))
        except SyntaxError:
            # check-syntax-warnings.py owns that failure and reports it better.
            continue
    return checked, failures


def main(argv: list[str]) -> int:
    root = ""
    if argv:
        paths = argv
    else:
        root = repo_root()
        paths = tracked_python_files(root)

    checked, failures = check_paths(paths, root)
    if failures:
        print(f"{len(failures)} text-mode call(s) without an explicit encoding:\n")
        for path, lineno, name in failures:
            if lineno:
                print(f"  {path}:{lineno}  {name}() — add encoding=\"utf-8\"")
            else:
                print(f"  {path}  {name} — re-save the file as UTF-8")
        print(
            "\nThe platform default is not UTF-8 on Windows or under the C locale, so any"
            "\nnon-ASCII byte raises UnicodeDecodeError there and nowhere else. See #136/#156."
            '\nBinary mode ("rb"/"wb") is not flagged and needs no encoding.'
        )
        return 1

    print(f"encoding OK — {checked} tracked Python files outside v1-v3 name an encoding on every text-mode call")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
