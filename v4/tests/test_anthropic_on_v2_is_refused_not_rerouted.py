"""`LLM_PROVIDER=anthropic` on the default V2 graph is refused, never re-routed to OpenAI (#192).

The V2 model factory (`app.core.llm`) has an Azure backend and an OpenAI-compatible one. With
`LLM_PROVIDER=anthropic` and `CORTEX_V4_ENABLED=false` it used to fall through to the latter:
a `ChatOpenAI` client against `api.openai.com` with `OPENAI_API_KEY`, so every prompt -- pod
specs, events, log excerpts -- went to a vendor the operator had not selected, and the run
looked entirely normal. Only a warning was logged.

Pinned here, against the constructed client object (nothing is ever called):
  * the V2 factory raises instead of building an OpenAI client for `anthropic`;
  * the server refuses to start, before the graph is built and the port opens;
  * the Cortex path, which does implement Anthropic, is unchanged;
  * the providers V2 does serve still build (vacuity guard for the refusals above).
"""
from __future__ import annotations

import logging
import sys
import types
from unittest.mock import AsyncMock, patch

import pytest

from app.core import llm

_LEFTOVER = "sk-openai-LEFTOVER-KEY"


@pytest.fixture(autouse=True)
def _fresh_factory_cache():
    llm._coordinator_llm.cache_clear()
    llm._subagent_llm.cache_clear()
    yield
    llm._coordinator_llm.cache_clear()
    llm._subagent_llm.cache_clear()


def _patched(provider: str, cortex: bool):
    s = llm.settings
    return (
        patch.object(s, "LLM_PROVIDER", provider),
        patch.object(s, "CORTEX_V4_ENABLED", cortex),
        patch.object(s, "OPENAI_API_KEY", _LEFTOVER),
        patch.object(s, "OPENAI_BASE_URL", None),
        patch.object(s, "ANTHROPIC_API_KEY", "sk-ant-x"),
    )


class TestTheV2FactoryNeverSubstitutesOpenAI:
    @pytest.mark.parametrize("factory", ["_coordinator_llm", "_subagent_llm"])
    def test_anthropic_without_cortex_raises_instead_of_building_chatopenai(self, factory):
        a, b, c, d, e = _patched("anthropic", cortex=False)
        with a, b, c, d, e, pytest.raises(RuntimeError) as exc:
            getattr(llm, factory)()
        msg = str(exc.value)
        assert "CORTEX_V4_ENABLED=true" in msg, "the refusal must say how to fix it"
        assert "OpenAI" in msg, "and name the vendor the data would otherwise have reached"
        assert _LEFTOVER not in msg, "never echo a credential"

    @pytest.mark.parametrize("factory", ["_coordinator_llm", "_subagent_llm"])
    def test_anthropic_with_cortex_still_gets_no_openai_client_from_v2(self, factory):
        """Under Cortex the V2 factory is never the right source of an Anthropic model."""
        a, b, c, d, e = _patched("anthropic", cortex=True)
        with a, b, c, d, e, pytest.raises(RuntimeError, match="no Anthropic backend"):
            getattr(llm, factory)()

    @pytest.mark.parametrize("provider", ["openai", "qwen"])
    def test_the_openai_compatible_providers_still_build(self, provider):
        from langchain_openai import ChatOpenAI

        a, b, c, d, e = _patched(provider, cortex=False)
        with a, b, c, d, e:
            assert isinstance(llm._coordinator_llm(), ChatOpenAI)
            assert isinstance(llm._subagent_llm(), ChatOpenAI)


class TestTheServerRefusesToStart:
    REFUSAL = "LLM_PROVIDER=anthropic requires CORTEX_V4_ENABLED=true"

    async def _boot(self, provider: str, cortex: bool) -> tuple[int | None, str, AsyncMock]:
        from app.main import lifespan
        from app.utils.logger import logger as app_logger

        records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r.getMessage())  # type: ignore[method-assign]
        app_logger.addHandler(handler)
        init_graph = AsyncMock(side_effect=RuntimeError("stop after the provider check"))
        code: int | None = None
        a, b, c, d, e = _patched(provider, cortex)
        try:
            with a, b, c, d, e, patch("app.agent.workflow.init_graph", init_graph):
                async with lifespan(None):
                    pass
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception:
            pass  # a later startup stage failed; irrelevant to the provider gate
        finally:
            app_logger.removeHandler(handler)
        return code, "\n".join(records), init_graph

    @pytest.mark.asyncio
    async def test_anthropic_on_v2_exits_before_the_graph_is_built(self):
        code, log, init_graph = await self._boot("anthropic", cortex=False)
        assert code == 1
        assert self.REFUSAL in log
        init_graph.assert_not_called()

    @pytest.mark.asyncio
    async def test_anthropic_on_cortex_passes_the_provider_gate(self):
        """Vacuity guard: the refusal is caused by the V2 graph, not by `anthropic` itself."""
        _code, log, init_graph = await self._boot("anthropic", cortex=True)
        assert self.REFUSAL not in log
        init_graph.assert_called_once()


def test_the_cortex_path_still_builds_anthropic():
    """Acceptance criterion of #192: the Cortex path keeps working unchanged."""
    from app.cortex import models as cortex_models

    class FakeAnthropic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    s = cortex_models.settings
    with patch.object(s, "LLM_PROVIDER", "anthropic"), \
         patch.object(s, "CORTEX_V4_ENABLED", True), \
         patch.object(s, "ANTHROPIC_API_KEY", "sk-ant-x"), \
         patch.dict("sys.modules"):
        fake = types.ModuleType("langchain_anthropic")
        fake.ChatAnthropic = FakeAnthropic
        sys.modules["langchain_anthropic"] = fake
        model = cortex_models._tier(small=False, streaming=True)

    assert isinstance(model, FakeAnthropic)
    assert model.kwargs["model"] == s.ANTHROPIC_LARGE_MODEL
