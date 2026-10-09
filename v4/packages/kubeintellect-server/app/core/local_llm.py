"""Startup check for LLM_PROVIDER=local — a self-hosted OpenAI-compatible server (#17).

A hosted provider fails fast and legibly: a bad key is a 401 on the first request. A local
server fails in quieter ways, and two of them produce an agent that looks healthy:

  * the endpoint is not running (or `localhost` is the wrong host inside a container), so the
    first incident is when the operator learns there is no model at all;
  * the model cannot call tools. Ollama refuses a tools request for a model without a tools
    template, and vLLM refuses one unless it was started with --enable-auto-tool-choice -- but
    a model that *accepts* tools and simply answers in prose is worse: the ReAct agents then
    reply confidently without ever reading the cluster, the failure invariant #1 forbids.

So before the server opens its port, every configured model is asked to make one tool call,
through the same client factory both graphs use (`app.core.llm._make_openai`). Anything short
of a real tool call refuses startup with a message that says what to change. The probe prompt
carries no cluster data. Nothing here is skippable: a deployment that cannot pass it cannot
investigate anything.
"""
from __future__ import annotations

import asyncio

import httpx
from langchain_core.tools import tool

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_MODELS_TIMEOUT_SECONDS = 10.0
_ERROR_CHARS = 300


class LocalLLMUnavailable(RuntimeError):
    """The local endpoint cannot serve KubeIntellect. The message says what to change."""


@tool
def get_pod_status(namespace: str, pod: str) -> str:
    """Return the status of a pod. (stub: used only to probe tool calling at startup)"""
    return "Running"


_PROBE_PROMPT = (
    "Use the get_pod_status tool to check pod 'api-0' in namespace 'prod'. "
    "Call the tool; do not answer in text."
)


def _models() -> list[tuple[str, str, int]]:
    """(role, model name, max_tokens) for each distinct model the two graphs will use."""
    seen: dict[str, tuple[str, str, int]] = {}
    for role, name, max_tokens in (
        ("OPENAI_COORDINATOR_MODEL", settings.OPENAI_COORDINATOR_MODEL, 4096),
        ("OPENAI_SUBAGENT_MODEL", settings.OPENAI_SUBAGENT_MODEL, 2048),
    ):
        seen.setdefault(name, (role, name, max_tokens))
    return list(seen.values())


def _is_served(model: str, served: set[str]) -> bool:
    # Ollama lists `qwen2.5:latest` and accepts the bare `qwen2.5` for it.
    return model in served or (":" not in model and f"{model}:latest" in served)


def _unreachable(base_url: str, exc: Exception) -> LocalLLMUnavailable:
    return LocalLLMUnavailable(
        f"LLM_PROVIDER=local: cannot reach the model server at {base_url} "
        f"({type(exc).__name__}: {str(exc)[:_ERROR_CHARS]}).\n"
        "  Fix: start it (Ollama: `ollama serve`; vLLM: `vllm serve <model>`; LM Studio: "
        "start the local server), or point OPENAI_BASE_URL at it. Inside a container or a "
        "pod, `localhost` is the container itself -- use the host's address "
        "(e.g. http://host.docker.internal:11434/v1) or the server's Service DNS name."
    )


async def _served_models(base_url: str) -> set[str] | None:
    """Model ids the server lists, or None if it does not implement GET /models."""
    headers: dict[str, str] = {}
    if settings.OPENAI_API_KEY:
        headers["Authorization"] = f"Bearer {settings.OPENAI_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=_MODELS_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
    except httpx.HTTPError as exc:
        raise _unreachable(base_url, exc) from exc
    if resp.status_code in (401, 403):
        raise LocalLLMUnavailable(
            f"LLM_PROVIDER=local: {base_url} rejected the request with HTTP {resp.status_code}.\n"
            "  Fix: the server requires an API key -- set OPENAI_API_KEY to it "
            "(for vLLM, the value given to --api-key)."
        )
    if resp.status_code != 200:
        logger.warning(
            f"LLM_PROVIDER=local: GET {base_url}/models returned HTTP {resp.status_code}; "
            "cannot list served models, relying on the tool-calling probe alone."
        )
        return None
    try:
        return {str(m["id"]) for m in resp.json().get("data", []) if "id" in m}
    except (ValueError, AttributeError, TypeError, KeyError):
        logger.warning(f"LLM_PROVIDER=local: GET {base_url}/models returned no model list.")
        return None


