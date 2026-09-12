#!/usr/bin/env python
"""Verify KubeIntellect can talk to its configured LLM — any provider, no cluster needed.

Runs the REAL LLM factory (``app.core.llm``) so it exercises exactly the code path the agent
uses, and checks the two things that decide whether KubeIntellect works at all:

  1. A chat completion against the coordinator model.
  2. **Tool / function calling** against the subagent model — the ReAct subagents cannot
     investigate anything without it, and a model that chats fine but emits no tool call
     produces an agent that answers confidently and never looks at the cluster.

``verify_qwen.py`` is the DashScope-specific ancestor of this script and still works; this
one drops the Qwen-only guards so it can check OpenAI, Azure OpenAI, AWS Bedrock, a LiteLLM
or vLLM proxy, Ollama, or anything else OpenAI-compatible.

Usage
-----
    cd v4
    set -a && source .env && set +a
    uv run python scripts/verify_llm.py

Or inline, so the key never touches disk. AWS Bedrock, via its OpenAI-compatible endpoint:

    LLM_PROVIDER=openai \
    OPENAI_BASE_URL=https://bedrock-runtime.eu-central-1.amazonaws.com/openai/v1 \
    OPENAI_API_KEY="$AWS_BEARER_TOKEN_BEDROCK" \
    OPENAI_COORDINATOR_MODEL=openai.gpt-oss-120b \
    OPENAI_SUBAGENT_MODEL=openai.gpt-oss-20b \
    uv run python scripts/verify_llm.py

Exit codes: 0 = all checks passed, 1 = a check failed, 2 = misconfiguration.
"""
from __future__ import annotations

import sys

from langchain_core.tools import tool


def _fail(msg: str, code: int = 1) -> None:
    print(f"\033[31m✗ {msg}\033[0m")
    sys.exit(code)


def _ok(msg: str) -> None:
    print(f"\033[32m✓ {msg}\033[0m")


def _warn(msg: str) -> None:
    print(f"\033[33m! {msg}\033[0m")


@tool
def get_pod_status(namespace: str, pod: str) -> str:
    """Return the status of a pod. (stub used only to test tool-calling)"""
    return "CrashLoopBackOff"


def _describe_endpoint(base_url: str | None) -> str:
    """Name the endpoint family, so the output says what was actually contacted."""
    if not base_url:
        return "OpenAI (api.openai.com)"
    u = base_url.lower()
    if "bedrock" in u:
        return "AWS Bedrock (OpenAI-compatible)"
    if "dashscope" in u:
        return "Alibaba DashScope / Qwen"
    if "openai.azure.com" in u or "cognitiveservices" in u:
        return "Azure OpenAI"
    if "11434" in u or "ollama" in u:
        return "Ollama"
    return "custom OpenAI-compatible endpoint"


def _tool_call_hint(base_url: str | None) -> str:
    """Tool-calling is the check that fails for provider-specific reasons."""
    u = (base_url or "").lower()
    if "bedrock" in u:
        return (
            "On Bedrock, confirm the model is enabled for your account AND supports "
            "client-side tool use. Check the model id format too — Bedrock ids look like "
            "'openai.gpt-oss-120b' or 'anthropic.claude-...', not bare OpenAI names."
        )
    if "dashscope" in u:
        return "Try qwen-max / qwen-plus — qwen-turbo's tool-calling is weaker."
    if "11434" in u or "ollama" in u:
        return "Many small local models cannot emit tool calls at all. Try a larger one."
    return "Pick a model that supports function calling."


def main() -> None:
    from app.core.config import settings

    endpoint = _describe_endpoint(settings.OPENAI_BASE_URL)

    print("── KubeIntellect ↔ LLM connectivity check ──")
    print(f"provider    : {settings.LLM_PROVIDER}")
    print(f"endpoint    : {endpoint}")
    print(f"base_url    : {settings.OPENAI_BASE_URL or '(default api.openai.com)'}")
    print(f"coordinator : {settings.OPENAI_COORDINATOR_MODEL}")
    print(f"subagent    : {settings.OPENAI_SUBAGENT_MODEL}")
    print(f"temperature : {settings.LLM_TEMPERATURE}")
    print()

    if settings.LLM_PROVIDER == "azure":
        if not (settings.AZURE_OPENAI_API_KEY and settings.AZURE_OPENAI_ENDPOINT):
            _fail("LLM_PROVIDER=azure but AZURE_OPENAI_API_KEY/ENDPOINT are not both set.", 2)
    elif settings.LLM_PROVIDER in ("openai", "qwen"):
        if not settings.OPENAI_API_KEY:
            _fail("OPENAI_API_KEY is not set. For Bedrock this is your Bedrock API key.", 2)
    elif settings.LLM_PROVIDER == "anthropic":
        if not settings.ANTHROPIC_API_KEY:
            _fail("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.", 2)
        if not settings.CORTEX_V4_ENABLED:
            _warn("LLM_PROVIDER=anthropic is only used by the V4 cortex (CORTEX_V4_ENABLED).")

    from app.core.llm import get_coordinator_llm, get_subagent_llm

    # 1. Basic chat completion.
    try:
        resp = get_coordinator_llm().invoke("Reply with exactly the word: pong")
        text = (resp.content or "").strip()
    except Exception as exc:
        hint = ""
        if "temperature" in str(exc).lower():
            hint = (
                "\n  This model may reject temperature=0.0 — several do, with HTTP 400. "
                "Set LLM_TEMPERATURE=1.0 and retry."
            )
        _fail(f"chat completion failed: {exc}{hint}")
    if not text:
        _fail("chat completion returned EMPTY content — the model answered nothing.")
    _ok(f"chat completion works — model replied: {text[:60]!r}")

    # 2. Tool / function calling. This is the one that silently ruins the agent.
    try:
        sub = get_subagent_llm().bind_tools([get_pod_status])
        out = sub.invoke("Use the get_pod_status tool to check pod 'api-0' in namespace 'prod'.")
        calls = getattr(out, "tool_calls", None) or []
    except Exception as exc:
        _fail(f"tool-calling request failed: {exc}\n  {_tool_call_hint(settings.OPENAI_BASE_URL)}")
    if not calls:
        _fail(
            "model did NOT emit a tool call. The ReAct subagents depend on function calling; "
            "without it the agent will answer without ever reading the cluster.\n  "
            + _tool_call_hint(settings.OPENAI_BASE_URL)
        )
    _ok(f"tool calling works — model called: {calls[0].get('name')}({calls[0].get('args')})")

    print()
    _ok(f"{endpoint} is ready for KubeIntellect.")


if __name__ == "__main__":
    main()
