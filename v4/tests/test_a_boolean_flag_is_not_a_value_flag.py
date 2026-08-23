"""One wrong row in the flag table moved every gate in the module at once.

Passes 51, 98, 99 and 101 each found a gate reading the command with its own idea of where the
verb was, and each was fixed by routing it through the one shared walk, `_skip_flags`. That walk
consults a table, `_VALUE_FLAGS`, to decide whether a flag consumes the token after it.

`--warnings-as-errors` was in that table. It is a **boolean** — pflag accepts it as a bare token
— so the walk consumed the *verb* as its value and every gate downstream read the token after
the verb instead. Sharing the parse had made the blast radius total rather than local. Measured
2026-08-20, with `hitl_bypass` on (an `auto_approve` session or "approve all"):

    kubectl get secrets -n prod                          -> [Protected], refused
    kubectl --warnings-as-errors get secrets -n prod     -> RAN, Secret rows returned
    kubectl --warnings-as-errors get sa -n prod          -> RAN, ServiceAccount rows returned
    kubectl --warnings-as-errors delete namespace shop   -> RAN, no always-confirm prompt

and at the gate level the verb read as `secrets` / `namespace` / `image`, so
`_extract_resource_type` returned None (the Secret blocklist compares against nothing),
`_classify_risk` fell from `high` to `medium`, and `_requires_always_confirm` — the gate that
fires *through* `hitl_bypass` — returned False.

Two things are asserted here. The narrow one: the table holds no boolean. The general one, which
is what the family actually deserves — **a flag that carries no meaning about what is being asked
must not change any gate's answer.** That property is checked across a corpus of commands, so the
next wrong row fails a test rather than a cluster.
"""
from __future__ import annotations

import shlex
from unittest.mock import MagicMock, patch

import pytest

from app.tools import kubectl_tool as kt
from app.tools.kubectl_tool import run_kubectl

# Boolean globals that say nothing about what is being asked. `--insecure-skip-tls-verify` is
# deliberately absent: it *is* meaningful — it is a connection flag and is refused outright.
_INERT = ["--warnings-as-errors", "--disable-compression", "--match-server-version"]

_COMMANDS = [
    "kubectl get pods -n prod",
    "kubectl get secrets -n prod",
    "kubectl get sa -n prod",
    "kubectl get ns",
    "kubectl delete pod api-0 -n prod",
    "kubectl delete namespace shop",
    "kubectl delete pv my-volume",
    "kubectl set image deploy/api api=nginx -n prod",
    "kubectl drain node-1",
    "kubectl rollout status deploy/api -n prod",
    "kubectl rollout restart deploy/api -n prod",
    "kubectl describe ns",
    "kubectl logs api-0 -n prod",
]


def _decisions(command: str) -> dict:
    """Every gate in `run_kubectl` that reasons about what the command asks for."""
    args = shlex.split(command)
    verb = kt._extract_verb(args)
    return {
        "verb": verb,
        "write": kt._is_write_verb(verb, args),
        "risk": kt._classify_risk(verb, args),
        "always_confirm": kt._requires_always_confirm(verb, args),
        "resource": kt._extract_resource_type(verb, args),
        "blocked_resource": kt._blocked_resource_hit(verb, args),
        "targeted_namespaces": kt._targeted_namespaces(verb, args),
        "namespace": kt._extract_namespace(args),
        "all_namespaces": kt._is_all_namespaces(args),
        "destructive": sorted(kt._destructive_verbs_in(args)),
    }


def _prefixed(command: str, flag: str) -> str:
    head, tail = command.split(" ", 1)
    return f"{head} {flag} {tail}"


