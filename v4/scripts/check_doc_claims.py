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

import ast
import inspect
import re
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
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


def _files_this_tree_ships(rootdir: Path, subdir: str, pattern: str) -> set[Path] | None:
    """The files under ``rootdir/subdir`` that the repository actually carries.

    Returns ``None`` when that question cannot be answered here — not a checkout, no git,
    or an index that lists nothing — so every caller falls back to "everything on disk"
    rather than to "nothing", which would silently check nothing at all.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", subdir],
            cwd=rootdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # ``git ls-files`` prints paths relative to *rootdir* ("tests/test_x.py"); the pattern
    # describes the file name, so match on the basename or nothing ever matches — and an
    # empty match set reads as "this tree ships nothing", which disables the guard entirely.
    tracked = {
        rootdir / name
        for name in proc.stdout.split("\0")
        if name and fnmatch(Path(name).name, pattern)
    }
    return tracked or None


def _files_on_disk_only(rootdir: Path, subdir: str, pattern: str) -> list[Path]:
    """Files pytest would collect here that a clone of this repository would not have.

    The count this gate writes into AGENTS.md is read by people who cloned the repo, so it
    has to be a property of the repository rather than of whoever ran the gate. This tree
    carries test files that are deliberately private (`.git/info/exclude`) and, while
    working, files not yet added; counting them wrote a number no clone could reproduce,
    and the gate then failed for a contributor on a file they never touched.
    """
    on_disk = sorted((rootdir / subdir).rglob(pattern))
    shipped = _files_this_tree_ships(rootdir, subdir, pattern)
    if shipped is None:
        return []
    return [path for path in on_disk if path not in shipped]


def _evaluation_harness_tests(rootdir: Path) -> list[str]:
    """The test modules `tests/conftest.py` skips when the private `evaluation/` tree is absent.

    Read with `ast` rather than by importing conftest, which sets environment variables on
    import. An unreadable or changed conftest yields an empty list: the count then reflects
    this machine, which is the pre-existing behaviour and visible as a drift failure, rather
    than a silently wrong number.
    """
    try:
        tree = ast.parse((rootdir / "tests" / "conftest.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "EVALUATION_HARNESS_TESTS"
                   for t in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except ValueError:
            return []
        return [str(v) for v in value] if isinstance(value, list) else []
    return []


def _collect_count(rootdir: Path) -> int | None:
    """Number of tests pytest *collects* under *rootdir* — collection only, nothing runs.

    Returns ``None`` if collection could not be performed at all (no venv, pytest missing),
    so a developer without the workspace installed still gets the rest of the checks rather
    than a hard crash. A collection *error* is different from an absent pytest and is
    reported by the caller.
    """
    ignores = [f"--ignore={path}" for path in _files_on_disk_only(rootdir, "tests", "test_*.py")]
    # Count what a CLONE collects, not what this machine does. tests/conftest.py drops the
    # evaluation-harness modules when `evaluation/` is not importable; that tree is private,
    # so it is present for a maintainer and absent in every clone and in CI -- a 26-test gap
    # that put a number in AGENTS.md which CI could never reproduce. Ignoring them here
    # unconditionally makes the published figure the one a contributor actually sees.
    ignores += [f"--ignore=tests/{name}" for name in _evaluation_harness_tests(rootdir)]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/", *ignores],
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


def _format_drift_count() -> int | None:
    """How many *shipped* files ``ruff format`` would rewrite.

    `make lint` runs `ruff format --check`; CI does not. AGENTS.md tells contributors how big
    that gap is so they can ignore it — and the number was ``~108`` while the real figure was
    **116**, which is the failure mode this whole script exists for: a number nobody measures.

    Counted over the files `git` tracks rather than by walking the directory, for the same
    reason the suite count is: a number published in a doc has to be a property of the
    repository, not of whoever happened to run the gate. Returns ``None`` if ruff cannot run.
    """
    tracked = _files_this_tree_ships(_V4, "packages/kubeintellect-server/app", "*.py") or set()
    tracked |= _files_this_tree_ships(_V4, "packages/ki-protocol", "*.py") or set()
    if not tracked:
        return None
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--check", *sorted(str(p) for p in tracked)],
            cwd=_V4,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+) files? would be reformatted", proc.stdout)
    if m:
        return int(m.group(1))
    # "N files already formatted" with nothing to rewrite is a real answer: zero.
    return 0 if proc.returncode == 0 else None


# (doc, pattern, expected, label). Capture group 1 of *pattern* is the number.
_Claim = tuple[str, str, int, str]


def _measured_claims() -> tuple[list[_Claim], list[str]]:
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

    drift = _format_drift_count()
    if drift is None:
        notes.append("SKIP ruff format drift: could not run `ruff format --check` — count not verified")
    else:
        claims.append((
            "root:AGENTS.md",
            r"would reformat \*\*(\d+)\*\* files",
            drift,
            "ruff format drift count",
        ))
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
        # faq.md is the "why not just use ChatGPT?" page — a sales surface. It sat at 18 while
        # the library reached 23, because no entry here named it. examples.md was right but
        # equally unguarded; both are now healed by --fix rather than by hand.
        ("faq.md", r"\*\*(\d+) deterministic playbooks\*\*", pc, "playbook count"),
        ("examples.md", r"the (\d+) shipped playbooks", pc, "playbook count"),
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


# ── Exit-code tables: what a `kq` command documents vs what it can return ─────
#
# The exit tables in cli-reference.md are the machine-readable half of the CLI's
# contract — a script writes `kq replay X || case $? in …` against them. They were
# hand-written prose that nothing checked, and `kq replay` documented five codes
# while returning six. A missing row is worse than a missing paragraph: the script
# does not fail, it takes the wrong branch.

_CLI_DIR = _V4 / "packages" / "kube-q" / "kube_q" / "cli"
_EXIT_DOC = "cli-reference.md"
_EXIT_HEADING = re.compile(r"^#{2,3} `kq ([a-z0-9-]+)[`\s]")
_EXIT_ROW = re.compile(r"^\|\s*`(\d+)`\s*\|")


def _returns_in(node: ast.AST):
    """Every ``return`` belonging to *node* itself, not to a function nested inside it."""
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        if isinstance(child, ast.Return):
            yield child
        yield from _returns_in(child)


def _flatten_return(value: ast.expr) -> list[ast.expr]:
    """Every expression one ``return`` statement can actually hand back.

    ``return 0 if asked_for_help else 2`` is two exit codes wearing one statement —
    which is exactly where `kq replay`'s undocumented ``2`` was hiding.
    """
    if isinstance(value, ast.IfExp):
        return _flatten_return(value.body) + _flatten_return(value.orelse)
    if isinstance(value, ast.BoolOp):
        return [v for operand in value.values for v in _flatten_return(operand)]
    return [value]


def _returns_of(fn: ast.AST) -> tuple[set[int], set[str], list[str]]:
    """(integer codes, module-level functions returned, expressions we could not read)."""
    codes: set[int] = set()
    calls: set[str] = set()
    opaque: list[str] = []
    for node in _returns_in(fn):
        if node.value is None:
            continue
        for value in _flatten_return(node.value):
            if isinstance(value, ast.Constant) and isinstance(value.value, int):
                codes.add(value.value)
            elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                calls.add(value.func.id)
            else:
                opaque.append(ast.unparse(value))
    return codes, calls, opaque


def _exit_codes_of(module: Path) -> tuple[set[int], list[str]]:
    """Every status ``run(argv)`` can return, plus a note for anything unresolvable.

    Helper returns are followed: `kq postmortem` returns ``_verdict_exit(...)`` and
    `kq config` returns ``cmd_show()``/``cmd_reset(...)``, so a reader that stopped at
    ``run`` would conclude those commands return almost nothing and happily pass a
    table saying otherwise. Anything it still cannot read is reported as an error
    rather than skipped — a guard that quietly resolves less than it used to is a
    guard that quietly stops guarding.

    Falling off the end of ``run`` is not tracked separately: ``main()`` dispatches
    with ``sys.exit(runner(argv))`` and ``sys.exit(None)`` is 0, which every command
    already documents.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    fns: dict[str, ast.AST] = {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "run" not in fns:
        return set(), [f"{module.name} has no module-level run(argv)"]
    codes, pending, opaque = _returns_of(fns["run"])
    unresolved = [f"{module.name}: run() returns `{o}`, which this guard cannot read" for o in opaque]
    seen = {"run"}
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name not in fns:
            unresolved.append(
                f"{module.name}: a return value comes from {name}(), which is not "
                f"defined in this module — this guard cannot follow it"
            )
            continue
        more_codes, more_calls, more_opaque = _returns_of(fns[name])
        codes |= more_codes
        pending |= more_calls
        unresolved += [
            f"{module.name}: {name}() returns `{o}`, which this guard cannot read"
            for o in more_opaque
        ]
    return codes, unresolved


def _prose_lines(text: str) -> list[str]:
    """*text* with every fenced code line blanked, indices preserved.

    Without this, `kq completion`'s own usage example ends its section three lines in:
    ``# bash — add to ~/.bashrc`` inside a ```bash fence is not a heading, but it is
    indistinguishable from one to anything matching on ``^#``.
    """
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else line)
    return out


