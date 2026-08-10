"""Production-grade reflexion subsystem tests.

Covers R1, R2, R3, R5, R6, R8 in pure-Python form (no Postgres needed).
The verify-resolution path (R4) is exercised in integration tests against
a real cluster — see evaluation/scenarios/.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

# ── R8: secret redaction ──────────────────────────────────────────────────────


class TestRedaction:
    def test_password_line_redacted(self):
        from app.utils.redact import redact_secrets
        text = "name: api\npassword: hunter2\nport: 8080"
        out = redact_secrets(text)
        assert "hunter2" not in out
        assert "<redacted>" in out

    def test_token_redacted_inline(self):
        from app.utils.redact import redact_secrets
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abcdefghij"
        out = redact_secrets(text)
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
        assert "<redacted" in out

    def test_internal_url_host_stripped(self):
        from app.utils.redact import redact_secrets
        out = redact_secrets("server: https://internal-prod-api.acme.local:8443/api")
        assert "internal-prod-api.acme.local" not in out
        assert "https://<redacted-host>" in out

    def test_truncation_at_max_chars(self):
        from app.utils.redact import redact_secrets
        # Realistic long input — many short non-token-matching lines.
        big = "\n".join(f"line {i}: ok" for i in range(500))
        out = redact_secrets(big, max_chars=200)
        assert len(out) <= 200
        assert out.endswith("[...]")

    def test_empty_string_safe(self):
        from app.utils.redact import redact_secrets
        assert redact_secrets("") == ""
        assert redact_secrets(None) == ""


# ── R2: structured outcome key ───────────────────────────────────────────────


class TestOutcomeKey:
    def _state(self, **overrides):
        base = {
            "messages": [HumanMessage(content="check ns scenario-test")],
            "matched_playbooks": [],
            "cluster_id": "kind-test:abc12345",
        }
        base.update(overrides)
        return base

    def test_key_uses_playbooks_when_present(self):
        from app.agent.nodes.coordinator import _outcome_key
        state = self._state(matched_playbooks=["CommandHardcodedFailure", "ServiceNoEndpoints"])
        key = _outcome_key(state, namespace="scenario-test")
        assert "playbook=CommandHardcodedFailure+ServiceNoEndpoints" in key
        assert "ns=scenario-test" in key
        assert "cluster=kind-test:abc12345" in key

    def test_key_falls_back_to_query_when_no_playbook(self):
        from app.agent.nodes.coordinator import _outcome_key
        state = self._state(matched_playbooks=[])
        key = _outcome_key(state, namespace="default")
        assert key.startswith("query=")
        assert "cluster=kind-test:abc12345" in key

    def test_key_sorts_playbooks_for_stability(self):
        from app.agent.nodes.coordinator import _outcome_key
        s1 = self._state(matched_playbooks=["B", "A", "C"])
        s2 = self._state(matched_playbooks=["C", "A", "B"])
        assert _outcome_key(s1, namespace="x") == _outcome_key(s2, namespace="x")


# ── R3: manifest-aware mutation pair extraction ──────────────────────────────


class TestMutationPairs:
    def _ai(self, *commands_and_yamls):
        """Each tuple = (command, stdin_yaml)."""
        return AIMessage(
            content="",
            tool_calls=[
                {"id": str(i), "name": "run_kubectl",
                 "args": {"command": cmd, "stdin": yml}}
                for i, (cmd, yml) in enumerate(commands_and_yamls)
            ],
        )

    def test_extracts_command_and_yaml(self):
        from app.agent.nodes.coordinator import _extract_mutation_pairs
        msgs = [self._ai(("kubectl apply -f -", "apiVersion: v1\nkind: Pod"))]
        pairs = _extract_mutation_pairs(msgs)
        assert len(pairs) == 1
        assert pairs[0]["command"].startswith("kubectl apply")
        assert pairs[0]["stdin_yaml"].startswith("apiVersion: v1")

    def test_skips_read_commands(self):
        from app.agent.nodes.coordinator import _extract_mutation_pairs
        msgs = [self._ai(
            ("kubectl get pods -n x", ""),
            ("kubectl apply -f -", "kind: Deployment"),
        )]
        pairs = _extract_mutation_pairs(msgs)
        assert len(pairs) == 1
        assert "apply" in pairs[0]["command"]

    def test_redacts_secrets_in_yaml(self):
        from app.agent.nodes.coordinator import _extract_mutation_pairs
        yaml_with_secret = "apiVersion: v1\nkind: Secret\npassword: leaked123"
        msgs = [self._ai(("kubectl apply -f -", yaml_with_secret))]
        pairs = _extract_mutation_pairs(msgs)
        assert "leaked123" not in pairs[0]["stdin_yaml"]


# ── R5: confidence resolution ────────────────────────────────────────────────


class TestConfidenceResolution:
    def test_verified_with_playbook_promotes(self):
        from app.agent.nodes.coordinator import _resolve_confidence
        c = _resolve_confidence({}, has_playbook=True, verified=True)
        assert c == 0.9

    def test_verified_no_playbook_below_promotion(self):
        from app.agent.nodes.coordinator import _resolve_confidence
        c = _resolve_confidence({}, has_playbook=False, verified=True)
        assert c == 0.7

    def test_unverified_with_playbook_below_promotion(self):
        from app.agent.nodes.coordinator import _resolve_confidence
        c = _resolve_confidence({}, has_playbook=True, verified=False)
        assert c == 0.7

    def test_unverified_no_playbook_lowest(self):
        from app.agent.nodes.coordinator import _resolve_confidence
        c = _resolve_confidence({}, has_playbook=False, verified=False)
        assert c == 0.5

    def test_verification_unknown_treated_as_not_verified(self):
        from app.agent.nodes.coordinator import _resolve_confidence
        c = _resolve_confidence({}, has_playbook=True, verified=None)
        assert c == 0.7   # not eligible for promotion


# ── R1: cluster_id helper ────────────────────────────────────────────────────


class TestClusterId:
    def test_cluster_id_returns_string(self):
        # Even when kubectl is missing, function returns a string fallback.
        from app.cluster_id import get_cluster_id, reset_cluster_id_cache
        reset_cluster_id_cache()
        cid = get_cluster_id()
        assert isinstance(cid, str)
        assert len(cid) > 0

    def test_cluster_id_is_cached(self):
        from app.cluster_id import get_cluster_id, reset_cluster_id_cache
        reset_cluster_id_cache()
        a = get_cluster_id()
        b = get_cluster_id()
        assert a == b   # cache hit; should be identical


# ── Namespace inference ───────────────────────────────────────────────────────


class TestNamespaceInference:
    def test_picks_majority_namespace(self):
        from app.agent.nodes.coordinator import _infer_namespace
        cmds = [
            "kubectl apply -f - -n scenario-test",
            "kubectl get pods -n scenario-test",
            "kubectl describe pod foo -n other",
        ]
        assert _infer_namespace(cmds) == "scenario-test"

    def test_returns_none_when_no_namespace(self):
        from app.agent.nodes.coordinator import _infer_namespace
        assert _infer_namespace(["kubectl get nodes"]) is None

    def test_handles_long_form_namespace_flag(self):
        from app.agent.nodes.coordinator import _infer_namespace
        assert _infer_namespace(["kubectl apply -f - --namespace=foo"]) == "foo"

    def test_extracts_namespace_from_stdin_yaml(self):
        # Real bug: `kubectl apply -f -` has no -n flag, so the namespace must
        # come from the manifest metadata. Without this we lose the column.
        from app.agent.nodes.coordinator import _infer_namespace
        pairs = [{
            "command": "kubectl apply -f -",
            "stdin_yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: foo\n  namespace: scenario-test\n",
        }]
        assert _infer_namespace(["kubectl apply -f -"], pairs) == "scenario-test"

    def test_yaml_namespace_combines_with_command_args(self):
        from app.agent.nodes.coordinator import _infer_namespace
        commands = ["kubectl apply -f -"]
        pairs = [
            {"command": "kubectl apply -f -", "stdin_yaml": "metadata:\n  namespace: alpha\n"},
            {"command": "kubectl apply -f -", "stdin_yaml": "metadata:\n  namespace: beta\n"},
            {"command": "kubectl apply -f -", "stdin_yaml": "metadata:\n  namespace: alpha\n"},
        ]
        # alpha appears twice, beta once → alpha wins
        assert _infer_namespace(commands, pairs) == "alpha"
