"""Gate: numbered doc claims stay in sync with the code.

Loads scripts/check_doc_claims.py and asserts it finds no drift. If someone adds
a playbook, enables a provider, or introduces a KI_V5_* flag without updating the
docs, this test fails with the exact mismatch.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_doc_claims.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_doc_claims", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass can resolve cls.__module__.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_doc_claims_match_code():
    checker = _load_checker()
    errors = checker.run_checks()
    assert errors == [], "Doc-claims drift:\n" + "\n".join(errors)


def test_canonical_values_are_sane():
    """Guard the extractor itself — if these go to 0 the checker is silently broken."""
    checker = _load_checker()
    c = checker._canonical()
    assert c.playbook_count >= 18
    assert c.detector_count >= 1
    assert {"openai", "azure", "qwen", "anthropic"} <= c.providers
    assert c.flag_count >= 40


def test_fix_rewrites_only_the_digits(tmp_path, monkeypatch):
    """`--fix` must replace capture group 1 and nothing else.

    The naive implementation substitutes the whole match and silently eats the prose
    around the number, which would turn a drifted count into a mangled sentence.
    """
    checker = _load_checker()
    (tmp_path / "sample.md").write_text(
        "KubeIntellect ships **23 built-in playbooks** — deterministic.\n", encoding="utf-8"
    )
    monkeypatch.setattr(checker, "_DOCS", tmp_path)

    changed = checker._fix_number("sample.md", r"\*\*(\d+) built-in playbooks\*\*", 24)

    assert changed == 1
    assert (tmp_path / "sample.md").read_text(encoding="utf-8") == (
        "KubeIntellect ships **24 built-in playbooks** — deterministic.\n"
    )


def test_fix_is_idempotent_and_does_not_touch_an_already_correct_doc(tmp_path, monkeypatch):
    checker = _load_checker()
    doc = tmp_path / "sample.md"
    doc.write_text("ships **24 built-in playbooks** here.\n", encoding="utf-8")
    monkeypatch.setattr(checker, "_DOCS", tmp_path)
    before = doc.read_text(encoding="utf-8")

    changed = checker._fix_number("sample.md", r"\*\*(\d+) built-in playbooks\*\*", 24)

    assert changed == 0
    assert doc.read_text(encoding="utf-8") == before


def test_every_claim_pattern_still_matches_its_doc():
    """Guard the guard: a claim whose regex matches nothing has silently stopped guarding.

    Renaming a heading or rewording a sentence is enough to orphan a pattern, and a gate
    that checks zero occurrences passes forever without ever being wrong out loud.
    """
    import re

    checker = _load_checker()
    claims = checker._numeric_claims(checker._canonical())
    assert len(claims) >= 12, "claim table shrank — a doc surface stopped being guarded"

    dead = [
        f"{doc} /{pattern}/ ({label})"
        for doc, pattern, _expected, label in claims
        if not re.search(pattern, checker._read(doc))
    ]
    assert dead == [], "claim patterns that match nothing:\n" + "\n".join(dead)
