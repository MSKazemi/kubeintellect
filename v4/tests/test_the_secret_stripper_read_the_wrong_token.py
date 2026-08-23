"""`helm get manifest` strips the release's Secrets — unless you write `-n` before the verb.

`run_helm`'s docstring, step 4: *"Secret stripping — `helm get manifest|all` renders the chart's
own Secret objects with base64 `data:` intact"*, and `run_kubectl` blocks Secrets for **every**
role and regardless of namespace, so this exists to keep that a statement about the product
rather than about one tool.

It decided when to strip from ``next((t.lower() for t in tokens[2:] if not t.startswith("-")), "")``
— the first non-flag token from index 2. Any global flag before the verb puts its *value* there.
Measured 2026-08-20 with a stubbed helm and an admin key, the base64 password came back in full::

    helm get manifest shop                 stripped   ✅
    helm get manifest shop -n prod         stripped   ✅
    helm -n prod get manifest shop         LEAKED     ⚠️  subcommand read as "prod"
    helm --namespace prod get manifest shop LEAKED    ⚠️
    helm -n prod get all shop              LEAKED     ⚠️
    helm get hooks shop                    LEAKED     ⚠️  renders manifests, never on the list

Third file with this defect (kubectl `_extract_verb` pass 51, `_requires_always_confirm` pass 98,
`_targeted_namespaces` pass 99) — and here the guard's own sibling `_extract_verb` in this very
module already skips flags correctly, with a docstring explaining why. The fix removes the
decision rather than repairing the parse: **every** `helm get` is stripped. The stripper is a
no-op on output with no protected `kind:`, so there is nothing to win by guessing which
subcommand renders one.

The second cause is pass 100's lens — a filter with an opinion about punctuation the caller
chose. The `kind:` line was matched as a bare token to end-of-line::

    ^kind:\\s*([A-Za-z0-9.-]+)\\s*$

so `kind: "Secret"` (legal YAML) and `kind: Secret  # managed by the platform team` (an ordinary
chart comment) both failed to match, and the document was kept with its `data:` block.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.tools import helm_tool as ht
from app.tools.helm_tool import run_helm

_ADMIN = {"configurable": {"user_role": "admin", "thread_id": "t"}}
_B64 = "aHVudGVyMi1wcm9k"

_MANIFEST = f"""---
# Source: shop/templates/cm.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: shop-config
data:
  log_level: debug
---
# Source: shop/templates/secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: shop-db
data:
  password: {_B64}
"""


def _run(command: str, output: str = _MANIFEST, role: str = "admin") -> str:
    with patch("subprocess.run") as run:
        proc = MagicMock()
        proc.stdout, proc.stderr, proc.returncode = output, "", 0
        run.return_value = proc
        return str(run_helm.invoke({"command": command},
                                   config={"configurable": {"user_role": role, "thread_id": "t"}}))


class TestTheFlagOrderDoesNotDecideWhetherSecretsAreStripped:

    @pytest.mark.parametrize("command", [
        "helm get manifest shop",
        "helm get manifest shop -n prod",
        "helm -n prod get manifest shop",
        "helm --namespace prod get manifest shop",
        "helm --namespace=prod get manifest shop",
        "helm -nprod get manifest shop",
        "helm -o yaml get manifest shop",
        "helm get all shop",
        "helm -n prod get all shop",
        "helm get hooks shop",
        "helm -n prod get hooks shop",
    ])
    def test_the_base64_data_never_comes_back(self, command):
        out = _run(command)
        assert _B64 not in out, f"{command!r} leaked the Secret"
        assert "protected kind" in out, f"{command!r} did not announce the removal"

    @pytest.mark.parametrize("command", [
        "helm get manifest shop",
        "helm -n prod get manifest shop",
        "helm get hooks shop",
    ])
    def test_the_rest_of_the_manifest_survives(self, command):
        """Document-level removal, not a blanket refusal — the tool has to stay useful."""
        out = _run(command)
        assert "shop-config" in out and "log_level: debug" in out
        assert "kind: ConfigMap" in out


class TestThePunctuationTheChartChose:

    @pytest.mark.parametrize("kind_line", [
        "kind: Secret",
        'kind: "Secret"',
        "kind: 'Secret'",
        "kind: Secret  # managed by the platform team",
        'kind: "Secret"  # managed by the platform team',
        "kind: Secret   ",
        "kind:  Secret",
        "KIND: Secret",
        "kind: secret",
        "kind: ServiceAccount",
    ])
    def test_a_protected_kind_is_recognised_however_it_is_written(self, kind_line):
        out = _run("helm get manifest shop", _MANIFEST.replace("kind: Secret", kind_line))
        assert _B64 not in out, f"{kind_line!r} kept the document"

    @pytest.mark.parametrize("kind_line", [
        "kind: ConfigMap",
        'kind: "ConfigMap"',
        "kind: Deployment  # the app",
    ])
    def test_an_ordinary_kind_is_still_kept(self, kind_line):
        doc = f"---\napiVersion: v1\n{kind_line}\nmetadata:\n  name: keep-me\n"
        out = _run("helm get manifest shop", doc)
        assert "keep-me" in out and "protected kind" not in out

    def test_a_kind_field_nested_under_another_key_is_not_a_document_kind(self):
        """Only a column-zero `kind:` names the object; an indented one is someone's data."""
        doc = ("---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: keep-me\n"
               "data:\n  note: |\n    kind: Secret\n")
        out = _run("helm get manifest shop", doc)
        assert "keep-me" in out and "protected kind" not in out


class TestOneFlagWalkForTheWholeModule:

    @pytest.mark.parametrize(("command", "verb"), [
        ("helm get manifest shop", "get"),
        ("helm -n prod get manifest shop", "get"),
        ("helm --namespace prod list", "list"),
        ("helm list", "list"),
    ])
    def test_the_verb_parse_is_unchanged(self, command, verb):
        assert ht._extract_verb(command.split()) == verb

    @pytest.mark.parametrize(("command", "index"), [
        ("helm get manifest shop", 1),
        ("helm -n prod get manifest shop", 3),
        ("helm --namespace=prod get manifest", 2),
        ("helm -o yaml list", 3),
    ])
    def test_skip_flags_finds_the_verb_position(self, command, index):
        assert ht._skip_flags(command.split(), 1) == index

    def test_a_command_that_is_only_flags_has_no_verb(self):
        assert ht._extract_verb(["helm", "-n", "prod"]) == ""


class TestTheOtherHelmPathsAreUnaffected:

    def test_a_non_get_verb_is_not_stripped(self):
        """`helm list` output has no manifest in it; the stripper must not reach it."""
        listing = "NAME  NAMESPACE  REVISION  STATUS\nshop  prod       1         deployed\n"
        out = _run("helm list", listing)
        assert "shop" in out and "protected kind" not in out

    def test_a_protected_namespace_is_still_refused_before_any_of_this(self):
        out = _run("helm -n kube-system get manifest shop")
        assert "[Protected]" in out and "infrastructure namespace" in out

    def test_a_write_verb_is_still_refused(self):
        out = _run("helm -n prod uninstall shop")
        assert _B64 not in out and "get manifest" not in out.lower().split("\n")[0]
