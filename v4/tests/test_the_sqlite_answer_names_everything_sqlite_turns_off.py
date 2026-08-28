"""Gate: the FAQ's SQLite answer accounts for every subsystem SQLite disables.

Walked on 2026-08-29 by following `docs/configuration.md`'s "pip install — complete
`.env` template" into a clean `HOME` and running `kubeintellect serve`. The documented
path works: auto-detection wrote `USE_SQLITE=true`, the server started, and `/healthz`
returned 200. Three subsystems announced themselves disabled on the way up:

    audit:           SQLite mode — audit logging disabled
    flight_recorder: SQLite mode — recording disabled
    memory:          SQLite mode — hierarchy disabled

`faq.md` listed the memory hierarchy and the flight recorder (plus two layers
downstream of them) and then closed with **"nothing else is"** — while the audit log,
the record of who asked for what, was also off and unnamed there. It is documented in
`operations.md` and `api-reference.md`; the defect was the FAQ's closing claim, which a
reader deciding whether SQLite is good enough for them is exactly the person to mislead.

This pins the *structure* rather than the prose: whatever set of subsystems the code
disables in SQLite mode, the FAQ answer has to name all of them. A fourth one added to
the code fails this test until the FAQ grows to match.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_FAQ = _REPO_ROOT / "v4" / "docs" / "faq.md"
_APP = _REPO_ROOT / "v4" / "packages" / "kubeintellect-server" / "app"

# subsystem log-prefix → the phrase the FAQ must use for it
_EXPECTED_PHRASE = {
    "audit": "audit log",
    "flight_recorder": "flight recorder",
    "memory": "memory hierarchy",
}


def _subsystems_disabled_by_sqlite() -> set[str]:
    """Every `<name>: SQLite mode — ... disabled` announcement, read from source."""
    found: set[str] = set()
    for path in _APP.rglob("*.py"):
        for match in re.finditer(
            r'"([a-z_]+): SQLite mode — [^"]*disabled"', path.read_text(encoding="utf-8")
        ):
            found.add(match.group(1))
    return found


def _sqlite_answer() -> str:
    text = _FAQ.read_text(encoding="utf-8")
    start = text.index("### Do I need PostgreSQL?")
    end = text.index("###", start + 10)
    # Whitespace-normalised: the answer is hand-wrapped prose, so a phrase this test
    # looks for can legitimately straddle a line break.
    return " ".join(text[start:end].lower().split())


class TestTheFaqAccountsForEverySqliteCasualty:
    def test_the_source_announces_a_known_set(self):
        """If this fails, the code grew or lost a subsystem — update both maps."""
        assert _subsystems_disabled_by_sqlite() == set(_EXPECTED_PHRASE), (
            "the set of subsystems that disable themselves in SQLite mode changed; "
            "the FAQ answer and this test's map must both be updated"
        )

    def test_the_faq_names_each_one(self):
        answer = _sqlite_answer()
        missing = [
            name
            for name in _subsystems_disabled_by_sqlite()
            if _EXPECTED_PHRASE[name] not in answer
        ]
        assert not missing, (
            "the FAQ's SQLite answer says 'nothing else is' disabled, but these "
            f"subsystems are and it does not name them: {missing}"
        )

    def test_the_answer_still_makes_the_closing_claim(self):
        """The claim is fine — it just has to be true. If it goes, so does this gate."""
        assert "nothing else is" in _sqlite_answer()
