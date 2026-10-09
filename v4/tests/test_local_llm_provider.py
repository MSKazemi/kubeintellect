"""`LLM_PROVIDER=local` — a self-hosted OpenAI-compatible server as a first-class provider (#17).

What is pinned, and what would have to break for each part to fail:

  * the provider is accepted, needs no API key, and its base URL is never empty -- an empty
    one is api.openai.com, the one place an operator choosing `local` must never reach;
  * both graphs build their client against that URL (V2 factory and Cortex tiers);
  * the startup check refuses -- with a message naming the fix -- when the endpoint is down,
    a model is not served, the server rejects tools, or the model answers in prose instead of
    calling the tool; and passes only on a real tool call;
  * the server exits before building the graph when the check fails;
  * `kubeintellect serve`/`status` no longer call `local` an invalid provider.

Every endpoint here is stubbed; nothing leaves the process.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage

from app.core import llm, local_llm
from app.core.config import LOCAL_LLM_DEFAULT_BASE_URL, Settings

_BASE = "http://ollama.test:11434/v1"


def _settings(**kw) -> Settings:
    # _env_file=None isolates the test from a developer's real v4/.env
    return Settings(_env_file=None, **kw)


@pytest.fixture(autouse=True)
def _fresh_factory_cache():
    llm._coordinator_llm.cache_clear()
    llm._subagent_llm.cache_clear()
    yield
    llm._coordinator_llm.cache_clear()
    llm._subagent_llm.cache_clear()


def _local(base_url: str | None = _BASE, key: str | None = None,
           coordinator: str = "qwen2.5:14b", subagent: str = "qwen2.5:7b"):
    s = llm.settings
    return (
        patch.object(s, "LLM_PROVIDER", "local"),
        patch.object(s, "OPENAI_BASE_URL", base_url),
        patch.object(s, "OPENAI_API_KEY", key),
        patch.object(s, "OPENAI_COORDINATOR_MODEL", coordinator),
        patch.object(s, "OPENAI_SUBAGENT_MODEL", subagent),
    )


class TestTheSetting:
    def test_local_is_a_valid_provider(self):
        assert _settings(LLM_PROVIDER="local").LLM_PROVIDER == "local"

    def test_the_base_url_defaults_to_ollama_never_to_openai(self, monkeypatch):
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        s = _settings(LLM_PROVIDER="local")
        assert s.OPENAI_BASE_URL == LOCAL_LLM_DEFAULT_BASE_URL == "http://localhost:11434/v1"

    def test_an_explicit_base_url_is_kept(self):
        assert _settings(LLM_PROVIDER="local", OPENAI_BASE_URL=_BASE).OPENAI_BASE_URL == _BASE

    def test_no_api_key_warning_for_local(self, caplog, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with caplog.at_level(logging.WARNING):
            _settings(LLM_PROVIDER="local", OPENAI_API_KEY=None)
        assert "OPENAI_API_KEY is not set" not in caplog.text

    def test_other_providers_keep_the_openai_default(self, monkeypatch):
        """Vacuity guard: the default is specific to `local`."""
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        assert _settings(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-x").OPENAI_BASE_URL is None


class TestTheClient:
    def test_no_key_is_needed(self, monkeypatch):
        from langchain_openai import ChatOpenAI

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        a, b, c, d, e = _local(key=None)
        with a, b, c, d, e:
            model = llm._make_openai("qwen2.5:14b")
        assert isinstance(model, ChatOpenAI)
        assert model.openai_api_base == _BASE
        assert model.openai_api_key is not None
        assert model.openai_api_key.get_secret_value() == llm.LOCAL_LLM_PLACEHOLDER_KEY

    def test_a_configured_key_is_used(self):
        a, b, c, d, e = _local(key="vllm-key")
        with a, b, c, d, e:
            model = llm._make_openai("qwen2.5:14b")
        assert model.openai_api_key.get_secret_value() == "vllm-key"  # type: ignore[union-attr]

    @pytest.mark.parametrize("empty", [None, ""])
    def test_an_empty_base_url_is_refused_not_sent_to_openai(self, empty):
        a, b, c, d, e = _local(base_url=empty)
        with a, b, c, d, e, pytest.raises(RuntimeError, match="OPENAI_BASE_URL is empty"):
            llm._make_openai("qwen2.5:14b")

    def test_the_v2_factory_targets_the_local_server(self):
        a, b, c, d, e = _local()
        with a, b, c, d, e:
            coord, sub = llm._coordinator_llm(), llm._subagent_llm()
        assert (coord.openai_api_base, coord.model_name) == (_BASE, "qwen2.5:14b")  # type: ignore[attr-defined]
        assert (sub.openai_api_base, sub.model_name) == (_BASE, "qwen2.5:7b")  # type: ignore[attr-defined]

    def test_the_cortex_tiers_target_the_local_server(self):
        from app.cortex import models as cortex_models

        a, b, c, d, e = _local()
        with a, b, c, d, e:
            small = cortex_models._tier(small=True, streaming=False)
            large = cortex_models._tier(small=False, streaming=True)
        assert (small.openai_api_base, small.model_name) == (_BASE, "qwen2.5:7b")  # type: ignore[attr-defined]
        assert (large.openai_api_base, large.model_name) == (_BASE, "qwen2.5:14b")  # type: ignore[attr-defined]


def _models_endpoint(handler):
    """Patch httpx.AsyncClient in local_llm so GET /models is answered by `handler`."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    return patch.object(local_llm.httpx, "AsyncClient", factory)


