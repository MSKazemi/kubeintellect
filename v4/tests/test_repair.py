"""Misconfig auto-repair (v5 P3) — LLM fix proposal, fail-safe."""
from __future__ import annotations

from app.tools.aci.repair import _strip_fences, propose_fix
from langchain_core.messages import AIMessage

_ORIG = "apiVersion: apps/v1\nkind: Deployment\nspec:\n  securityContext:\n    runAsNonRoot: false\n"
_FIXED = "apiVersion: apps/v1\nkind: Deployment\nspec:\n  securityContext:\n    runAsNonRoot: true\n"


class _LLM:
    def __init__(self, content):
        self._content = content

    async def ainvoke(self, messages, config=None):
        return AIMessage(content=self._content)


class _Boom:
    async def ainvoke(self, messages, config=None):
        raise RuntimeError("model down")


class TestStripFences:
    def test_strips_yaml_fence(self):
        assert _strip_fences("```yaml\nkind: X\n```") == "kind: X"

    def test_plain_unchanged(self):
        assert _strip_fences("kind: X") == "kind: X"


class TestProposeFix:
    async def test_returns_corrected(self):
        out = await propose_fix(_ORIG, "runAsNonRoot must be true", llm=_LLM(_FIXED))
        assert "runAsNonRoot: true" in out

    async def test_strips_fences_from_reply(self):
        out = await propose_fix(_ORIG, "v", llm=_LLM(f"```yaml\n{_FIXED}```"))
        assert out.startswith("apiVersion") and "runAsNonRoot: true" in out

    async def test_empty_reply_returns_original(self):
        assert await propose_fix(_ORIG, "v", llm=_LLM("")) == _ORIG

    async def test_reply_without_kind_returns_original(self):
        # a reply that lost the manifest (e.g. prose) must not overwrite it
        assert await propose_fix(_ORIG, "v", llm=_LLM("Sure! I fixed it for you.")) == _ORIG

    async def test_exception_fails_safe(self):
        assert await propose_fix(_ORIG, "v", llm=_Boom()) == _ORIG
