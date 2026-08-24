"""The maps pass 106 did *not* find drift in — pinned, so the next sweep is a test run.

Pass 106 found three phantom modules in `docs/architecture.md`. Pass 107 asked the same question
of every other map in the repo and found **none** — the first dry result since pass 34. Audited
2026-08-20:

    docs/cli-reference.md      every documented flag is accepted (3 apparent misses were mine:
                               `kubeintellect` is a second binary, and one `--limit` sat inside a
                               shell-completion *example*)
    KUBE_Q_URL default         code and all 8 doc mentions agree on https://api.kubeintellect.com,
                               and both that host and the apex resolve
    docs/configuration.md      95 documented env-var rows vs 157 Settings fields — every row is
                               either a real field or a labelled non-server var
    v4/CLAUDE.md               13 module paths, 10 package dirs, 20 setting names — all real
    AGENTS.md                  3 module paths, 2 dirs — all real

A clean audit that leaves nothing behind gets repeated by hand forever, which is the same failure
the architecture map had: *a map is only trustworthy while something checks it against the
territory*. So the sweep is a test now. Nothing here fixes a defect — these assertions all passed
the moment they were written, and that is the point.

Deliberately **not** gated: the per-flag CLI reference. Extracting flags from prose produced three
false positives in one page (a second binary's flags, and a flag named inside a completion
example), and a gate that cries wolf gets suppressed. `kq`'s own `--help` output is already
covered by the kq suite.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.core.config import Settings

_V4 = Path(__file__).resolve().parents[1]
_SERVER = _V4 / "packages" / "kubeintellect-server"
_CLAUDE_MD = _V4 / "CLAUDE.md"
_AGENTS_MD = _V4.parent / "AGENTS.md"
_CONFIG_DOC = _V4 / "docs" / "configuration.md"

#: Env vars `docs/configuration.md` documents that are deliberately not server settings. Each
#: must still be read *somewhere* in the repo — a documented variable nothing consumes is the
#: `KI_V5_*` unwired-flag defect wearing different clothes (see tests/test_v5_flag_wiring.py).
_NON_SERVER_VARS = {
    "KUBE_Q_URL":     "kq CLI config — the backend the CLI talks to",
    "KUBE_Q_API_KEY": "kq CLI config — the bearer token the CLI sends",
    "GRAFANA_URL":    "read by app/cli.py only; the server never queries Grafana",
}

#: Non-vacuity floors. A regex that stops matching must fail, not pass on an empty set.
_MIN_CLAUDE_PATHS = 10
_MIN_CLAUDE_DIRS = 6
_MIN_CONFIG_ROWS = 60

_SETTINGS_FIELDS = set(Settings.model_fields)


def _module_paths(text: str) -> list[str]:
    return sorted(set(re.findall(r"\bapp/[A-Za-z0-9_./-]+\.py\b", text)))


def _package_dirs(text: str) -> list[str]:
    return sorted(set(re.findall(r"\bapp/[A-Za-z0-9_/-]+/(?![A-Za-z0-9_]*\.py)", text)))


def _documented_env_rows() -> list[tuple[str, str]]:
    """`| \\`NAME\\` | default | description |` rows from the configuration page."""
    return re.findall(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|\s*([^|]*?)\s*\|", _CONFIG_DOC.read_text(encoding="utf-8"), re.M)


def _read_anywhere(name: str) -> bool:
    return any(
        name in p.read_text(encoding="utf-8")
        for p in (_V4 / "packages").rglob("*.py")
        if "/tests/" not in p.as_posix()
    )


#: `v4/CLAUDE.md` is tracked in the private tree only — a public clone, and therefore CI, has
#: no such file. Asserting on it unguarded is not a stricter test, it is a `FileNotFoundError`
#: everywhere but this laptop. The guard is narrow on purpose: it covers exactly the one map
#: that is private, and `TestThePublicMapsAreNeverSkipped` below fails if it ever widens.
_CLAUDE_MAP_PRESENT = _CLAUDE_MD.is_file()


@pytest.mark.skipif(
    not _CLAUDE_MAP_PRESENT,
    reason="v4/CLAUDE.md is tracked in the private tree only; nothing to check here",
)
class TestTheClaudeMapNamesRealModules:
    """`v4/CLAUDE.md` is the map every session reads before touching anything."""

    def test_the_scan_found_the_map(self):
        text = _CLAUDE_MD.read_text(encoding="utf-8")
        assert len(_module_paths(text)) >= _MIN_CLAUDE_PATHS
        assert len(_package_dirs(text)) >= _MIN_CLAUDE_DIRS

    def test_every_module_path_exists(self):
        missing = [p for p in _module_paths(_CLAUDE_MD.read_text(encoding="utf-8")) if not (_SERVER / p).exists()]
        assert missing == [], f"CLAUDE.md names modules that do not exist: {missing}"

    def test_every_package_dir_exists(self):
        missing = [d for d in _package_dirs(_CLAUDE_MD.read_text(encoding="utf-8")) if not (_SERVER / d).is_dir()]
        assert missing == [], f"CLAUDE.md names packages that do not exist: {missing}"

    def test_every_setting_it_names_is_a_real_setting(self):
        """The layer tables pair each feature with the flag that gates it."""
        named = set(re.findall(r"`([A-Z][A-Z0-9_]{4,})`", _CLAUDE_MD.read_text(encoding="utf-8")))
        # names that are plainly not settings: wire formats, acronyms, other namespaces
        candidates = {
            n for n in named
            if not n.startswith(("KUBE_Q_", "ADR", "SSE", "YAML", "RE2", "CRUD", "HITL", "LLM"))
        }
        assert candidates, "no setting names parsed out of CLAUDE.md"
        unknown = sorted(candidates - _SETTINGS_FIELDS)
        assert unknown == [], f"CLAUDE.md names flags that are not Settings fields: {unknown}"


class TestTheAgentsMapNamesRealModules:

    def test_every_module_path_exists(self):
        text = _AGENTS_MD.read_text(encoding="utf-8")
        missing = [p for p in _module_paths(text) if not (_SERVER / p).exists()]
        assert missing == [], f"AGENTS.md names modules that do not exist: {missing}"

    def test_every_package_dir_exists(self):
        text = _AGENTS_MD.read_text(encoding="utf-8")
        missing = [d for d in _package_dirs(text) if not (_SERVER / d).is_dir()]
        assert missing == [], f"AGENTS.md names packages that do not exist: {missing}"


class TestEveryDocumentedEnvVarGoesSomewhere:
    """A documented variable nothing reads is a setting that lies about having an effect."""

    def test_the_table_was_actually_parsed(self):
        rows = _documented_env_rows()
        assert len(rows) >= _MIN_CONFIG_ROWS, (
            f"only {len(rows)} env-var rows parsed from configuration.md — the table format "
            "changed and this gate is no longer reading it"
        )

    def test_every_row_is_a_setting_or_a_declared_exception(self):
        unknown = sorted(
            name for name, _ in _documented_env_rows()
            if name not in _SETTINGS_FIELDS and name not in _NON_SERVER_VARS
        )
        assert unknown == [], (
            "configuration.md documents variables that are neither Settings fields nor listed in "
            f"_NON_SERVER_VARS: {unknown}"
        )

    @pytest.mark.parametrize("name", sorted(_NON_SERVER_VARS))
    def test_each_declared_exception_is_really_consumed(self, name):
        """The exception list is not a hiding place — each entry must be read by real code."""
        assert _read_anywhere(name), (
            f"{name} is documented and excused as a non-server setting, but nothing outside "
            f"tests reads it ({_NON_SERVER_VARS[name]})"
        )

    def test_each_declared_exception_is_still_documented(self):
        """The reverse: an exception for a row that no longer exists is dead weight."""
        documented = {name for name, _ in _documented_env_rows()}
        stale = sorted(set(_NON_SERVER_VARS) - documented)
        assert stale == [], f"_NON_SERVER_VARS excuses rows configuration.md no longer has: {stale}"


class TestThePublicMapsAreNeverSkipped:
    """A skip is how a suite stops testing without ever going red.

    One class above is conditional, because the map it checks is genuinely absent from a public
    checkout. That is the only acceptable skip in this file, and it must stay the only one: if
    the public maps ever went missing too, every assertion here would be skipped and the file
    would report green having checked nothing.
    """

    def test_the_public_maps_are_present(self):
        missing = [p.name for p in (_AGENTS_MD, _CONFIG_DOC) if not p.is_file()]

        assert missing == [], f"a map this file must always check is absent: {missing}"

    def test_the_public_map_checks_are_not_vacuous(self):
        rows = _documented_env_rows()
        agents_paths = _module_paths(_AGENTS_MD.read_text(encoding="utf-8"))

        assert len(rows) >= _MIN_CONFIG_ROWS, f"only {len(rows)} documented env rows parsed"
        assert agents_paths, "no module paths parsed out of AGENTS.md"

    def test_only_the_private_map_is_allowed_to_be_conditional(self):
        """Pins the exception. A second `skipif` in this file has to be argued for.

        Counted from the AST, not from the text: a substring count of "skipif" also matches
        this test's own assertion, so it would report 2 forever and never mean anything.
        """
        import ast

        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        skips = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            for dec in node.decorator_list
            if "skipif" in ast.dump(dec)
        ]

        assert skips == ["TestTheClaudeMapNamesRealModules"], f"unexpected conditionals: {skips}"
