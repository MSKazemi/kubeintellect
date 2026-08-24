"""`-n kube-system` was guarded; `-itn kube-system` was not, and kubectl cannot tell them apart.

`_flag_value` claimed "every form pflag accepts" and enumerated five. pflag accepts a sixth
family — the **combined shorthand group** — and the reader looked for an argument beginning with
`-n`, which `-Rn kube-system` does not. Everything in that family was invisible, so the namespace
never reached `_check_protected_access`: the guard did not decide `kube-system` was allowed, it
never saw a namespace at all.

Verified against kubectl v1.36.4 itself, not from memory, and not against a mock:

    kubectl apply -f - -Rn kube-system   parses          (no "unknown shorthand flag")
    kubectl apply -f - -Rwn ...          fails on `w` alone   -> the group IS decomposed L-to-R
    kubectl apply -f - -Rn               "flag needs an argument: 'n' in -n"
                                                         -> `-n` really consumes the next token

The boolean/value split below comes from the same place: `kubectl <verb> --help` prints `=false`
for a boolean and `=''`/`=[]` for a value flag, swept across every subcommand. `-f` and `-p` were
the only two letters that answer differently depending on the verb — boolean on `logs`
(`--follow`, `--previous`), a value flag everywhere else — which is why the verb is now threaded
through instead of guessed.

The direction that matters most here is the one that does *not* fire. A group is walked exactly
as pflag walks it, stopping at the first letter that takes a value, because scanning the whole
group for the target letter would read `-ojson` as a `--namespace` (there is an `n` in `json`)
and swallow whatever came next. Over-reading a guard is not the safe direction either: it would
refuse ordinary commands and teach an operator to route around the guard.
"""

from __future__ import annotations

import shlex

import pytest

from app.core.config import settings
from app.tools.kubectl_tool import (
    _check_protected_access,
    _extract_namespace,
    _extract_verb,
    _flag_value,
    _KUBECTL_BOOLEAN_SHORTHANDS,
    _VERB_BOOLEAN_SHORTHANDS,
)

BLOCKED = "kube-system"


def args_of(cmd: str) -> list[str]:
    """The production token shape — `kubectl` included, because `_operand_after_verb` locates
    the verb inside the list rather than trusting a fixed index."""
    return shlex.split(cmd)


def ns_of(cmd: str) -> str | None:
    a = args_of(cmd)
    return _extract_namespace(a, _extract_verb(a))


def refused(cmd: str) -> bool:
    a = args_of(cmd)
    return _check_protected_access(_extract_verb(a), a, None) is not None


# ── 1. the family that was invisible ──────────────────────────────────────────────────────────


class TestEveryFormKubectlParsesTheSame:
    @pytest.mark.parametrize("cmd", [
        # the five that already worked — kept so a regression here is loud
        f"kubectl get pods -n {BLOCKED}",
        f"kubectl get pods -n={BLOCKED}",
        f"kubectl get pods -n{BLOCKED}",
        f"kubectl get pods --namespace {BLOCKED}",
        f"kubectl get pods --namespace={BLOCKED}",
        # the sixth family, all of which reached the cluster unrefused before 2026-08-24
        f"kubectl get pods -Rn {BLOCKED}",
        f"kubectl get pods -Rn{BLOCKED}",
        f"kubectl get pods -Rn={BLOCKED}",
        f"kubectl get pods -An {BLOCKED}",
        f"kubectl get pods -wn {BLOCKED}",
        f"kubectl exec -itn {BLOCKED} mypod -- sh",
        f"kubectl exec -itn{BLOCKED} mypod -- sh",
        f"kubectl delete pod x -Rn {BLOCKED}",
    ])
    def test_the_namespace_is_seen(self, cmd):
        assert ns_of(cmd) == BLOCKED

    @pytest.mark.parametrize("cmd", [
        f"kubectl get pods -Rn {BLOCKED}",
        f"kubectl exec -itn {BLOCKED} mypod -- sh",
        f"kubectl delete pod x -itn {BLOCKED}",
    ])
    def test_and_the_guard_refuses_it(self, cmd):
        assert refused(cmd), "a protected namespace reached the cluster"

    def test_the_two_spellings_agree(self):
        """The whole point: two commands kubectl cannot tell apart must not get two answers."""
        assert refused(f"kubectl get pods -n {BLOCKED}") == refused(f"kubectl get pods -Rn {BLOCKED}")


# ── 2. the verb decides for -f and -p ─────────────────────────────────────────────────────────


