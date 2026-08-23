"""The reporter built to catch guards that protect nothing had that exact hole.

`config_audit` exists for one reason: an operator who configured a protection deserves to be told
when it does nothing. `/v1/v5/status` surfaces it as `unenforceable_guard_config` and `kq v5-status`
prints it.

`autonomy_override_problems` checked that an entry had an `=` and that the *level* was one of
A0–A3. It never checked the namespace the entry names. The lookup is an exact dict hit on a
lowercased key, so any entry whose namespace is not a real namespace name parses cleanly, stores a
key nothing will ever match, and is reported as fine.

Measured 2026-08-20 with `AUTONOMY_LEVEL=A3` and `AUTONOMY_NAMESPACE_LEVELS="prod-*=A0"` — a
natural thing to write, because the sibling `AUTONOMY_A3_ALLOWLIST` **does** take globs and
`docs/configuration.md` says so in the same breath:

    unenforceable_guard_config()                    []          <-- "nothing wrong"
    GET /v1/v5/status  unenforceable_guard_config   []
    level_for_namespace("prod-web")                 A3          <-- they asked for A0
    a3_allowed("CrashLoopBackOff", "prod-web")      True        <-- with CrashLoopBackOff/prod-*

So the guard was inert, the reporter that exists to catch inert guards said nothing, and the
auto-fix path was live in precisely the namespaces the operator had tried to pin to
investigate-only. This failure mode is fail-**open**, which is what makes it worth more than the
tidiness it looks like.

Silent for a glob (`*`, `?`), a slash, and an embedded space. `a3_allowlist_problems` had the same
class of gap — empty playbook, empty pattern, a second `/` — failing closed rather than open, so a
correctness report rather than a hole, which is what its own docstring already claimed to be.

The empty key is deliberately still not reported: `level_for_namespace("")` is the cluster-scoped
lookup, so `=A0` genuinely pins cluster-scoped objects. Undocumented, but not inert — and calling
it "protects nothing" would have been the same kind of false statement this module exists to stop.
"""
from __future__ import annotations

import pytest
from app.autonomy.ladder import a3_allowed, level_for_namespace
from app.core.config import settings
from app.core.config_audit import (
    a3_allowlist_problems,
    autonomy_override_problems,
    unenforceable_guard_config,
)


@pytest.fixture(autouse=True)
def _clean_guard_config(mocker):
    """No brake, no override, no allowlist — each test declares exactly what it needs."""
    mocker.patch.object(settings, "AUTONOMY_LEVEL", "A3")
    mocker.patch.object(settings, "AUTONOMY_NAMESPACE_LEVELS", "")
    mocker.patch.object(settings, "AUTONOMY_A3_ALLOWLIST", "")
    yield


def _overrides(mocker, raw: str) -> None:
    mocker.patch.object(settings, "AUTONOMY_NAMESPACE_LEVELS", raw)


def _allowlist(mocker, raw: str) -> None:
    mocker.patch.object(settings, "AUTONOMY_A3_ALLOWLIST", raw)


#: (entry, a namespace it was plainly meant to cover) — every one of these parses and matches
#: nothing. A namespace name is an RFC 1123 label, so none of these can ever be one.
UNMATCHABLE_OVERRIDES = [
    ("prod-*=A0", "prod-web"),
    ("prod-?=A0", "prod-1"),
    ("*=A0", "anything"),
    ("prod/web=A0", "prod-web"),
    ("prod web=A0", "prod-web"),
    ("PROD_WEB=A0", "prod-web"),          # underscore is not legal in a namespace
    ("prod-web-=A0", "prod-web"),         # trailing hyphen
    ("-prod=A0", "prod"),
]

#: Spellings that really do pin the namespace. A reporter that flags these is an outage.
WORKING_OVERRIDES = [
    ("prod-web=A0", "prod-web"),
    ("Prod-Web=A0", "prod-web"),          # folded on both sides
    ("  prod-web  =  A0  ", "prod-web"),  # whitespace is stripped
    ("prod-web=A1", "prod-web"),
    ("a=A0", "a"),                        # a single character is a legal label
    ("prod-web-1=A0", "prod-web-1"),
]


class TestAnOverrideThatCanNeverMatchIsReported:

    @pytest.mark.parametrize("entry,namespace", UNMATCHABLE_OVERRIDES)
    def test_it_really_does_not_take_effect(self, mocker, entry, namespace):
        """The premise: these are inert. Asserted first, so the report is not the only evidence."""
        _overrides(mocker, entry)
        assert level_for_namespace(namespace) == "A3", (
            f"{entry!r} unexpectedly pinned {namespace!r} — it is not the silent no-op this "
            "test is about"
        )

    @pytest.mark.parametrize("entry,namespace", UNMATCHABLE_OVERRIDES)
    def test_and_the_operator_is_told(self, mocker, entry, namespace):
        _overrides(mocker, entry)
        problems = autonomy_override_problems()
        assert problems, f"{entry!r} protects nothing and was reported as fine"
        assert entry.strip() in problems[0], "the report must quote the entry the operator wrote"

    @pytest.mark.parametrize("entry,namespace", UNMATCHABLE_OVERRIDES)
    def test_it_reaches_the_surface_an_operator_reads(self, mocker, entry, namespace):
        _overrides(mocker, entry)
        assert any("AUTONOMY_NAMESPACE_LEVELS" in p for p in unenforceable_guard_config())

    @pytest.mark.parametrize("entry", ["prod-*=A0", "prod-?=A0"])
    def test_a_glob_says_where_globs_do_work(self, mocker, entry):
        """The mistake is invited by the sibling setting, so the message must name it."""
        _overrides(mocker, entry)
        assert "AUTONOMY_A3_ALLOWLIST" in autonomy_override_problems()[0]


