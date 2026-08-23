"""The operator-tunable resource blocklist matched only the exact string that was typed.

`KUBECTL_BLOCKED_RESOURCES` is the half of the resource guard an operator is *invited* to
extend — the Helm values file says "Override to add …". The guard tests
`_resource_spellings(token) & blocked`, and that helper folds case and derives spellings for
the **token the model typed**; the configured set was passed through untouched.

Measured 2026-08-20 against the real guard (`_check_protected_access` on the full argv):

    KUBECTL_BLOCKED_RESOURCES  get configmap  get ConfigMap  get configmaps  get cm
    'configmap'                REFUSED        REFUSED        ALLOWED         ALLOWED
    'ConfigMap'                ALLOWED        ALLOWED        ALLOWED         ALLOWED
    'configmaps'               ALLOWED        ALLOWED        REFUSED         ALLOWED
    'CONFIGMAP'                ALLOWED        ALLOWED        ALLOWED         ALLOWED

`ConfigMap` is the spelling Kubernetes itself uses for `kind:`, and it blocked nothing at all.
`configmaps` is the spelling `kubectl api-resources` prints, and the singular entry did not
cover it. The credential floor (`ALWAYS_BLOCKED_RESOURCES`) was never affected — this
under-blocks what an operator added, it does not unblock Secrets.

Both sides of the comparison are now expanded through the same rules. Short names are *not*
derived — they come from API discovery and cannot be computed from a string — and that limit is
asserted here rather than papered over with a guessed table.
"""
from __future__ import annotations

import shlex

import pytest
from app.core import config_audit
from app.core.config import settings
from app.tools import helm_tool as ht
from app.tools import kubectl_tool as kt
from app.tools.kubectl_tool import _number_forms, _resource_spellings


def _refused(cmd: str) -> bool:
    args = shlex.split(cmd)
    assert args[0] == "kubectl", "the guard is fed the full argv, kubectl included"
    return kt._check_protected_access(kt._extract_verb(args), args, "") is not None


@pytest.fixture
def blocked_resources(monkeypatch):
    def _set(raw: str):
        monkeypatch.setattr(settings, "KUBECTL_BLOCKED_RESOURCES", raw)
    return _set


SETTING_SPELLINGS = ["configmap", "ConfigMap", "configmaps", "CONFIGMAP", " ConfigMaps "]
COMMAND_SPELLINGS = ["configmap", "ConfigMap", "configmaps", "CONFIGMAPS"]


class TestTheMeasuredMatrix:
    @pytest.mark.parametrize("setting", SETTING_SPELLINGS)
    @pytest.mark.parametrize("typed", COMMAND_SPELLINGS)
    def test_any_spelling_of_the_setting_blocks_any_spelling_of_the_command(
        self, blocked_resources, setting, typed
    ):
        blocked_resources(setting)
        assert _refused(f"kubectl get {typed} -n shop")

    @pytest.mark.parametrize("setting", SETTING_SPELLINGS)
    def test_it_blocks_writes_too_not_only_reads(self, blocked_resources, setting):
        blocked_resources(setting)
        assert _refused("kubectl delete configmaps app-config -n shop")

    @pytest.mark.parametrize("setting", SETTING_SPELLINGS)
    def test_it_blocks_the_manifest_form(self, blocked_resources, setting):
        blocked_resources(setting)
        args = ["kubectl", "apply", "-f", "-"]
        stdin = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\n"
        assert kt._check_protected_access("apply", args, stdin) is not None

    def test_an_unrelated_resource_is_still_allowed(self, blocked_resources):
        blocked_resources("configmaps")
        assert not _refused("kubectl get pods -n shop")
        assert not _refused("kubectl get deployments -n shop")


class TestTheCredentialFloorIsUnaffected:
    @pytest.mark.parametrize("setting", ["", "configmap", "ConfigMap", "CONFIGMAP", "nonsense"])
    @pytest.mark.parametrize("typed", ["secrets", "secret", "Secret", "sa",
                                       "serviceaccounts", "secrets.v1."])
    def test_credentials_are_refused_whatever_the_operator_configured(
        self, blocked_resources, setting, typed
    ):
        blocked_resources(setting)
        assert _refused(f"kubectl get {typed} -n shop")