class TestTheTableHoldsNoBoolean:

    def test_the_two_sets_are_disjoint(self):
        """The invariant that would have caught this before it shipped."""
        assert kt._VALUE_FLAGS & kt._BOOLEAN_GLOBAL_FLAGS == frozenset()

    def test_the_boolean_set_actually_names_the_booleans(self):
        """Disjointness from an empty set is true and worthless.

        Without this, deleting the contents of `_BOOLEAN_GLOBAL_FLAGS` keeps every assertion in
        this class green while removing the only thing that guards the table.
        """
        assert set(_INERT) <= kt._BOOLEAN_GLOBAL_FLAGS
        assert "--insecure-skip-tls-verify" in kt._BOOLEAN_GLOBAL_FLAGS

    def test_warnings_as_errors_is_not_a_value_flag(self):
        assert "--warnings-as-errors" not in kt._VALUE_FLAGS

    @pytest.mark.parametrize("flag", sorted(_INERT))
    def test_a_bare_boolean_does_not_swallow_the_verb(self, flag):
        assert kt._extract_verb(["kubectl", flag, "delete", "pod", "x"]) == "delete"

    @pytest.mark.parametrize("flag", sorted(_INERT))
    def test_nor_does_its_explicit_false_form(self, flag):
        """pflag also accepts `--flag=false`; `=` already stopped the walk, so this pins it."""
        assert kt._extract_verb(["kubectl", f"{flag}=false", "delete", "pod", "x"]) == "delete"


class TestAnInertFlagChangesNoGate:
    """The general property. A flag that says nothing about the request decides nothing."""

    @pytest.mark.parametrize("command", _COMMANDS)
    @pytest.mark.parametrize("flag", sorted(_INERT))
    def test_the_decisions_are_identical_with_the_flag_in_front(self, command, flag):
        assert _decisions(_prefixed(command, flag)) == _decisions(command)

    @pytest.mark.parametrize("command", _COMMANDS)
    def test_and_with_all_of_them_at_once(self, command):
        stacked = command
        for flag in _INERT:
            stacked = _prefixed(stacked, flag)
        assert _decisions(stacked) == _decisions(command)

    @pytest.mark.parametrize("command", _COMMANDS)
    def test_and_when_it_trails_the_command(self, command):
        assert _decisions(f"{command} --warnings-as-errors") == _decisions(command)


class TestTheGuardsThemselvesThroughTheRealTool:

    _BYPASS = {"configurable": {"user_role": "admin", "thread_id": "t", "hitl_bypass": True}}
    _ROWS = "NAME       TYPE     DATA\ndb-creds   Opaque   1\n"

    def _run(self, command: str) -> tuple[str, bool]:
        with patch("subprocess.run") as run:
            proc = MagicMock()
            proc.stdout, proc.stderr, proc.returncode = self._ROWS, "", 0
            run.return_value = proc
            try:
                return str(run_kubectl.invoke({"command": command, "stdin": None},
                                              config=self._BYPASS)), run.called
            except KeyError as exc:
                if "__pregel_scratchpad" not in str(exc):
                    raise
                return "<HITL>", run.called

    @pytest.mark.parametrize("command", [
        "kubectl --warnings-as-errors get secrets -n prod",
        "kubectl --warnings-as-errors get sa -n prod",
        "kubectl --disable-compression get secrets -n prod",
        "kubectl --warnings-as-errors --match-server-version get secrets -n prod",
    ])
    def test_the_secret_block_still_fires(self, command):
        out, ran = self._run(command)
        assert "[Protected]" in out and not ran, f"{command!r} returned credential rows"

    @pytest.mark.parametrize("command", [
        "kubectl --warnings-as-errors delete namespace shop",
        "kubectl --warnings-as-errors delete pv my-volume",
        "kubectl --warnings-as-errors set image deploy/api api=nginx -n prod",
    ])
    def test_the_always_confirm_gate_still_fires_through_the_bypass(self, command):
        out, ran = self._run(command)
        assert out == "<HITL>" and not ran, f"{command!r} ran unprompted"

    def test_an_ordinary_read_is_not_over_blocked(self):
        out, ran = self._run("kubectl --warnings-as-errors get pods -n prod")
        assert ran and "[Protected]" not in out


class TestTheKnownGapIsStated:
    """`-v` (klog verbosity) takes a value and is *not* in the table. That is fail-closed."""

    def test_dash_v_reads_its_value_as_the_verb_and_therefore_gates(self):
        args = shlex.split("kubectl -v 6 get pods")
        verb = kt._extract_verb(args)
        assert verb == "6"
        assert kt._is_write_verb(verb, args) is True, "an unknown verb must count as a write"

    def test_the_equals_form_is_parsed_correctly(self):
        assert kt._extract_verb(shlex.split("kubectl -v=6 get pods")) == "get"
