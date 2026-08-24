"""The kubeconfig fallback parser has to read the file kubectl actually writes.

`list_contexts()` tries `kubectl config get-contexts` first and falls back to a minimal scan of
`~/.kube/config`. The fallback matched only `- name:` — the ordering where the YAML sequence dash
lands on the name — but kubectl writes the other one:

    contexts:
    - context:
        cluster: kind-kubeintellect
      name: kind-kubeintellect

Measured 2026-08-24 against a kubectl-written file holding two contexts: `kubectl config
get-contexts -o name` printed both, the fallback returned `[]`. And the fallback is the branch
that runs when kubectl is *not installed* — the one case where it is the only source there is.
The REPL then offers no `/context` completions and answers `/context` with "No kubectl contexts
found (is kubectl installed and is ~/.kube/config valid?)", a question whose two hypotheses the
user can check and find innocent.
"""

from __future__ import annotations

import textwrap

import pytest

from kube_q.core import kubeconfig

_KUBECTL_LAYOUT = """
    apiVersion: v1
    kind: Config
    clusters:
    - cluster:
        server: https://127.0.0.1:6443
      name: kind-kubeintellect
    contexts:
    - context:
        cluster: kind-kubeintellect
        user: kind-kubeintellect
      name: kind-kubeintellect
    - context:
        cluster: prod-eks
        user: prod-eks
        namespace: payments
      name: prod-eks
    current-context: kind-kubeintellect
    users:
    - name: a-user-who-must-not-appear
"""

_NAME_FIRST = """
    contexts:
    - name: staging
      context:
        cluster: c1
    - name: prod
      context:
        cluster: c2
"""

_NAME_FIRST_INDENTED = """
    contexts:
      - name: staging
        context:
          cluster: c1
      - name: prod
        context:
          cluster: c2
    current-context: staging
"""


@pytest.fixture
def kubeconfig_file(tmp_path, monkeypatch):
    def write(body: str) -> None:
        path = tmp_path / "config"
        path.write_text(textwrap.dedent(body).lstrip("\n"))
        monkeypatch.setenv("KUBECONFIG", str(path))
    return write


class TestTheFallbackParser:
    def test_it_reads_the_layout_kubectl_writes(self, kubeconfig_file):
        kubeconfig_file(_KUBECTL_LAYOUT)
        assert kubeconfig._from_kubeconfig_file() == ["kind-kubeintellect", "prod-eks"]

    def test_it_still_reads_the_name_first_layout(self, kubeconfig_file):
        """The ordering the old parser handled must keep working."""
        kubeconfig_file(_NAME_FIRST)
        assert kubeconfig._from_kubeconfig_file() == ["staging", "prod"]

    def test_it_reads_entries_indented_under_the_key(self, kubeconfig_file):
        kubeconfig_file(_NAME_FIRST_INDENTED)
        assert kubeconfig._from_kubeconfig_file() == ["staging", "prod"]

    def test_it_stops_at_the_next_top_level_key(self, kubeconfig_file):
        """`users:` entries carry a `name:` too — leaking one would offer a login as a context."""
        kubeconfig_file(_KUBECTL_LAYOUT)
        assert "a-user-who-must-not-appear" not in kubeconfig._from_kubeconfig_file()

    def test_a_cluster_name_before_the_block_is_not_a_context(self, kubeconfig_file):
        kubeconfig_file(_KUBECTL_LAYOUT)
        names = kubeconfig._from_kubeconfig_file()
        assert names.count("kind-kubeintellect") == 1, (
            f"the identically-named cluster entry leaked in: {names}"
        )

    def test_an_extension_name_inside_a_context_is_not_a_context(self, kubeconfig_file):
        """kubectl writes `extensions:` entries inside a context, each with its own `name:`
        (`context_info`). Matching `name:` at any depth would offer that as a context."""
        kubeconfig_file("""
            contexts:
            - context:
                cluster: c1
                extensions:
                - extension:
                    last-update: Mon, 01 Jan 2026 00:00:00 UTC
                  name: context_info
              name: prod
        """)
        assert kubeconfig._from_kubeconfig_file() == ["prod"]

    def test_quotes_and_comments_are_handled(self, kubeconfig_file):
        kubeconfig_file("""
            contexts:
            # the cluster we use on call
            - context:
                cluster: c1
              name: "prod-eu"
            - context:
                cluster: c2
              name: 'prod-us'
        """)
        assert kubeconfig._from_kubeconfig_file() == ["prod-eu", "prod-us"]

    def test_a_file_with_no_contexts_block_is_empty(self, kubeconfig_file):
        """Vacuity guard: the parser must still be able to answer "none"."""
        kubeconfig_file("""
            apiVersion: v1
            kind: Config
            users:
            - name: someone
        """)
        assert kubeconfig._from_kubeconfig_file() == []

    def test_a_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KUBECONFIG", str(tmp_path / "nope"))
        assert kubeconfig._from_kubeconfig_file() == []

    def test_the_first_path_in_kubeconfig_wins(self, tmp_path, monkeypatch):
        """KUBECONFIG may hold several paths; the parser documents that it reads the first."""
        first = tmp_path / "first"
        first.write_text(textwrap.dedent(_NAME_FIRST).lstrip("\n"))
        monkeypatch.setenv("KUBECONFIG", f"{first}:{tmp_path / 'second'}")
        assert kubeconfig._from_kubeconfig_file() == ["staging", "prod"]


class TestListContexts:
    def test_it_falls_back_to_the_file_when_kubectl_gives_nothing(
        self, kubeconfig_file, monkeypatch
    ):
        kubeconfig_file(_KUBECTL_LAYOUT)
        monkeypatch.setattr(kubeconfig, "_from_kubectl", lambda: [])
        assert kubeconfig.list_contexts() == ["kind-kubeintellect", "prod-eks"]

    def test_kubectl_wins_when_it_answers(self, kubeconfig_file, monkeypatch):
        """Vacuity guard for the test above: the fallback must be a fallback, not the only path."""
        kubeconfig_file(_KUBECTL_LAYOUT)
        monkeypatch.setattr(kubeconfig, "_from_kubectl", lambda: ["from-kubectl"])
        assert kubeconfig.list_contexts() == ["from-kubectl"]

    def test_a_missing_kubectl_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(kubeconfig.shutil, "which", lambda _name: None)
        assert kubeconfig._from_kubectl() == []