def _listing(*ids: str):
    return lambda request: httpx.Response(200, json={"data": [{"id": i} for i in ids]})


class _FakeBound:
    def __init__(self, result):
        self._result = result

    async def ainvoke(self, _prompt):
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class _FakeModel:
    def __init__(self, result):
        self._result = result

    def bind_tools(self, _tools):
        return _FakeBound(self._result)


def _replying(result):
    return patch.object(llm, "_make_openai", lambda *a, **k: _FakeModel(result))


_TOOL_CALL = AIMessage(
    content="",
    tool_calls=[{"name": "get_pod_status", "args": {"namespace": "prod", "pod": "api-0"}, "id": "c1"}],
)


class TestTheStartupCheck:
    @pytest.mark.asyncio
    async def test_a_real_tool_call_passes(self):
        a, b, c, d, e = _local()
        with a, b, c, d, e, _models_endpoint(_listing("qwen2.5:14b", "qwen2.5:7b")), \
             _replying(_TOOL_CALL):
            await local_llm.preflight_local_llm()

    @pytest.mark.asyncio
    async def test_an_unreachable_endpoint_is_refused(self):
        def down(request):
            raise httpx.ConnectError("connection refused", request=request)

        a, b, c, d, e = _local()
        with a, b, c, d, e, _models_endpoint(down), _replying(_TOOL_CALL), \
             pytest.raises(local_llm.LocalLLMUnavailable) as exc:
            await local_llm.preflight_local_llm()
        assert "cannot reach" in str(exc.value)
        assert _BASE in str(exc.value)
        assert "localhost" in str(exc.value), "say why localhost is wrong inside a container"

    @pytest.mark.asyncio
    async def test_a_model_the_server_does_not_serve_is_refused(self):
        a, b, c, d, e = _local()
        with a, b, c, d, e, _models_endpoint(_listing("llama3.1:8b")), _replying(_TOOL_CALL), \
             pytest.raises(local_llm.LocalLLMUnavailable) as exc:
            await local_llm.preflight_local_llm()
        msg = str(exc.value)
        assert "does not serve model 'qwen2.5:14b'" in msg
        assert "llama3.1:8b" in msg, "list what the server does serve"
        assert "ollama pull" in msg

    @pytest.mark.asyncio
    async def test_a_bare_ollama_name_matches_its_latest_tag(self):
        a, b, c, d, e = _local(coordinator="qwen2.5", subagent="qwen2.5")
        with a, b, c, d, e, _models_endpoint(_listing("qwen2.5:latest")), _replying(_TOOL_CALL):
            await local_llm.preflight_local_llm()

    @pytest.mark.asyncio
    async def test_a_model_that_answers_in_prose_is_refused(self):
        """The dangerous case: tools accepted, never called -- an agent that never looks."""
        prose = AIMessage(content="The pod api-0 is probably fine.")
        a, b, c, d, e = _local()
        with a, b, c, d, e, _models_endpoint(_listing("qwen2.5:14b", "qwen2.5:7b")), \
             _replying(prose), pytest.raises(local_llm.LocalLLMUnavailable) as exc:
            await local_llm.preflight_local_llm()
        assert "answered in text instead of calling the tool" in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_server_that_rejects_tools_is_refused(self):
        rejected = openai.BadRequestError(
            "registry.ollama.ai/library/gemma:2b does not support tools",
            response=httpx.Response(400, request=httpx.Request("POST", f"{_BASE}/chat/completions")),
            body=None,
        )
        a, b, c, d, e = _local()
        with a, b, c, d, e, _models_endpoint(_listing("qwen2.5:14b", "qwen2.5:7b")), \
             _replying(rejected), pytest.raises(local_llm.LocalLLMUnavailable) as exc:
            await local_llm.preflight_local_llm()
        msg = str(exc.value)
        assert "refused a tool-calling request" in msg
        assert "--enable-auto-tool-choice" in msg

    @pytest.mark.asyncio
    async def test_a_server_without_a_models_listing_still_gets_the_tool_probe(self):
        missing = lambda request: httpx.Response(404)  # noqa: E731
        prose = AIMessage(content="no tools here")
        a, b, c, d, e = _local()
        with a, b, c, d, e, _models_endpoint(missing), _replying(prose), \
             pytest.raises(local_llm.LocalLLMUnavailable, match="answered in text"):
            await local_llm.preflight_local_llm()