class TestTheSpellingsThatWorkAreLeftAlone:

    @pytest.mark.parametrize("entry,namespace", WORKING_OVERRIDES)
    def test_the_override_takes_effect(self, mocker, entry, namespace):
        _overrides(mocker, entry)
        assert level_for_namespace(namespace) == entry.split("=")[1].strip()

    @pytest.mark.parametrize("entry,namespace", WORKING_OVERRIDES)
    def test_and_nothing_is_reported(self, mocker, entry, namespace):
        _overrides(mocker, entry)
        assert autonomy_override_problems() == [], (
            "a working override was reported as unenforceable — a false alarm here trains an "
            "operator to ignore the whole field"
        )

    def test_the_empty_key_is_deliberately_not_reported(self, mocker):
        """`level_for_namespace("")` is the cluster-scoped lookup, so `=A0` really does pin it."""
        _overrides(mocker, "=A0")
        assert level_for_namespace("") == "A0"
        assert autonomy_override_problems() == []

    def test_the_pre_existing_checks_still_fire(self, mocker):
        for entry in ("prod-web", "prod-web=A9", "prod-web=lenient"):
            _overrides(mocker, entry)
            assert autonomy_override_problems(), f"{entry!r} stopped being reported"

    def test_a_bad_level_is_reported_once_not_twice(self, mocker):
        """The level branch returns early; a legal namespace must not also trip the name check."""
        _overrides(mocker, "prod-web=A9")
        assert len(autonomy_override_problems()) == 1


class TestTheFullChainTheOperatorWouldHaveLivedThrough:
    """Guard inert + reporter silent + auto-fix live, in the namespaces they meant to protect."""

    def test_the_glob_override_left_auto_fix_enabled(self, mocker):
        _overrides(mocker, "prod-*=A0")
        _allowlist(mocker, "CrashLoopBackOff/prod-*")
        assert level_for_namespace("prod-web") == "A3"
        assert a3_allowed("CrashLoopBackOff", "prod-web") is True
        assert unenforceable_guard_config(), (
            "the one thing that would have told them is the report — it must not be empty"
        )

    def test_the_exact_spelling_shuts_auto_fix_off(self, mocker):
        """The fix they would reach for after reading the report."""
        _overrides(mocker, "prod-web=A0")
        _allowlist(mocker, "CrashLoopBackOff/prod-*")
        assert level_for_namespace("prod-web") == "A0"
        assert a3_allowed("CrashLoopBackOff", "prod-web") is False
        assert unenforceable_guard_config() == []


class TestTheAllowlistHadTheSameClassOfGap:
    """Fails closed, so a correctness report — which is exactly what its docstring claims."""

    @pytest.mark.parametrize("entry,playbook,namespace", [
        ("CrashLoopBackOff/", "CrashLoopBackOff", "prod"),
        ("/prod", "CrashLoopBackOff", "prod"),
        ("CrashLoopBackOff/prod/x", "CrashLoopBackOff", "prod"),
    ])
    def test_an_entry_that_matches_nothing_is_reported(self, mocker, entry, playbook, namespace):
        _allowlist(mocker, entry)
        assert a3_allowed(playbook, namespace) is False, "premise: this entry allows nothing"
        assert a3_allowlist_problems(), f"{entry!r} allows nothing and was reported as fine"

    @pytest.mark.parametrize("entry,playbook,namespace", [
        ("CrashLoopBackOff/prod", "CrashLoopBackOff", "prod"),
        ("CrashLoopBackOff/prod-*", "CrashLoopBackOff", "prod-web"),
        ("  CrashLoopBackOff / prod  ", "CrashLoopBackOff", "prod"),
    ])
    def test_a_working_entry_is_left_alone(self, mocker, entry, playbook, namespace):
        _allowlist(mocker, entry)
        assert a3_allowed(playbook, namespace) is True, "premise: this entry allows the pair"
        assert a3_allowlist_problems() == []

    def test_the_pre_existing_no_slash_check_still_fires(self, mocker):
        _allowlist(mocker, "CrashLoopBackOff")
        assert a3_allowlist_problems()

    def test_globs_are_not_reported_here_because_they_work_here(self, mocker):
        """The asymmetry between the two settings is the point, so it is asserted."""
        _allowlist(mocker, "CrashLoopBackOff/prod-*")
        assert a3_allowlist_problems() == []
        _overrides(mocker, "prod-*=A0")
        assert autonomy_override_problems() != []


class TestTheReportIsStillEmptyOnADefaultDeployment:
    """The whole surface is only useful if it is silent when there is nothing to say."""

    def test_no_guard_config_means_no_problems(self, mocker):
        assert unenforceable_guard_config() == []

    def test_the_shipped_defaults_are_enforceable(self, mocker):
        """Whatever the defaults are, they must not trip the reporter."""
        mocker.stopall()
        assert unenforceable_guard_config() == [], (
            "the shipped default configuration reports itself as unenforceable"
        )
