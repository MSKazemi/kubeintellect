"""A guard setting that parses cleanly and protects nothing.

`KUBECTL_BLOCKED_NAMESPACES` is the project's outermost blast-radius control: the namespaces
the agent may never read, mutate, or query logs and metrics for, at any role. Eight comparison
sites across `kubectl_tool`, `helm_tool` and `namespace_guard` read `<value>.lower() in blocked`
— the normalisation was applied to **one side only**. `Settings.kubectl_blocked_namespaces` kept
whatever case the operator typed.

Measured 2026-08-20 with `KUBECTL_BLOCKED_NAMESPACES="Kube-System"` — one capital letter, no
error anywhere:

    kubectl get pods -A                                  -> 2 kube-system rows in the output
    kubectl delete deployment coredns -n kube-system     -> ALLOWED (normally [Protected])
    blocked_namespace_in_query('{namespace="kube-system"}') -> None (allowed)
    ladder.level_for_namespace("kube-system")            -> "A1" (normally pinned "A0")

`ladder._normalise` carried the docstring *"Match how the kubectl tool compares namespaces, so
the two cannot disagree"* — it lower-cased the namespace under test while the set it compared
against was not lower-cased, and the kubectl gate itself normalised neither side. The claim was
false in both directions.

Kubernetes namespace names are RFC 1123 labels, hence always lowercase, so folding the
blocklist can only ever *add* protection. What folding cannot fix — a glob, a slash, a stray
character — is reported by `app.core.config_audit` instead of vanishing.
"""
from __future__ import annotations

import logging

import pytest
from app.autonomy import ladder
from app.core import config_audit
from app.core.config import settings
from app.tools import kubectl_tool as kt
from app.tools import namespace_guard as ng

_TABLE = """NAMESPACE     NAME                  READY   STATUS    RESTARTS   AGE
kube-system   coredns-abc           1/1     Running   0          9d
kube-system   etcd-control-plane    1/1     Running   0          9d
shop          web-1                 1/1     Running   0          2h
"""


@pytest.fixture
def blocked(monkeypatch):
    def _set(raw: str):
        monkeypatch.setattr(settings, "KUBECTL_BLOCKED_NAMESPACES", raw)
    return _set


class TestOneCapitalLetterUsedToDisableEveryGuard:
    """Each assertion is one of the four measured failures, now with the fix in place."""

    @pytest.mark.parametrize("raw", ["kube-system", "Kube-System", "KUBE-SYSTEM", " Kube-System "])
    def test_the_mutation_gate_refuses_however_the_operator_typed_it(self, blocked, raw):
        blocked(raw)
        hit = kt._check_protected_access("delete", ["deployment", "coredns", "-n", "kube-system"], "")
        assert hit is not None and "[Protected]" in hit

    @pytest.mark.parametrize("raw", ["kube-system", "Kube-System"])
    def test_all_namespaces_output_is_filtered(self, blocked, raw):
        blocked(raw)
        out = kt._filter_all_namespaces_output("get", ["pods", "--all-namespaces"], _TABLE)
        assert "kube-system" not in out
        assert "shop" in out  # only the protected namespace is removed

    @pytest.mark.parametrize("raw", ["kube-system", "Kube-System"])
    def test_the_logql_promql_guard_refuses(self, blocked, raw):
        blocked(raw)
        assert ng.blocked_namespace_in_query('{namespace="kube-system"} |= "error"') == "kube-system"

    @pytest.mark.parametrize("raw", ["kube-system", "Kube-System"])
    def test_the_autonomy_ladder_pins_it_to_a0(self, blocked, raw):
        blocked(raw)
        assert ladder.level_for_namespace("kube-system") == "A0"

    def test_a_shouted_command_line_is_refused_too(self, blocked):
        """The command line is written by an LLM, not validated by the API server, so the
        value is folded on that side as well rather than trusted to be lowercase."""
        blocked("kube-system")
        for args in (["deployment", "x", "-n", "KUBE-SYSTEM"],
                     ["deployment", "x", "-nKube-System"],
                     ["deployment", "x", "--namespace=Kube-System"]):
            hit = kt._check_protected_access("delete", args, "")
            assert hit is not None, args

    def test_an_unrelated_namespace_is_still_allowed(self, blocked):
        blocked("kube-system")
        assert kt._check_protected_access("get", ["pods", "-n", "shop"], "") is None
        assert ladder.level_for_namespace("shop") != "A0"


class TestTheLadderAndTheGateCannotDisagree:
    """`ladder._normalise` claims to match the kubectl tool. Asserted, not commented."""

    @pytest.mark.parametrize("ns", ["kube-system", "KUBE-SYSTEM", " kube-system", "shop", "Shop"])
    def test_pinned_to_a0_exactly_when_the_gate_refuses(self, blocked, ns):
        blocked("kube-system,monitoring")
        refused = kt._check_protected_access("get", ["pods", "-n", ns], "") is not None
        assert (ladder.level_for_namespace(ns) == "A0") is refused


