"""A default install must reach a model endpoint without an Azure subscription.

The shipped default used to be `LLM_PROVIDER=azure`, which is the one provider a new user
cannot satisfy with a single credential: it needs a deployed Azure OpenAI resource, its
endpoint URL and two deployment names before the first call succeeds. `openai` needs one key.

This is also a deployment invariant now, not only an onboarding one. The public API moved off
Azure in 2026-09, so a default that silently points back at Azure OpenAI would resurrect a
dependency the project deliberately removed — and it would do it quietly, because a missing
Azure credential is a *warning* at startup, not an error. The server comes up, serves traffic,
and fails at the first LLM call.

Both halves are asserted because they are set in different files and drifted independently
before: the Python default in `app/core/config.py`, and the chart default in
`deploy/helm/kubeintellect/values.yaml` which overrides it via the ConfigMap.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.config import Settings

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "kubeintellect"


def _values(name: str) -> dict:
    return yaml.safe_load((CHART / name).read_text(encoding="utf-8"))


class TestShippedPythonDefault:
    def test_field_default_is_openai(self):
        # Asserted on the field, not an instance: conftest sets LLM_PROVIDER in os.environ
        # for the whole suite, so an instance would report the test harness's choice rather
        # than what ships.
        assert Settings.model_fields["LLM_PROVIDER"].default == "openai"

    def test_unconfigured_settings_resolve_to_openai(self, monkeypatch):
        for var in ("LLM_PROVIDER", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"):
            monkeypatch.delenv(var, raising=False)
        s = Settings(_env_file=None)
        assert s.LLM_PROVIDER == "openai"

    def test_azure_is_still_a_supported_choice(self):
        # Moving the default off Azure must not remove the provider — existing deployments
        # set it explicitly and have to keep working.
        s = Settings(
            _env_file=None,
            LLM_PROVIDER="azure",
            AZURE_OPENAI_API_KEY="k",
            AZURE_OPENAI_ENDPOINT="https://x.openai.azure.com/",
        )
        assert s.LLM_PROVIDER == "azure"


class TestChartDefault:
    def test_chart_llm_provider_is_openai(self):
        assert _values("values.yaml")["config"]["llmProvider"] == "openai"

    @pytest.mark.parametrize("key", ["requireAuth", "allowedOrigins"])
    def test_auth_gate_is_reachable_through_helm(self, key):
        # These had no named value at all, so the only way to enable auth on a Helm-deployed
        # release was config.extraEnv — an escape hatch documented for experiment flags. An
        # operator reading values.yaml to find the auth switch would have concluded there
        # wasn't one, which is how the public API ran open to the internet.
        assert key in _values("values.yaml")["config"]


class TestPublicProductionProfile:
    """The profile behind api.kubeintellect.com. Its ingress is the open internet."""

    def test_profile_exists(self):
        assert (CHART / "values-hetzner.yaml.example").is_file()

    def test_requires_auth(self):
        # With requireAuth false and no keys set, the server resolves every anonymous caller
        # to `admin`. Verified live on 2026-09-03: an unauthenticated GET /v1/namespaces
        # returned the real namespace list with HTTP 200.
        assert _values("values-hetzner.yaml.example")["config"]["requireAuth"] is True

    def test_does_not_use_azure(self):
        assert _values("values-hetzner.yaml.example")["config"]["llmProvider"] == "openai"

    def test_langfuse_stays_off(self):
        # Six pods and ~36 Gi of PVCs with no resource limits on any template. Enabling it
        # is what takes the box over its 16 GB envelope.
        assert _values("values-hetzner.yaml.example")["langfuse"]["enabled"] is False

    def test_ships_no_credentials(self):
        v = _values("values-hetzner.yaml.example")
        for tier in ("adminApiKeys", "operatorApiKeys", "readonlyApiKeys", "openaiApiKey"):
            assert v["secrets"][tier] == "", f"{tier} must be blank in a committed example"


class TestRequireAuthWithoutKeysIsRefusedAtStartup:
    """The chart deliberately does NOT reject `requireAuth: true` with blank secrets.

    It cannot: the keys legitimately arrive via `--set-string`, and every values file in the
    chart directory has to render standalone (test_chart_shutdown_contract.py). So the pairing
    is enforced one layer down, in the process itself — and that is the layer this asserts,
    because the ConfigMap comment now points here as the reason the template guard is absent.
    """

    # The distinctive half of the refusal message in app/main.py. Keyed on rather than the
    # exit code alone, because later startup stages exit(1) too — a test that only saw
    # SystemExit would pass just as happily if the auth check had been deleted outright.
    REFUSAL = "REQUIRE_AUTH=true but no API keys are configured"

    @staticmethod
    async def _boot(monkeypatch, *, auth_enabled: bool) -> tuple[int | None, str]:
        import logging as _logging

        from app.core.config import settings as live
        from app.main import lifespan

        monkeypatch.setattr(live, "REQUIRE_AUTH", True)
        monkeypatch.setattr(type(live), "auth_enabled", property(lambda self: auth_enabled))

        records: list[str] = []
        handler = _logging.Handler()
        handler.emit = lambda r: records.append(r.getMessage())  # type: ignore[method-assign]
        from app.utils.logger import logger as app_logger

        app_logger.addHandler(handler)
        code: int | None = None
        try:
            async with lifespan(None):
                pass
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception:
            pass  # a later startup stage failed; irrelevant to the auth gate
        finally:
            app_logger.removeHandler(handler)
        return code, "\n".join(records)

    @pytest.mark.asyncio
    async def test_no_keys_is_refused_before_the_port_opens(self, monkeypatch):
        code, log = await self._boot(monkeypatch, auth_enabled=False)
        assert code == 1
        assert self.REFUSAL in log

    @pytest.mark.asyncio
    async def test_a_configured_key_passes_the_auth_gate(self, monkeypatch):
        """Vacuity guard: proves the refusal above is caused by the missing keys."""
        _code, log = await self._boot(monkeypatch, auth_enabled=True)
        assert self.REFUSAL not in log
