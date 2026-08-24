"""Every declared v5 flag must either be consumed by the code, or be a KNOWN no-op.

Why this gate exists (2026-08-19). `v4/docs/v5-experimental-flags.md` is a **public** page in the
docs-site nav that lists ~60 `KI_V5_*` / `CORTEX_V5_*` flags with a description of what each one
does, and `core.version.active_experimental_flags()` reports **any** boolean v5 flag set to `True`
as an active experimental flag — in `/version`, in `version_line()`, and in the `kq v5-status`
surface. Nothing checked that a flag was wired to anything.

25 of them were not. Setting `KI_V5_RIGHTSIZING=true` changes no behaviour, and the product then
reports it as on. That is a silent no-op with a user-visible claim attached — the same failure class
as a detector predicate that can never fire, but on the configuration surface.

This gate does not force the debt to be paid; it stops it growing and makes it shrink monotonically.
Wiring a flag = deleting a line from `KNOWN_UNWIRED`. Adding a flag without wiring it = a red test.
"""
from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "packages" / "kubeintellect-server" / "app"
_CONFIG = _APP / "core" / "config.py"
_DOC = Path(__file__).resolve().parents[1] / "docs" / "v5-experimental-flags.md"
_VERSION = _APP / "core" / "version.py"
_ALLOWLIST_LITERAL = re.compile(r"UNWIRED_EXPERIMENTAL_FLAGS = frozenset\(\{.*?\n\}\)", re.S)
_FIELD_RE = re.compile(r"^\s{4}((?:KI_V5|CORTEX_V5)_[A-Z0-9_]+)\s*:", re.M)

# E402 on purpose: this import belongs next to the note below, which is the whole reason the
# allowlist lives in production code rather than in this file.
from app.core.version import UNWIRED_EXPERIMENTAL_FLAGS as KNOWN_UNWIRED  # noqa: E402

#: The allowlist now lives in PRODUCTION code (`app/core/version.py`), not here — because the
#: reporting surface has to consult it at runtime to avoid calling an unwired flag "active".
#: This module verifies that production set against real `settings.<FLAG>` consumption, so the two
#: cannot drift: wire a flag and forget to delete its entry, and `test_the_allowlist_shrinks…` fails.


def _declared() -> set[str]:
    return set(_FIELD_RE.findall(_CONFIG.read_text(encoding="utf-8")))


def _app_source() -> str:
    """All of `app/`, minus the two places a flag NAME appears without being consumed.

    `config.py` declares the fields; `version.py` holds the allowlist literal itself. Scanning
    either would make every flag look wired — the second one bit for real when the allowlist moved
    into production code: all 25 suddenly reported as fixed, because the set that records them as
    dead is written in the same string form a consumer would use.
    """
    parts = []
    for p in _APP.rglob("*.py"):
        if p == _CONFIG:
            continue
        text = p.read_text(encoding="utf-8")
        if p == _VERSION:
            text = _ALLOWLIST_LITERAL.sub("", text)
        parts.append(text)
    return "\n".join(parts)


def _unwired() -> set[str]:
    """Flags no code reads.

    Consumption means `settings.FLAG` or a `"FLAG"` string lookup — deliberately NOT bare textual
    presence, because the first version of this check counted a COMMENT mentioning
    `KI_V5_ACI_READ_VERBS_ENABLED` as a consumer and reported the flag as wired.
    """
    src = _app_source()
    return {f for f in _declared() if f"settings.{f}" not in src and f'"{f}"' not in src}


class TestV5FlagWiring:
    def test_no_new_unwired_flag_is_introduced(self):
        new = _unwired() - KNOWN_UNWIRED
        assert not new, (
            f"{len(new)} v5 flag(s) are declared and documented but read by no code: "
            f"{sorted(new)}. Wire them, or add them to KNOWN_UNWIRED with a reason."
        )

    def test_the_allowlist_shrinks_and_never_goes_stale(self):
        fixed = KNOWN_UNWIRED - _unwired()
        assert not fixed, (
            f"These flags are wired now — delete them from KNOWN_UNWIRED: {sorted(fixed)}"
        )

    def test_every_allowlisted_flag_still_exists(self):
        gone = KNOWN_UNWIRED - _declared()
        assert not gone, f"KNOWN_UNWIRED names flags that no longer exist: {sorted(gone)}"

    def test_the_reporting_surface_is_why_this_matters(self):
        # `active_experimental_flags()` reports any TRUE boolean v5 flag, wired or not — so an
        # unwired flag does not merely do nothing, it makes the product claim a feature is on.
        from app.core.version import active_experimental_flags
        assert callable(active_experimental_flags)
        src = (_APP / "core" / "version.py").read_text(encoding="utf-8")
        assert "isinstance(value, bool)" in src and "startswith(_EXPERIMENTAL_PREFIXES)" in src