class TestTheAuditNamesWhatFoldingCannotFix:
    def test_a_correct_config_has_no_problems(self, blocked):
        blocked("kube-system,monitoring,kube-public")
        assert config_audit.blocked_namespace_problems() == []

    def test_capitalisation_is_fixed_not_reported(self, blocked):
        # Folding makes it work, so warning about it would be noise.
        blocked("Kube-System")
        assert config_audit.blocked_namespace_problems() == []

    def test_padding_is_fixed_not_reported(self, blocked):
        blocked(" kube-system , monitoring ")
        assert config_audit.blocked_namespace_problems() == []

    def test_a_glob_is_reported_and_says_where_globs_do_work(self, blocked):
        blocked("kube-*")
        problems = config_audit.blocked_namespace_problems()
        assert len(problems) == 1
        assert "protects nothing" in problems[0]
        assert "AUTONOMY_A3_ALLOWLIST" in problems[0]

    def test_a_slash_is_reported(self, blocked):
        blocked("ingress/nginx")
        assert len(config_audit.blocked_namespace_problems()) == 1

    def test_an_over_long_name_is_reported(self, blocked):
        blocked("a" * 64)
        assert len(config_audit.blocked_namespace_problems()) == 1

    def test_exactly_63_characters_is_legal(self, blocked):
        blocked("a" * 63)
        assert config_audit.blocked_namespace_problems() == []

    def test_only_the_bad_entry_is_reported(self, blocked):
        blocked("kube-system,kube-*,monitoring")
        problems = config_audit.blocked_namespace_problems()
        assert len(problems) == 1 and "kube-*" in problems[0]

    def test_the_good_entries_still_protect(self, blocked):
        blocked("kube-system,kube-*")
        assert kt._check_protected_access("get", ["pods", "-n", "kube-system"], "") is not None


class TestSilentlyDroppedAutonomyConfig:
    def test_an_override_without_an_equals_is_reported(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTONOMY_NAMESPACE_LEVELS", "prod=A0,dev:A2")
        problems = config_audit.autonomy_override_problems()
        assert len(problems) == 1 and "dev:A2" in problems[0]

    def test_it_says_which_level_the_namespace_actually_gets(self, monkeypatch):
        """Dropping an override fails **open** — the namespace keeps the permissive default
        the override existed to tighten — so the message names that default."""
        monkeypatch.setattr(settings, "AUTONOMY_NAMESPACE_LEVELS", "prod:A0")
        monkeypatch.setattr(settings, "AUTONOMY_LEVEL", "A2")
        assert "A2" in config_audit.autonomy_override_problems()[0]

    def test_an_unknown_level_is_reported(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTONOMY_NAMESPACE_LEVELS", "prod=A9")
        problems = config_audit.autonomy_override_problems()
        assert len(problems) == 1 and "A9" in problems[0]

    def test_a_valid_override_is_not_reported(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTONOMY_NAMESPACE_LEVELS", "prod=A0,dev=A2")
        assert config_audit.autonomy_override_problems() == []

    def test_a_malformed_a3_entry_is_reported(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTONOMY_A3_ALLOWLIST", "CrashLoopBackOff/dev,ImagePull")
        problems = config_audit.a3_allowlist_problems()
        assert len(problems) == 1 and "ImagePull" in problems[0]

    def test_a_valid_a3_entry_is_not_reported(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTONOMY_A3_ALLOWLIST", "CrashLoopBackOff/dev-*")
        assert config_audit.a3_allowlist_problems() == []


class TestItIsImpossibleToMissAndNeverFatal:
    def test_startup_logs_every_problem_at_error_level(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "KUBECTL_BLOCKED_NAMESPACES", "kube-*,ingress/nginx")
        monkeypatch.setattr(settings, "AUTONOMY_NAMESPACE_LEVELS", "")
        monkeypatch.setattr(settings, "AUTONOMY_A3_ALLOWLIST", "")
        with caplog.at_level(logging.ERROR):
            problems = config_audit.log_guard_config_problems()
        assert len(problems) == 2
        assert caplog.text.count("guard_config_unenforceable") == 2

    def test_a_clean_config_logs_nothing(self, monkeypatch, caplog):
        monkeypatch.setattr(settings, "KUBECTL_BLOCKED_NAMESPACES", "kube-system")
        monkeypatch.setattr(settings, "AUTONOMY_NAMESPACE_LEVELS", "")
        monkeypatch.setattr(settings, "AUTONOMY_A3_ALLOWLIST", "")
        with caplog.at_level(logging.ERROR):
            assert config_audit.log_guard_config_problems() == []
        assert "guard_config_unenforceable" not in caplog.text

    def test_it_does_not_raise_on_any_input(self, monkeypatch):
        # A typo must never take the agent offline; it must only become impossible to miss.
        for raw in ("", ",,,", "   ", "*", "///", "a" * 300):
            monkeypatch.setattr(settings, "KUBECTL_BLOCKED_NAMESPACES", raw)
            config_audit.unenforceable_guard_config()

    async def test_v5_status_surfaces_it(self, monkeypatch):
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        monkeypatch.setattr(settings, "KUBECTL_BLOCKED_NAMESPACES", "kube-*")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            body = (await client.get("/v1/v5/status")).json()
        assert len(body["unenforceable_guard_config"]) == 1
        assert "kube-*" in body["unenforceable_guard_config"][0]

    async def test_v5_status_is_empty_when_the_config_is_enforceable(self, monkeypatch):
        from app.main import app
        from httpx import ASGITransport, AsyncClient

        monkeypatch.setattr(settings, "KUBECTL_BLOCKED_NAMESPACES", "kube-system")
        monkeypatch.setattr(settings, "AUTONOMY_NAMESPACE_LEVELS", "")
        monkeypatch.setattr(settings, "AUTONOMY_A3_ALLOWLIST", "")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            body = (await client.get("/v1/v5/status")).json()
        assert body["unenforceable_guard_config"] == []
