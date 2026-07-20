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
