"""`kubeintellect status` must not report a cluster it never contacted.

Every other row on that board is measured: the database gets a real `psycopg.connect`, Prometheus,
Loki, Grafana and Langfuse each get an HTTP probe. The `Kube:` row — the one thing this product
exists to operate — was green on `Path.exists()` alone, decorated with a context name read out of
that same local file. An expired credential, a stopped kind cluster or a VPN that is down all
printed ✓, and an operator checking "can it see my cluster?" before an incident was told yes.

Three states now, because two cannot express it: reachable, did-not-answer, and *not verified*
when there is no kubectl to ask with. "Not verified" is deliberately not ✓.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess

import pytest

from app import cli

_BOGUS_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- cluster: {server: https://127.0.0.1:59999}
  name: nowhere
contexts:
- context: {cluster: nowhere, user: nobody}
  name: nowhere
current-context: nowhere
users:
- name: nobody
  user: {}
"""


class TestTheProbeAnswersHonestly:
    @pytest.mark.skipif(shutil.which("kubectl") is None, reason="needs a real kubectl to probe with")
    def test_an_api_server_that_is_not_there_is_not_reachable(self, tmp_path):
        cfg = tmp_path / "kubeconfig"
        cfg.write_text(_BOGUS_KUBECONFIG, encoding="utf-8")
        assert cli._cluster_reachable(str(cfg)) is False, (
            "a kubeconfig pointing at a closed port reported the cluster as reachable"
        )

    def test_a_server_that_answers_is_reachable(self, monkeypatch):
        monkeypatch.setattr(
            cli.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="{}", stderr=""),
        )
        assert cli._cluster_reachable("/any/path") is True

    def test_a_hang_is_unreachable_never_healthy(self, monkeypatch):
        """`--request-timeout` bounds the API request, not the connection attempts before it."""
        def _hang(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="kubectl", timeout=8)

        monkeypatch.setattr(cli.subprocess, "run", _hang)
        assert cli._cluster_reachable("/any/path") is False

    def test_no_kubectl_is_not_verified_rather_than_a_verdict(self, monkeypatch):
        def _missing(*args, **kwargs):
            raise FileNotFoundError("kubectl")

        monkeypatch.setattr(cli.subprocess, "run", _missing)
        assert cli._cluster_reachable("/any/path") is None, (
            "with no kubectl the probe must say 'unknown', not invent a verdict"
        )


class TestTheRowSaysWhichOfTheThreeItIs:
    @pytest.fixture(autouse=True)
    def _quiet_board(self, monkeypatch, tmp_path):
        """Neutralize every other probe so the test is about the Kube row and nothing else.

        `cmd_status` loads `./.env` before it prints anything, so without this the row under test
        depends on whichever datasource URLs the developer happens to have configured — and the
        board then spends three seconds per unreachable probe. The first draft did exactly that
        and took 15 seconds a call.
        """
        monkeypatch.setattr(cli, "_load_dotenv", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_CONFIG_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(cli, "_get_kube_context", lambda p: "kind-kubeintellect")
        for var in ("PROMETHEUS_URL", "LOKI_URL", "GRAFANA_URL", "LANGFUSE_ENABLED"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("USE_SQLITE", "true")
        cfg = tmp_path / "kubeconfig"
        cfg.write_text(_BOGUS_KUBECONFIG, encoding="utf-8")
        monkeypatch.setenv("KUBECONFIG_PATH", str(cfg))

    def _row(self, capsys) -> str:
        # A deliberately broken board now exits 1 — see test_status_exit_code.py. These tests
        # are about what the row *says*.
        with contextlib.suppress(SystemExit):
            cli.cmd_status(None)
        line = next(ln for ln in capsys.readouterr().out.splitlines() if "Kube:" in ln)
        return line

    def test_reachable_reads_as_reachable(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_cluster_reachable", lambda p: True)
        assert "API server reachable" in self._row(capsys)

    def test_unreachable_is_not_dressed_up_as_healthy(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_cluster_reachable", lambda p: False)
        row = self._row(capsys)
        assert "did not answer" in row
        # Not a bare "reachable" check: pytest's tmp_path is named after the test, so the path in
        # the row contains the word — a test whose input is derived from the test itself.
        assert "API server reachable" not in row

    def test_unknown_is_neither(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_cluster_reachable", lambda p: None)
        row = self._row(capsys)
        assert "not verified" in row
        assert "did not answer" not in row


class TestTheLLMRowDoesNotClaimMoreThanItChecked:
    """The board's ✓ next to `LLM:` meant "these environment variables are non-empty".

    A revoked key, an endpoint with a typo, and a deployment name that does not exist are all
    indistinguishable from a working configuration at this point — and they surface on the first
    incident, which is the worst moment to learn them. The row keeps its ✓ (the configuration
    really is present, which is what was measured) and now says exactly that much.

    It deliberately makes no request: the cheapest useful probe is a round trip to a paid
    endpoint, and `status` is run casually and often. Turning that into a real credential check
    is a product decision, not a repair.
    """

    @pytest.fixture(autouse=True)
    def _quiet_board(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "_load_dotenv", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_CONFIG_FILE", tmp_path / "absent.env")
        monkeypatch.setattr(cli, "_cluster_reachable", lambda p: None)
        for var in ("PROMETHEUS_URL", "LOKI_URL", "GRAFANA_URL", "LANGFUSE_ENABLED",
                    "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("USE_SQLITE", "true")

    def _row(self, capsys) -> str:
        with contextlib.suppress(SystemExit):   # a red board exits 1; the row text is the subject
            cli.cmd_status(None)
        return next(ln for ln in capsys.readouterr().out.splitlines() if "LLM:" in ln)

    def test_openai_credentials_present_are_marked_unverified(self, monkeypatch, capsys):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key")
        row = self._row(capsys)
        assert "not verified" in row, f"a bare tick still reads as 'the model works': {row!r}"

    def test_azure_credentials_present_are_marked_unverified(self, monkeypatch, capsys):
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "not-a-real-key")
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid/")
        row = self._row(capsys)
        assert "not verified" in row, f"a bare tick still reads as 'the model works': {row!r}"

    @pytest.mark.parametrize("provider", ["openai", "azure"])
    def test_missing_credentials_still_say_what_is_missing(self, monkeypatch, capsys, provider):
        """The unverified note must not displace the actionable one."""
        monkeypatch.setenv("LLM_PROVIDER", provider)
        row = self._row(capsys)
        assert "missing" in row.lower()
        assert "not verified" not in row, (
            "an unconfigured provider was told its credentials are merely unverified"
        )
