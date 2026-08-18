"""Guard AGENTS.md safety invariant #6 — the injected `RunnableConfig` annotation.

`langchain_core` matches the injected run config **by identity** (`type_ is RunnableConfig`).
Widening the annotation to `Optional[RunnableConfig]` / `RunnableConfig | None` — exactly what a
type checker, ruff's `UP045`, or an agent "cleaning up implicit Optional" will suggest — stops
the config being injected at all. The tool then silently receives `config=None` and loses
`user_role` (RBAC) and `hitl_bypass` (the HITL gate).

Nothing else in the suite can catch this:

* **mypy cannot** — both forms type-check; the correct one even needs `# type: ignore`.
* **ruff cannot** — the pin is `<0.16` precisely because `UP045` *suggests the broken form*.
* **behavioural tests mostly cannot** — a tool that never reads `config` still passes every
  test while silently receiving `None`, which is how `read_verbs.py` carried the widened form
  undetected. It made no RBAC decision, so nothing failed; the first RBAC check added there
  would have failed open.

So this file asserts the invariant two ways: statically over the real source, and dynamically
against the installed `langchain_core` so the *reason* stays proven if the library changes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Optional

import pytest
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

APP_ROOT = Path(__file__).resolve().parents[1] / "packages" / "kubeintellect-server" / "app"

# Any parameter annotated as an injected tool arg carrying the run config.
INJECTED_CONFIG_RE = re.compile(
    r"^\s*config:\s*Annotated\[(?P<inner>[^,]+),\s*InjectedToolArg\]", re.MULTILINE
)


def _injected_config_sites() -> list[tuple[Path, int, str]]:
    """Every `config: Annotated[..., InjectedToolArg]` parameter in the app source."""
    sites: list[tuple[Path, int, str]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "InjectedToolArg" not in text:
            continue
        for m in INJECTED_CONFIG_RE.finditer(text):
            line = text[: m.start()].count("\n") + 1
            sites.append((path, line, m.group("inner").strip()))
    return sites


def test_injected_config_sites_are_discoverable() -> None:
    """The scanner must actually find the known sites — a regex that matches nothing
    would make every other assertion in this file vacuously true."""
    sites = _injected_config_sites()
    assert sites, "found no injected-config parameters — the scanner regex is broken"
    files = {p.name for p, _, _ in sites}
    assert "kubectl_tool.py" in files, f"expected the mutating tool among {files}"


@pytest.mark.parametrize("path,line,inner", _injected_config_sites(), ids=lambda v: str(v))
def test_injected_config_annotation_is_bare_runnableconfig(
    path: Path, line: int, inner: str
) -> None:
    """Every injected config parameter must be annotated bare `RunnableConfig`."""
    assert inner == "RunnableConfig", (
        f"{path}:{line} annotates the injected run config as `{inner}`. "
        "It must be exactly `RunnableConfig` — langchain_core matches by identity, so any "
        "widened form silently disables run-config injection, taking RBAC (`user_role`) and "
        "the HITL gate (`hitl_bypass`) with it. See AGENTS.md safety invariant #6."
    )


def test_bare_annotation_actually_receives_the_config() -> None:
    """Dynamic half: prove the required form really is injected by this langchain version."""
    seen: dict[str, object] = {}

    @tool
    def probe(q: str, config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:  # type: ignore[assignment]
        """Canary tool using the invariant's required annotation."""
        seen["config"] = config
        return "ok"

    probe.invoke({"q": "x"}, config={"configurable": {"user_role": "admin"}})
    got = seen.get("config")
    assert isinstance(got, dict), "bare RunnableConfig was not injected at all"
    assert got.get("configurable", {}).get("user_role") == "admin", (
        "bare RunnableConfig was injected but carried no `user_role` — RBAC would fail open"
    )


def test_widened_annotation_silently_loses_the_config() -> None:
    """Dynamic half: prove the *forbidden* form is the hazard this invariant claims.

    If this test ever fails, langchain_core has changed its matching rules and the invariant
    should be re-derived rather than assumed — that is a good failure, not a bad one.
    """
    seen: dict[str, object] = {}

    @tool
    def probe(q: str, config: Annotated[Optional[RunnableConfig], InjectedToolArg] = None) -> str:
        """Canary tool using the forbidden widened annotation."""
        seen["config"] = config
        return "ok"

    probe.invoke({"q": "x"}, config={"configurable": {"user_role": "admin"}})
    assert seen.get("config") is None, (
        "Optional[RunnableConfig] now receives the run config — langchain_core's matching "
        "changed. Re-derive AGENTS.md invariant #6 before relaxing anything."
    )