class TestTheAmbiguousTwo:
    @pytest.mark.parametrize("cmd", [
        f"kubectl logs -fn {BLOCKED} mypod",
        f"kubectl logs -pn {BLOCKED} mypod",
        f"kubectl logs -fpn {BLOCKED} mypod",
    ])
    def test_boolean_on_logs(self, cmd):
        assert ns_of(cmd) == BLOCKED

    @pytest.mark.parametrize("cmd", [
        f"kubectl logs -fn {BLOCKED} mypod",
        f"kubectl logs -pn {BLOCKED} mypod",
    ])
    def test_the_guard_threads_the_verb_through_too(self, cmd):
        """`_extract_namespace` resolving `-fn` is worth nothing if `_check_protected_access`
        calls it without the verb. Reading the logs of a kube-system pod is exactly the access
        the blocklist exists to refuse."""
        assert refused(cmd)

    def test_and_an_ordinary_namespace_still_is_not_refused(self):
        assert not refused("kubectl logs -fn default mypod")

    def test_a_value_flag_everywhere_else(self):
        """`kubectl apply -fns.yaml` is a *filename*. Reading it as `-n s.yaml` would invent a
        namespace out of a path — the reason the verb is threaded through rather than assumed."""
        assert ns_of("kubectl apply -fns.yaml") is None

    def test_without_a_verb_it_stays_conservative(self):
        """`helm_tool` shares this parser and helm's shorthands were not measured here."""
        assert _flag_value(args_of(f"kubectl logs -fn {BLOCKED} mypod"), "-n", "--namespace") is None

    def test_logs_is_the_only_exception_recorded(self):
        assert set(_VERB_BOOLEAN_SHORTHANDS) == {"logs"}


# ── 3. the direction that must not fire ───────────────────────────────────────────────────────


class TestItDoesNotInventANamespace:
    @pytest.mark.parametrize("cmd", [
        "kubectl get ns -ojson",             # 'json' contains an n
        "kubectl get ns -oname",             # so does 'name'
        "kubectl apply -fns.yaml",           # so does a filename
        "kubectl get pods -lapp=nginx",      # and a label selector
        "kubectl get pods -o wide",
        "kubectl exec -it mypod -- sh",
        "kubectl get pods -",                # bare `-` is stdin, not a group
        "kubectl get pods",
    ])
    def test_no_namespace_is_read(self, cmd):
        assert ns_of(cmd) is None

    def test_a_value_letter_stops_the_walk(self):
        """`-ojson` must not be scanned past the `o`; pflag gives the rest to `--output`."""
        assert ns_of("kubectl get pods -ojson default") is None

    def test_a_real_namespace_after_a_value_flag_is_still_found(self):
        assert ns_of(f"kubectl get pods -ojson -n {BLOCKED}") == BLOCKED

    def test_an_ordinary_namespace_is_not_refused(self):
        assert not refused("kubectl get pods -Rn default")


# ── 4. the -o reader gained the same reach ────────────────────────────────────────────────────


class TestTheOutputReaderToo:
    @pytest.mark.parametrize("cmd,expected", [
        ("kubectl get ns -o json", "json"),
        ("kubectl get ns -ojson", "json"),
        ("kubectl get ns -o=json", "json"),
        ("kubectl get ns -Rojson", "json"),
        ("kubectl get ns -Ro json", "json"),
        ("kubectl get ns -Ro=json", "json"),
        ("kubectl get ns --output=json", "json"),
    ])
    def test_every_form(self, cmd, expected):
        a = args_of(cmd)
        assert _flag_value(a, "-o", "--output", _extract_verb(a)) == expected

    def test_the_blocked_namespace_filter_still_sees_the_format(self):
        """`_filter_namespace_output` reads `-o` to choose a parser; an unread format means the
        listing is filtered by the wrong one, or not at all."""
        from app.tools.kubectl_tool import _filter_namespace_output
        out = '{"items":[{"metadata":{"name":"kube-system"}},{"metadata":{"name":"shop"}}]}'
        filtered = _filter_namespace_output("get", args_of("kubectl get ns -Rojson"), out)
        assert "kube-system" not in filtered and "shop" in filtered


# ── 5. the tables are evidence, not recall ────────────────────────────────────────────────────


class TestTheShorthandTables:
    def test_no_letter_is_both_boolean_and_verb_scoped(self):
        for letters in _VERB_BOOLEAN_SHORTHANDS.values():
            assert not (letters & _KUBECTL_BOOLEAN_SHORTHANDS)

    @pytest.mark.parametrize("letter", "olcLk")
    def test_known_value_letters_are_not_listed_as_boolean(self, letter):
        """`-o -l -c -L -k` all take a value in kubectl v1.36.4. Listing one as boolean is how
        `-ojson` would start reading as `--namespace`."""
        assert letter not in _KUBECTL_BOOLEAN_SHORTHANDS

    def test_the_o_readers_pass_the_verb_for_consistency_not_for_behaviour(self):
        """An equivalent mutant, recorded rather than papered over.

        Dropping `verb` from the two `-o` readers changes nothing observable, and the honest
        reason is that neither can be reached with the one verb the table distinguishes:

        * `_filter_namespace_output` returns before the read unless `_extract_resource_type`
          names a namespace kind, and that returns None for every verb outside its resource set
          — `logs` is not in it;
        * `_filter_all_namespaces_output` returns before the read unless `--all-namespaces` is
          set, and `kubectl logs` has no such flag (v1.36.4, checked).

        They pass the verb so that a future caller does not have to rediscover why one reader
        threads it and another does not — not because a test can tell the difference today.
        """
        from app.tools.kubectl_tool import _extract_resource_type
        assert _extract_resource_type("logs", args_of("kubectl logs -o json mypod")) is None

    def test_the_blocklist_is_what_the_guard_actually_consults(self):
        """Vacuity guard: these tests mean nothing if kube-system stopped being blocked."""
        assert BLOCKED in settings.kubectl_blocked_namespaces