class TestPublicDocTellsTheTruth:
    """`docs/v5-experimental-flags.md` is a published page in the docs-site nav.

    Non-negotiable: nothing representing this project may state something false. A row that
    describes what a flag does, with no hint that nothing reads it, is exactly that.
    """

    def _marked(self) -> set[str]:
        return set(
            re.findall(r"^\| `((?:KI_V5|CORTEX_V5)_[A-Z0-9_]+)` \u26a0\ufe0f \|", _DOC.read_text(encoding="utf-8"), re.M)
        )

    def test_the_doc_marks_exactly_the_unwired_flags(self):
        assert self._marked() == set(KNOWN_UNWIRED), (
            "docs/v5-experimental-flags.md disagrees with KNOWN_UNWIRED — "
            f"only in doc: {sorted(self._marked() - KNOWN_UNWIRED)}; "
            f"only in test: {sorted(KNOWN_UNWIRED - self._marked())}"
        )

    def test_the_doc_explains_the_marker(self):
        assert "declared but not wired" in _DOC.read_text(encoding="utf-8")

    def test_every_documented_flag_is_a_real_field(self):
        documented = set(re.findall(r"^\| `((?:KI_V5|CORTEX_V5)_[A-Z0-9_]+)`", _DOC.read_text(encoding="utf-8"), re.M))
        assert not documented - _declared(), f"documented but not declared: {sorted(documented - _declared())}"


class TestTheReportingSurfaceStopsLying:
    """Marking the docs was half the fix; at runtime the product still claimed the feature was on.

    An operator turning on `KI_V5_RIGHTSIZING` and reading it back from `/healthz`, `/v1/v5/status`
    or the startup log had every reason to believe rightsizing was live. These assert the two halves
    of the honest answer: it is NOT in the active set, and it IS reported as set-but-ineffective —
    silence would just move the lie from commission to omission.
    """

    def test_an_unwired_flag_is_not_reported_active(self, mocker):
        from app.core.config import settings as cfg
        from app.core import version as ver
        mocker.patch.object(cfg, "KI_V5_RIGHTSIZING", True)
        assert "KI_V5_RIGHTSIZING" not in ver.active_experimental_flags()

    def test_but_the_operator_is_told_it_does_nothing(self, mocker):
        from app.core.config import settings as cfg
        from app.core import version as ver
        mocker.patch.object(cfg, "KI_V5_RIGHTSIZING", True)
        assert ver.set_but_unwired_flags() == ["KI_V5_RIGHTSIZING"]
        assert "NOT WIRED" in ver.version_line()
        assert "KI_V5_RIGHTSIZING" in ver.version_info()["set_but_unwired_flags"]

    def test_a_wired_flag_is_unaffected(self, mocker):
        from app.core.config import settings as cfg
        from app.core import version as ver
        mocker.patch.object(cfg, "CORTEX_V5_ENABLED", True)
        assert "CORTEX_V5_ENABLED" in ver.active_experimental_flags()
        assert "CORTEX_V5_ENABLED" not in ver.set_but_unwired_flags()

    def test_the_baseline_is_still_silent(self):
        # Nothing set ⇒ no scary "NOT WIRED" noise on a default install.
        from app.core import version as ver
        assert ver.set_but_unwired_flags() == []
        assert "NOT WIRED" not in ver.version_line()

    def test_both_endpoints_expose_it(self):
        from app.api.v1.endpoints.health import HealthResponse
        from app.api.v1.endpoints.v5_status import V5Status
        assert "set_but_unwired_flags" in HealthResponse.model_fields
        assert "set_but_unwired_flags" in V5Status.model_fields


class TestTheCliRendersEveryFieldTheApiReturns:
    """`kq v5-status` builds its table by hand, one `add_row` per field — so a field added to the
    API response is *silently* absent from the CLI until someone notices.

    That is how `set_but_unwired_flags` nearly ended up server-only: the endpoint told the truth and
    the command still printed `active_flags → (none — v4 baseline)` to an operator who had set a
    flag. The generalisation of every defect found this week is the same — a surface that answers
    confidently while omitting the part that matters. This gate makes the omission loud.
    """

    _CMD = Path(__file__).resolve().parents[1] / "packages" / "kube-q" / "kube_q" / "cli" / "v5_status_cmd.py"

    def test_the_command_file_is_where_we_think(self):
        assert self._CMD.exists(), f"kq v5-status command not found at {self._CMD}"

    def test_no_response_field_is_dropped_by_the_cli(self):
        from app.api.v1.endpoints.v5_status import V5Status
        src = self._CMD.read_text(encoding="utf-8")
        missing = [f for f in V5Status.model_fields if f'"{f}"' not in src]
        assert not missing, (
            f"GET /v1/v5/status returns {missing} and `kq v5-status` never mentions it — the CLI "
            "would print an incomplete table with no sign anything was left out."
        )
