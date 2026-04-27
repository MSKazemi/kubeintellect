"""LLM factory — returns a configured chat model based on LLM_PROVIDER setting."""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, List, Type

from langchain_core.language_models import BaseChatModel

from app.core.config import settings

logger = logging.getLogger(__name__)

# Resolved once at import time so the ImportError warning fires only on startup,
# not on every request.  None = not yet resolved, False = unavailable.
_LangfuseCallbackHandler: Type[Any] | None | bool = None


def _resolve_langfuse() -> Type[Any] | None:
    global _LangfuseCallbackHandler
    if _LangfuseCallbackHandler is not None:
        return None if _LangfuseCallbackHandler is False else _LangfuseCallbackHandler  # type: ignore[return-value]
    try:
        from langfuse.langchain import CallbackHandler
        _LangfuseCallbackHandler = CallbackHandler
        return CallbackHandler
    except ImportError:
        logger.warning("LANGFUSE_ENABLED=true but 'langfuse' is not installed. Run: uv add langfuse")
        _LangfuseCallbackHandler = False
        return None


def get_langfuse_callbacks() -> List[Any]:
    """Return [CallbackHandler()] if Langfuse tracing is enabled, else [].

    Each call returns a fresh CallbackHandler so every LangGraph invocation
    gets its own trace context.  The Langfuse SDK batches + flushes internally.
    """
    if not settings.LANGFUSE_ENABLED:
        return []
    if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
        logger.warning("LANGFUSE_ENABLED=true but PUBLIC_KEY or SECRET_KEY is missing — tracing disabled.")
        return []
    CallbackHandler = _resolve_langfuse()
    if CallbackHandler is None:
        return []
    # Pass keys explicitly for compatibility with both langfuse v2 (keyword args)
    # and v3+ (reads LANGFUSE_* env vars automatically — no kwargs needed).
    try:
        return [CallbackHandler(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
        )]
    except TypeError:
        # langfuse v3+: env vars are already set — CallbackHandler picks them up.
        return [CallbackHandler()]


def _make_azure(deployment: str, temperature: float = 0.0, max_tokens: int = 4096) -> BaseChatModel:
    from langchain_openai import AzureChatOpenAI
    endpoint = settings.AZURE_OPENAI_ENDPOINT or ""
    if endpoint and not endpoint.startswith(("http://", "https://")):
        logger.warning(
            f"AZURE_OPENAI_ENDPOINT '{endpoint}' has no protocol — prepending https://. "
            "Set AZURE_OPENAI_ENDPOINT=https://... in ~/.kubeintellect/.env to silence this."
        )
        endpoint = f"https://{endpoint}"
    if not endpoint:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT is not set. Run 'kubeintellect init' or set it in ~/.kubeintellect/.env"
        )
    return AzureChatOpenAI(
        azure_deployment=deployment,
        azure_endpoint=endpoint,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
    )


def _make_openai(model: str, temperature: float = 0.0, max_tokens: int = 4096) -> BaseChatModel:
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=model,
        api_key=settings.OPENAI_API_KEY,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=True,
    )


@lru_cache(maxsize=4)
def _coordinator_llm() -> BaseChatModel:
    if settings.LLM_PROVIDER == "azure":
        return _make_azure(settings.AZURE_COORDINATOR_DEPLOYMENT, max_tokens=4096)
    return _make_openai(settings.OPENAI_COORDINATOR_MODEL, max_tokens=4096)


@lru_cache(maxsize=4)
def _subagent_llm() -> BaseChatModel:
    if settings.LLM_PROVIDER == "azure":
        return _make_azure(settings.AZURE_SUBAGENT_DEPLOYMENT, max_tokens=2048)
    return _make_openai(settings.OPENAI_SUBAGENT_MODEL, max_tokens=2048)


def get_coordinator_llm() -> BaseChatModel:
    """Full-capability model for coordinator and synthesizer."""
    return _coordinator_llm()


def get_subagent_llm() -> BaseChatModel:
    """Faster/cheaper model for parallel RCA subagents."""
    return _subagent_llm()