def _doc_section(text: str, command: str) -> list[str] | None:
    """The lines of the ``### `kq <command>`` section, or None if there is no such section."""
    lines = _prose_lines(text)
    start = None
    for i, line in enumerate(lines):
        m = _EXIT_HEADING.match(line)
        if m and m.group(1) == command:
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        if re.match(r"^#{1,3} ", lines[j]):
            return lines[start:j]
    return lines[start:]


def _exit_table(section: list[str]) -> set[int] | None:
    """Codes listed in the section's "Exit code" table, or None if it has no such table."""
    for i, line in enumerate(section):
        if not line.startswith("| Exit code"):
            continue
        codes: set[int] = set()
        for row in section[i + 1:]:
            if not row.startswith("|"):
                break
            m = _EXIT_ROW.match(row)
            if m:
                codes.add(int(m.group(1)))
        return codes
    return None


_HELP_EXIT_HEADING = re.compile(r"^[ \t]*Exit codes?:[ \t]*$")
_HELP_EXIT_ROW = re.compile(r"^[ \t]+(\d+)[ \t]{2,}\S")


def _docstring_exit_codes(module: Path) -> set[int] | None:
    """Codes listed under ``Exit codes:`` in the module docstring, or None if absent.

    That docstring is not decoration: these commands ``print(__doc__)`` on a usage
    error, so it is the exit table the operator reads *at the moment they got one
    wrong*. `kq replay`'s omitted the usage code ``2`` — the one code that block was
    being printed to explain.
    """
    doc = ast.get_docstring(ast.parse(module.read_text(encoding="utf-8")))
    if not doc:
        return None
    lines = doc.splitlines()
    for i, line in enumerate(lines):
        if not _HELP_EXIT_HEADING.match(line):
            continue
        codes: set[int] = set()
        for row in lines[i + 1:]:
            if row.strip() and not row[:1].isspace():
                break
            m = _HELP_EXIT_ROW.match(row)
            if m:
                codes.add(int(m.group(1)))
        return codes
    return None


