"""Tiered model factory (V4 architecture §3).

Tiers:
  triage     — small/fast; classification + plan JSON; never streams
  specialist — small/fast; the gather tool loop; never streams
  synthesis  — large; the final answer; the ONLY streaming call in the graph

Providers: azure | openai (existing factory machinery) and optionally
anthropic (lazy import — requires the `anthropic` extra). Non-synthesis tiers
set disable_streaming so astream_events carries tokens only for the answer.
"""
from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _anthropic(model: str, streaming: bool) -> BaseChatModel:
    try:
        from langchain_anthropic import (  # type: ignore[import-not-found]  # optional extra
            ChatAnthropic,
        )
    except ImportError as exc:
        raise RuntimeError(
            "LLM_PROVIDER=anthropic requires the anthropic extra: "
            "uv add langchain-anthropic"
        ) from exc
    return ChatAnthropic(
        model=model,
        api_key=settings.ANTHROPIC_API_KEY,
        # Honour LLM_TEMPERATURE like the azure/openai makers in app.core.llm do. This was
        # hardcoded to 0.0, so the setting silently stopped configuring when the provider
        # changed — invisible here because Anthropic accepts 0.0, unlike the three Azure
        # deployments whose 400 is what made the temperature configurable in the first place.
        temperature=settings.LLM_TEMPERATURE,
        max_tokens=4096,
        disable_streaming=not streaming,
    )


def _tier(small: bool, streaming: bool) -> BaseChatModel:
    provider = settings.LLM_PROVIDER
    if provider == "anthropic":
        model = settings.ANTHROPIC_SMALL_MODEL if small else settings.ANTHROPIC_LARGE_MODEL
        return _anthropic(model, streaming)
    # azure / openai reuse the V2 maker functions (validated endpoints)
    from app.core.llm import _make_azure, _make_openai

    if provider == "azure":
        deployment = (
            settings.AZURE_SUBAGENT_DEPLOYMENT if small
            else settings.AZURE_COORDINATOR_DEPLOYMENT
        )
        return _make_azure(deployment, max_tokens=2048 if small else 4096,
                           streaming=streaming)
    model = (
        settings.OPENAI_SUBAGENT_MODEL if small else settings.OPENAI_COORDINATOR_MODEL
    )
    return _make_openai(model, max_tokens=2048 if small else 4096, streaming=streaming)


@lru_cache(maxsize=1)
def get_triage_llm() -> BaseChatModel:
    return _tier(small=True, streaming=False)


@lru_cache(maxsize=1)
def get_specialist_llm() -> BaseChatModel:
    return _tier(small=True, streaming=False)


@lru_cache(maxsize=1)
def get_synthesis_llm() -> BaseChatModel:
    return _tier(small=False, streaming=True)