async def _probe_tool_call(role: str, model: str, max_tokens: int, base_url: str) -> None:
    import openai

    from app.core.llm import _make_openai

    llm = _make_openai(model, max_tokens=max_tokens, streaming=False).bind_tools([get_pod_status])
    timeout = settings.LOCAL_LLM_PROBE_TIMEOUT_SECONDS
    try:
        out = await asyncio.wait_for(llm.ainvoke(_PROBE_PROMPT), timeout=timeout)
    except TimeoutError as exc:
        raise LocalLLMUnavailable(
            f"LLM_PROVIDER=local: model {model!r} ({role}) did not answer within {timeout:.0f}s "
            f"at {base_url}.\n"
            "  Fix: the first request loads the model, which can be slow on CPU -- raise "
            "LOCAL_LLM_PROBE_TIMEOUT_SECONDS, or choose a smaller model."
        ) from exc
    except openai.APIConnectionError as exc:
        raise _unreachable(base_url, exc) from exc
    except openai.NotFoundError as exc:
        raise LocalLLMUnavailable(
            f"LLM_PROVIDER=local: {base_url} does not serve model {model!r} ({role}): "
            f"{str(exc)[:_ERROR_CHARS]}\n"
            f"  Fix: set {role} to a model the server serves (Ollama: `ollama pull {model}`, "
            "or `ollama list` to see what is installed)."
        ) from exc
    except openai.APIStatusError as exc:
        if exc.status_code != 400 or "tool" not in str(exc).lower():
            raise LocalLLMUnavailable(
                f"LLM_PROVIDER=local: model {model!r} ({role}) at {base_url} failed with HTTP "
                f"{exc.status_code}: {str(exc)[:_ERROR_CHARS]}\n"
                "  Fix: check the model server's log -- a model too large for the available "
                "memory is a common cause."
            ) from exc
        raise LocalLLMUnavailable(
            f"LLM_PROVIDER=local: {base_url} refused a tool-calling request for model "
            f"{model!r} ({role}) with HTTP {exc.status_code}: {str(exc)[:_ERROR_CHARS]}\n"
            "  KubeIntellect's agents investigate only through tool calls, so this model "
            "cannot be used.\n"
            "  Fix: choose a model with tool support (Ollama: one tagged 'tools', e.g. "
            "qwen2.5, llama3.1, mistral-nemo); for vLLM, start it with "
            "--enable-auto-tool-choice --tool-call-parser <parser>."
        ) from exc
    if not getattr(out, "tool_calls", None):
        raise LocalLLMUnavailable(
            f"LLM_PROVIDER=local: model {model!r} ({role}) accepted a tool-calling request but "
            "answered in text instead of calling the tool.\n"
            "  Without tool calls the agent would answer without ever reading the cluster, so "
            "KubeIntellect will not start on this model.\n"
            "  Fix: choose a larger model with reliable tool calling (e.g. qwen2.5:14b, "
            "llama3.1:8b or larger), or check the server's tool-call parser/chat template."
        )
    logger.info(f"LLM_PROVIDER=local: {model!r} ({role}) at {base_url} made a tool call — OK")


async def preflight_local_llm() -> None:
    """Raise LocalLLMUnavailable unless every configured model can make a tool call."""
    base_url = settings.OPENAI_BASE_URL or ""
    served = await _served_models(base_url)
    models = _models()
    if served is not None:
        for role, model, _ in models:
            if not _is_served(model, served):
                listed = ", ".join(sorted(served)) or "none"
                raise LocalLLMUnavailable(
                    f"LLM_PROVIDER=local: {base_url} does not serve model {model!r} ({role}). "
                    f"Served: {listed}.\n"
                    f"  Fix: set {role} to one of those, or install it "
                    f"(Ollama: `ollama pull {model}`)."
                )
    for role, model, max_tokens in models:
        await _probe_tool_call(role, model, max_tokens, base_url)
