"""The Helm chart exposes the server settings that used to exist only in config.py.

Two things can drift silently here. A chart default can stop equalling the server default (an
operator who changes nothing then gets different behaviour from a source install), and a
combination the server refuses at startup can render fine and crash-loop in the cluster. The
first is checked statically against ``Settings``; the second needs a real ``helm template``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "kubeintellect"
_VALUES = yaml.safe_load((_CHART / "values.yaml").read_text(encoding="utf-8"))

# chart value (under `config`) -> server setting
_PARITY = {
    "selfGovernEnabled": "SELF_GOVERN_ENABLED",
    "promqlDetectionEnabled": "PROMQL_DETECTION_ENABLED",
    "promqlDetectionIntervalSeconds": "PROMQL_DETECTION_INTERVAL_SECONDS",
    "postmortemMinGrounding": "POSTMORTEM_MIN_GROUNDING",
    "localLlmProbeTimeoutSeconds": "LOCAL_LLM_PROBE_TIMEOUT_SECONDS",
}


@pytest.mark.parametrize("value,setting", sorted(_PARITY.items()))
def test_chart_default_equals_server_default(value, setting):
    from app.core.config import Settings

    server = Settings.model_fields[setting].default
    assert _VALUES["config"][value] == server, (
        f"config.{value} defaults to {_VALUES['config'][value]!r} but {setting} defaults to "
        f"{server!r}: a chart install would behave differently from a source install."
    )


def test_nothing_new_is_enabled_by_default():
    cfg = _VALUES["config"]
    assert cfg["selfGovernEnabled"] is False
    assert cfg["promqlDetectionEnabled"] is False


def test_every_new_setting_is_wired_into_the_configmap():
    text = (_CHART / "templates" / "configmap.yaml").read_text(encoding="utf-8")
    for setting in _PARITY.values():
        assert f"{setting}:" in text, f"{setting} is in values.yaml but never reaches the pod"


def test_the_startup_probe_is_gated_on_the_local_provider():
    text = (_CHART / "templates" / "deployment.yaml").read_text(encoding="utf-8")
    block = text.split("startupProbe:", 1)[0].rsplit("{{-", 1)[-1]
    assert 'eq (.Values.config.llmProvider | default "openai") "local"' in block, (
        "startupProbe must only render for llmProvider=local; other providers keep their behaviour"
    )


@pytest.mark.skipif(shutil.which("helm") is None, reason="needs a real helm to render with")
class TestRenderedChart:
    @staticmethod
    def _render(*overrides: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["helm", "template", "parity-test", str(_CHART), *overrides],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )

    _LOCAL = ("--set", "config.llmProvider=local",
              "--set", "secrets.openaiBaseUrl=http://ollama.ollama.svc:11434/v1")

    def test_default_render_has_no_startup_probe(self):
        proc = self._render()
        assert proc.returncode == 0, proc.stderr
        assert "startupProbe" not in proc.stdout

    def test_local_provider_gets_a_startup_probe_covering_two_models_plus_margin(self):
        proc = self._render(*self._LOCAL)
        assert proc.returncode == 0, proc.stderr
        assert "startupProbe:" in proc.stdout
        # 180s x 2 models + 120s margin = 480s; period 10s -> 48 failures
        assert "failureThreshold: 48" in proc.stdout.split("startupProbe:", 1)[1].split("livenessProbe:", 1)[0]

    def test_threshold_follows_the_probe_timeout(self):
        proc = self._render(*self._LOCAL, "--set", "config.localLlmProbeTimeoutSeconds=300")
        assert proc.returncode == 0, proc.stderr
        # 300 x 2 + 120 = 720 -> 72
        assert "failureThreshold: 72" in proc.stdout

    def test_too_small_an_explicit_threshold_is_refused(self):
        proc = self._render(*self._LOCAL, "--set", "startupProbe.failureThreshold=10")
        assert proc.returncode != 0
        assert "at least 48" in proc.stderr

    def test_local_without_a_base_url_is_refused(self):
        proc = self._render("--set", "config.llmProvider=local")
        assert proc.returncode != 0
        assert "secrets.openaiBaseUrl" in proc.stderr

    def test_anthropic_without_cortex_is_refused(self):
        proc = self._render("--set", "config.llmProvider=anthropic")
        assert proc.returncode != 0
        assert "cortexV4Enabled" in proc.stderr

    def test_anthropic_with_cortex_renders(self):
        proc = self._render("--set", "config.llmProvider=anthropic", "--set", "config.cortexV4Enabled=true")
        assert proc.returncode == 0, proc.stderr

    def test_promql_detection_without_prometheus_is_refused(self):
        proc = self._render("--set", "config.promqlDetectionEnabled=true")
        assert proc.returncode != 0
        assert "prometheusUrl" in proc.stderr

    def test_a_grounding_floor_of_zero_is_not_replaced_by_the_default(self):
        proc = self._render("--set", "config.postmortemMinGrounding=0")
        assert proc.returncode == 0, proc.stderr
        assert 'POSTMORTEM_MIN_GROUNDING: "0"' in proc.stdout

    def test_an_out_of_range_grounding_floor_is_refused(self):
        assert self._render("--set", "config.postmortemMinGrounding=1.5").returncode != 0

    def test_defaults_render_the_server_defaults(self):
        out = self._render().stdout
        for line in ('SELF_GOVERN_ENABLED: "false"', 'PROMQL_DETECTION_ENABLED: "false"',
                     'PROMQL_DETECTION_INTERVAL_SECONDS: "30"', 'POSTMORTEM_MIN_GROUNDING: "0.9"',
                     'LOCAL_LLM_PROBE_TIMEOUT_SECONDS: "180"'):
            assert line in out, line
