#!/usr/bin/env python
"""Verify KubeIntellect can talk to Qwen Cloud / DashScope — no cluster needed.

Runs the REAL LLM factory (app.core.llm) so it exercises exactly the code path
the agent uses. Checks two things that matter for KubeIntellect:

  1. Chat completion works against the configured Qwen model (coordinator tier).
  2. Tool / function calling works (the ReAct subagents depend on this).

Usage
-----
Put your DashScope config in v4/.env (see .env.example), then:

    cd v4
    set -a && source .env && set +a
    uv run python scripts/verify_qwen.py

Or inline (key never touches disk):

    LLM_PROVIDER=openai \
    OPENAI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1 \
    OPENAI_API_KEY=sk-your-dashscope-key \
    OPENAI_COORDINATOR_MODEL=qwen-max OPENAI_SUBAGENT_MODEL=qwen-plus \
    uv run python scripts/verify_qwen.py

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


@tool
def get_pod_status(namespace: str, pod: str) -> str:
    """Return the status of a pod. (stub used only to test tool-calling)"""
    return "CrashLoopBackOff"


def main() -> None:
    from app.core.config import settings

    print("── KubeIntellect ↔ Qwen Cloud connectivity check ──")
    print(f"provider    : {settings.LLM_PROVIDER}")
    print(f"base_url    : {settings.OPENAI_BASE_URL or '(default api.openai.com)'}")
    print(f"coordinator : {settings.OPENAI_COORDINATOR_MODEL}")
    print(f"subagent    : {settings.OPENAI_SUBAGENT_MODEL}")
    print()

    if settings.LLM_PROVIDER not in ("openai", "qwen"):
        _fail("LLM_PROVIDER must be 'qwen' (or 'openai') for DashScope/Qwen.", 2)
    if not settings.OPENAI_API_KEY:
        _fail("OPENAI_API_KEY (your DashScope key) is not set.", 2)
    if not settings.OPENAI_BASE_URL or "dashscope" not in settings.OPENAI_BASE_URL:
        print("\033[33m! OPENAI_BASE_URL does not look like a DashScope endpoint — continuing anyway.\033[0m")

    from app.core.llm import get_coordinator_llm, get_subagent_llm

    # 1. Basic chat completion.
    try:
        llm = get_coordinator_llm()
        resp = llm.invoke("Reply with exactly the word: pong")
        text = (resp.content or "").strip()
    except Exception as exc:
        _fail(f"chat completion failed: {exc}")
    _ok(f"chat completion works — model replied: {text[:60]!r}")

    # 2. Tool / function calling (subagent tier).
    try:
        sub = get_subagent_llm().bind_tools([get_pod_status])
        out = sub.invoke(
            "Use the get_pod_status tool to check pod 'api-0' in namespace 'prod'."
        )
        calls = getattr(out, "tool_calls", None) or []
    except Exception as exc:
        _fail(f"tool-calling request failed: {exc}")
    if not calls:
        _fail(
            "model did NOT emit a tool call — the ReAct subagents need function "
            "calling. Try qwen-max/qwen-plus (qwen-turbo tool-calling can be weaker)."
        )
    _ok(f"tool calling works — model called: {calls[0].get('name')}({calls[0].get('args')})")

    print()
    _ok("Qwen Cloud is ready for KubeIntellect. You can deploy to Alibaba ACK next.")


if __name__ == "__main__":
    main()
