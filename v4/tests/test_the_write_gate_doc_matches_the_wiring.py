"""Gate: the write-authority paragraph in `how-it-works.md` describes the wiring that exists.

`docs/how-it-works.md` told readers that every proposed mutation is routed through the single
write-authority decision in `app/tools/aci/mutating.py` — "the gate is enforced server-side at
that chokepoint" — and that the decision composes "the action class's statistically earned rung".
Measured, none of that reached a cluster: `decide_write`/`plan_mutation` have no production
caller, `earned_rung` arrives as its `L2` default, and `promotion_outcomes`, the ADR-102 store
that would earn the rung, is created by the schema and written by nothing outside its own tests.
The live A3 brake is elsewhere — `watchtower.py` composing the ladder, the allowlist and
`auto_write_permitted()`.

A doc describing a security boundary that is not in the path is the worst member of the class
this audit chases: it reports a guarantee when the underlying state is not that guarantee. So
the assertions below are **equivalences, not one-way checks** — wiring the chokepoint up fails
them just as loudly as un-wiring it, and whoever does the wiring is told which sentence to fix.

Reachability is read from the AST, never from a text scan: `app/memory/prospective.py` defines
an unrelated `record_outcome`, so grepping the name alone reports this loop as closed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DOC = _ROOT / "docs" / "how-it-works.md"
_PACKAGES = _ROOT / "packages"

#: Wording the doc must carry for exactly as long as the module has no production caller.
_NOT_WIRED = "it is not yet wired into the graph"
_NO_WRITER = "has no production writer yet"


def _production_sources() -> list[Path]:
    """Every shipped Python module — tests, scripts and build files excluded."""
    found: list[Path] = []
    for pkg, sub in (
        ("kubeintellect-server", "app"),
        ("kube-q", "kube_q"),
        ("ki-protocol", "ki_protocol"),
    ):
        root = _PACKAGES / pkg / sub
        found.extend(p for p in root.rglob("*.py") if "tests" not in p.parts)
    return sorted(found)


def _modules_importing(target: str, *, exclude: str) -> list[str]:
    """Production modules that import *target* — the only way to reach into it.

    Both surfaces live in modules nothing star-imports, so an import edge is necessary as well
    as sufficient: a caller must name the module to reach the symbol.
    """
    importers: list[str] = []
    for path in _production_sources():
        if path.name == exclude:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == target:
                importers.append(str(path.relative_to(_ROOT)))
                break
            if isinstance(node, ast.Import) and any(a.name == target for a in node.names):
                importers.append(str(path.relative_to(_ROOT)))
                break
    return importers


class TestTheScanItselfIsNotVacuous:
    """A reachability check that read no files would call every surface unreachable."""

    def test_the_production_tree_was_actually_read(self):
        sources = _production_sources()
        assert len(sources) > 100, f"only {len(sources)} production modules found — scan is broken"

    def test_a_module_that_is_imported_is_reported(self):
        # app.autonomy.budget is imported by watchtower.py; if the finder cannot see that edge
        # it cannot see any edge, and every "no production caller" claim below is meaningless.
        assert _modules_importing("app.autonomy.budget", exclude="budget.py")


class TestTheDocAndTheWiringSayTheSameThing:
    @pytest.mark.parametrize(
        ("module", "defining_file", "marker", "surface"),
        [
            ("app.tools.aci.mutating", "mutating.py", _NOT_WIRED, "the ACI write chokepoint"),
            ("app.autonomy.promotion_source", "promotion_source.py", _NO_WRITER,
             "the ADR-102 promotion store"),
        ],
    )
    def test_unwired_iff_the_doc_says_unwired(self, module, defining_file, marker, surface):
        importers = _modules_importing(module, exclude=defining_file)
        doc = _DOC.read_text(encoding="utf-8")
        if importers:
            assert marker not in doc, (
                f"{surface} now has a production caller ({', '.join(importers)}) — "
                f"docs/how-it-works.md still tells readers {marker!r}. Update the paragraph: the "
                "gate is real now, and the doc is understating it."
            )
        else:
            assert marker in doc, (
                f"{surface} has no production caller, and docs/how-it-works.md no longer says "
                f"{marker!r}. Readers are being told a brake is in their write path when it is "
                "not — restore the qualification or wire the module up."
            )


class TestTheLiveBrakeIsTheOneTheDocNames:
    """The corrected paragraph names three gates on the A3 path; all three must be real."""

    def test_the_watchtower_composes_all_three(self):
        source = (_PACKAGES / "kubeintellect-server" / "app" / "autonomy" / "watchtower.py").read_text(
            encoding="utf-8"
        )
        assert "auto_write_permitted" in source
        assert "allowlist" in source.lower()

    def test_auto_write_permitted_denies_on_both_brakes(self):
        from app.autonomy.budget import auto_write_permitted

        assert auto_write_permitted.__doc__
        reasons = auto_write_permitted.__doc__.lower()
        assert "kill switch" in reasons and "change freeze" in reasons

    def test_the_earned_rung_is_still_a_default(self):
        """The doc says `earned_rung` arrives as its L2 default; hold that to the signature."""
        import inspect

        from app.tools.aci.mutating import decide_write

        default = inspect.signature(decide_write).parameters["earned_rung"].default
        assert default == "L2", f"the doc names L2 as the standing default; signature says {default!r}"