def _check_exit_codes() -> list[str]:
    """Assert every `kq` subcommand documents exactly the exit codes it can return."""
    errors: list[str] = []
    text = _read(_EXIT_DOC)
    modules = sorted(_CLI_DIR.glob("*_cmd.py"))
    if not modules:
        return [
            f"no kq subcommand modules under {_CLI_DIR} — the exit-code guard "
            f"checked nothing, which is not the same as finding nothing wrong"
        ]
    for module in modules:
        command = module.stem[: -len("_cmd")].replace("_", "-")
        codes, unresolved = _exit_codes_of(module)
        errors += unresolved
        section = _doc_section(text, command)
        if section is None:
            errors.append(
                f"{_EXIT_DOC}: there is no `kq {command}` section, but "
                f"{module.name} ships that command"
            )
            continue
        documented = _exit_table(section)
        if documented is None:
            if codes - {0}:
                errors.append(
                    f"{_EXIT_DOC}: `kq {command}` has no exit-code table, but it can "
                    f"exit {sorted(codes - {0})} as well as 0 — an undocumented "
                    f"non-zero exit is a branch no script can be written against"
                )
            continue
        if documented != codes:
            detail = []
            if codes - documented:
                detail.append(f"can return {sorted(codes - documented)} undocumented")
            if documented - codes:
                detail.append(f"documents {sorted(documented - codes)} it cannot return")
            errors.append(f"{_EXIT_DOC}: `kq {command}` " + "; ".join(detail))
        # Second surface, same claim: the usage text printed on a usage error.
        in_help = _docstring_exit_codes(module)
        if in_help is not None and in_help != codes:
            detail = []
            if codes - in_help:
                detail.append(f"omits {sorted(codes - in_help)}")
            if in_help - codes:
                detail.append(f"lists {sorted(in_help - codes)} it cannot return")
            errors.append(
                f"{module.name}: the `Exit codes:` block in the usage text "
                + "; ".join(detail)
            )
    return errors