class TestTheServerRefusesToStart:
    REFUSAL = "Startup failed: LLM_PROVIDER=local"

    async def _boot(self, preflight: AsyncMock) -> tuple[int | None, str, AsyncMock]:
        from app.main import lifespan
        from app.utils.logger import logger as app_logger

        records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r.getMessage())  # type: ignore[method-assign]
        app_logger.addHandler(handler)
        init_graph = AsyncMock(side_effect=RuntimeError("stop after the provider check"))
        code: int | None = None
        a, b, c, d, e = _local()
        try:
            with a, b, c, d, e, patch.object(local_llm, "preflight_local_llm", preflight), \
                 patch("app.agent.workflow.init_graph", init_graph):
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
    async def test_a_failed_check_exits_before_the_graph_is_built(self):
        failing = AsyncMock(side_effect=local_llm.LocalLLMUnavailable(
            "LLM_PROVIDER=local: cannot reach the model server"))
        code, log, init_graph = await self._boot(failing)
        assert code == 1
        assert self.REFUSAL in log
        init_graph.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_passing_check_lets_startup_continue(self):
        """Vacuity guard: the exit above is caused by the failed check."""
        passing = AsyncMock(return_value=None)
        _code, log, init_graph = await self._boot(passing)
        passing.assert_awaited_once()
        assert self.REFUSAL not in log
        init_graph.assert_called_once()


def test_the_cli_does_not_call_local_an_invalid_provider():
    from app.cli import _validate_config

    issues = _validate_config({"LLM_PROVIDER": "local"})
    assert [i for i in issues if i.field == "LLM_PROVIDER"] == []
    # Vacuity guard: the same call does flag a provider that is not valid.
    bogus = _validate_config({"LLM_PROVIDER": "bogus"})
    assert "LLM_PROVIDER" in [i.field for i in bogus if i.level == "error"]
