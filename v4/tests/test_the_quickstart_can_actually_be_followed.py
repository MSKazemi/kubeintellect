"""Gate: the quickstart describes commands that exist, and no key page that does not.

Walked end to end on 2026-08-28. Three of its claims held — `pip install kube-q`,
Python 3.12+, and `kq` defaulting to `https://api.kubeintellect.com` (which answers
`/v1/healthz` 200). Two did not, and both are pinned here:

* **Step 1 sent the reader to a page that issues no key.** `kubeintellect.com/demo`
  is the browser terminal — "No sign-up required", no email field, no key. The
  mint route (`POST /v1/auth/demo-keys`) exists in this repository but is absent
  from the deployed API's OpenAPI document, which serves five paths in total. The
  docs described an intended flow as though it had shipped, down to a sample
  `ki-ro-…` token and a 30-day expiry.
* **The CLI table was four commands short** of what `app/cli.py` defines.

Neither is checkable by the doc-claims gate, which compares *numbers*. Both are
checkable here, offline, against the code rather than against the network.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCS = _REPO_ROOT / "v4" / "docs"
_QUICKSTART = _DOCS / "quickstart.md"
_CLI_SOURCE = (
    _REPO_ROOT / "v4" / "packages" / "kubeintellect-server" / "app" / "cli.py"
)


def _declared_subcommands() -> set[str]:
    """Every `sub.add_parser("name"` in the CLI, read from source.

    Read rather than executed: the parser is built inside `main()`, so importing
    the module does not expose it, and shelling out to `--help` would make this
    gate depend on a console script being installed.
    """
    source = _CLI_SOURCE.read_text(encoding="utf-8")
    names = set(re.findall(r'add_parser\(\s*"([a-z][a-z0-9-]*)"', source))
    return names


def _documented_subcommands() -> set[str]:
    """Every `kubeintellect <cmd>` named in the quickstart's CLI table."""
    text = _QUICKSTART.read_text(encoding="utf-8")
    return set(re.findall(r"`kubeintellect ([a-z][a-z0-9-]*)", text))


class TestTheCliTableNamesEveryCommand:
    def test_the_source_declares_a_plausible_number_of_commands(self):
        # Vacuity guard: an empty set on either side would make the comparison free.
        declared = _declared_subcommands()
        assert len(declared) >= 10, f"only found {len(declared)}: {sorted(declared)}"

    def test_the_quickstart_documents_a_plausible_number(self):
        assert len(_documented_subcommands()) >= 9

    def test_no_command_exists_that_the_quickstart_does_not_name(self):
        missing = sorted(_declared_subcommands() - _documented_subcommands())
        assert not missing, (
            f"{len(missing)} command(s) the CLI defines and the quickstart never "
            f"mentions: {missing}"
        )

    def test_no_command_is_documented_that_does_not_exist(self):
        # The worse direction: a reader typing a command that was never built.
        extra = sorted(_documented_subcommands() - _declared_subcommands())
        assert not extra, f"quickstart names command(s) the CLI does not define: {extra}"


class TestNoDocSendsTheReaderForAKeyThatIsNotIssued:
    """`kubeintellect.com/demo` is the browser terminal, not a key dispenser.

    Measured 2026-08-28: the page carries no email field, `POST /v1/auth/demo-keys`
    is not in the deployed API, and the hosted demo answers an unauthenticated
    request with 422 (schema validation) rather than 401. Re-adding the instruction
    should fail here rather than reach a reader who cannot follow it.
    """

    @staticmethod
    def _markdown_files() -> list[Path]:
        return sorted(_DOCS.rglob("*.md"))

    def test_there_are_docs_to_scan(self):
        assert len(self._markdown_files()) > 10

    def test_no_doc_tells_the_reader_to_enter_an_email_for_a_key(self):
        offenders = []
        for path in self._markdown_files():
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), start=1):
                low = line.lower()
                if "kubeintellect.com/demo" in low and "email" in low:
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{number}")
                elif "enter your email" in low:
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{number}")
        assert not offenders, (
            "docs still describe an email-for-a-key flow that the demo page does "
            f"not implement: {offenders}"
        )

    def test_no_doc_claims_a_demo_key_expiry_the_deployment_cannot_honour(self):
        offenders = []
        for path in self._markdown_files():
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                low = line.lower()
                if re.search(r"keys? (expire|last)", low) and "30 day" in low:
                    offenders.append(f"{path.relative_to(_REPO_ROOT)}:{number}")
        assert not offenders, (
            "docs promise a demo-key lifetime, but nothing issues a demo key: "
            f"{offenders}"
        )