# ── Pinned container image tags ──────────────────────────────────────────────
#
# A documented `docker pull ghcr.io/mskazemi/kubeintellect:<v>` is an instruction,
# not a description, and nothing checked it until 2026-08-29. It had drifted three
# ways at once: the README handed newcomers `2.2.0` (three releases old, and its
# own /healthz answers `"version":"0+unknown"`), while `deploy/alibaba.md` pinned
# `2.3.1` FOUR times — an image that cannot start at all:
#
#     $ docker run --rm --entrypoint python ghcr.io/mskazemi/kubeintellect:2.3.1 \
#           -c "import uvicorn"
#     ModuleNotFoundError: No module named 'uvicorn'
#
# (that is issue #158, fixed 08-21, after every image those tags point at was
# built). `2.4.1` starts and answers `/healthz` in 10 s, measured. A pinned tag
# goes stale silently and by default, so it needs a gate rather than a habit.
#
# CHANGELOG.md is excluded on purpose: its old tags are history, and history is
# supposed to name the version it happened to.

_IMAGE_TAG_RE = re.compile(r"ghcr\.io/mskazemi/(?:charts/)?kubeintellect:(\d+\.\d+\.\d+)")


def _current_version() -> str | None:
    """The version this tree ships, from the server distribution's metadata."""
    pyproject = _V4 / "packages" / "kubeintellect-server" / "pyproject.toml"
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def _check_image_tags() -> list[str]:
    version = _current_version()
    if version is None:
        return ["SKIP image tags: could not read the version from the server pyproject"]

    surfaces = [_ROOT / "README.md"]
    surfaces += sorted(_DOCS.rglob("*.md"))
    surfaces += sorted((_V4 / "packages" / "kube-q" / "docs").rglob("*.md"))

    errors: list[str] = []
    for path in surfaces:
        if path.name == "CHANGELOG.md" or not path.is_file():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for tag in _IMAGE_TAG_RE.findall(line):
                if tag != version:
                    rel = path.relative_to(_ROOT)
                    errors.append(
                        f"{rel}:{n}: pins the image tag {tag}, but this tree ships "
                        f"{version} — a reader runs the old image, which may not start"
                    )
    return errors


def run_checks() -> list[str]:
    c = _canonical()
    errors: list[str] = []
    for doc, pattern, expected, label in _numeric_claims(c):
        errors += _check_number(doc, pattern, expected, label)
    errors += _check_providers(c)
    errors += _check_exit_codes()
    errors += _check_image_tags()
    test_claims, notes = _measured_claims()
    errors += notes
    for doc, pattern, expected, label in test_claims:
        errors += _check_number(doc, pattern, expected, label)
    return errors


def fix_claims() -> tuple[int, list[str]]:
    """Rewrite every drifted numeric claim to match the code. Returns (count, notes)."""
    c = _canonical()
    test_claims, notes = _measured_claims()
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
        "(playbooks, detectors, providers, v5 flags, test counts, CLI exit codes)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