class TestSingularAndPluralAreTheSameResource:
    @pytest.mark.parametrize("singular,plural", [
        ("configmap", "configmaps"),
        ("pod", "pods"),
        ("ingress", "ingresses"),          # -es, not -s: naive pluralisation gets this wrong
        ("networkpolicy", "networkpolicies"),   # -y → -ies
        ("storageclass", "storageclasses"),
    ])
    def test_each_form_derives_the_other(self, singular, plural):
        assert plural in _resource_spellings(singular)
        assert singular in _resource_spellings(plural)

    @pytest.mark.parametrize("entry,typed", [
        ("ingress", "ingresses"),
        ("ingresses", "ingress"),
        ("networkpolicy", "networkpolicies"),
        ("networkpolicies", "networkpolicy"),
    ])
    def test_the_guard_agrees_with_that(self, blocked_resources, entry, typed):
        blocked_resources(entry)
        assert _refused(f"kubectl get {typed} -n shop")

    def test_number_forms_of_an_empty_string_is_empty(self):
        assert _number_forms("") == set()

    def test_over_generation_is_harmless_because_it_names_nothing(self):
        # `configmapses` is derived and is not a resource; it can only ever fail to match.
        assert "configmapses" in _resource_spellings("configmaps")


class TestTheShortNameLimitIsRealAndStated:
    def test_a_short_name_is_not_derived(self, blocked_resources):
        """Documented limit, asserted so it cannot quietly change: short names come from API
        discovery, so `cm` cannot be computed from `configmaps`. The operator must list it."""
        blocked_resources("configmaps")
        assert not _refused("kubectl get cm -n shop")

    def test_listing_the_short_name_works(self, blocked_resources):
        blocked_resources("configmaps,cm")
        assert _refused("kubectl get cm -n shop")
        assert _refused("kubectl get configmap -n shop")

    def test_the_credential_short_name_is_covered_by_the_floor(self, blocked_resources):
        # `sa` is in `_RESOURCE_ALIASES` because the guard has a floor for credentials.
        blocked_resources("")
        assert _refused("kubectl get sa -n shop")


class TestHelmRendersTheSameJudgement:
    def test_a_rendered_manifest_kind_is_dropped(self, blocked_resources):
        blocked_resources("ConfigMap")
        manifest = ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\n"
                    "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: web\n")
        out = ht._strip_blocked_kinds(manifest)
        assert "ConfigMap" not in out
        assert "Service" in out


class TestTheAuditNamesAnEntryThatCannotMatch:
    def test_a_correct_list_has_no_problems(self, blocked_resources):
        blocked_resources("secret,secrets,configmaps,secrets.v1.")
        assert config_audit.blocked_resource_problems() == []

    def test_capitalisation_is_fixed_not_reported(self, blocked_resources):
        blocked_resources("ConfigMap")
        assert config_audit.blocked_resource_problems() == []

    def test_a_glob_is_reported(self, blocked_resources):
        blocked_resources("config*")
        problems = config_audit.blocked_resource_problems()
        assert len(problems) == 1 and "blocks nothing" in problems[0]

    def test_a_slashed_entry_is_reported(self, blocked_resources):
        blocked_resources("apps/deployments")
        assert len(config_audit.blocked_resource_problems()) == 1

    def test_it_reaches_the_combined_report(self, blocked_resources, monkeypatch):
        blocked_resources("config*")
        monkeypatch.setattr(settings, "KUBECTL_BLOCKED_NAMESPACES", "kube-system")
        monkeypatch.setattr(settings, "AUTONOMY_NAMESPACE_LEVELS", "")
        monkeypatch.setattr(settings, "AUTONOMY_A3_ALLOWLIST", "")
        assert len(config_audit.unenforceable_guard_config()) == 1


class TestEachLayerCarriesItsOwnWeight:
    """Three layers of this fix are individually redundant against the token-side expansion.

    Reverting them alone failed **zero** tests, which proves only that they are masked — so each
    is exercised here through the behaviour it uniquely provides: a configured entry in a form
    the typed token can never produce.
    """

    def test_a_fully_qualified_configured_entry_blocks_the_plain_token(self, blocked_resources):
        """`_blocked_resources()` — the entry is expanded, not just the token.

        `_resource_spellings("configmap")` cannot produce `configmaps.v1.`, so without expanding
        the configured side this entry matches nothing an operator would ever type.
        """
        blocked_resources("configmaps.v1.")
        assert _refused("kubectl get configmap -n shop")
        assert _refused("kubectl get configmaps -n shop")

    def test_helm_expands_the_configured_side_too(self, blocked_resources):
        blocked_resources("configmaps.v1.")
        manifest = ("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: app-config\n"
                    "---\napiVersion: v1\nkind: Service\nmetadata:\n  name: web\n")
        out = ht._strip_blocked_kinds(manifest)
        assert "ConfigMap" not in out
        assert "Service" in out

    def test_the_setting_itself_is_case_folded(self, blocked_resources):
        """`Settings.kubectl_blocked_resources` is the canonical, case-insensitive set — the
        guard should not have to re-fold it, and it is the only place that promise lives."""
        blocked_resources("ConfigMap, SECRETS ")
        parsed = settings.kubectl_blocked_resources
        assert "configmap" in parsed
        assert not any(entry != entry.lower() for entry in parsed)
