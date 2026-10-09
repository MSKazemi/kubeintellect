"""`LLM_PROVIDER=anthropic` on the default graph would route to OpenAI — say so, in those words.

The V2 graph (the default; `CORTEX_V4_ENABLED` is false) has no Anthropic backend. Before
#192 was closed, `app.core.llm._coordinator_llm` fell through to `_make_openai`, so a user
who selected `anthropic` got a `ChatOpenAI` client talking to `api.openai.com` with
`OPENAI_API_KEY`, and their `ANTHROPIC_API_KEY` was never read. That configuration is now
refused (see `test_anthropic_on_v2_is_refused_not_rerouted.py`); the message logged when the
config loads is the same text the refusal carries.

What this file pins is the *wording*: the message must name the vendor that would have
received the data. An earlier text said only that `anthropic` "is only used by the V4
cortex", which is the cause, not the consequence. Provider choice is frequently a compliance
decision (see `v4/docs/data-handling.md` and discussion #83), so the identity of that vendor
is the load-bearing fact, not the name of the graph.

Deleting `OpenAI` from that message must fail this file.
"""
from __future__ import annotations

import logging

from app.core.config import Settings


def _settings(**kw) -> Settings:
    # _env_file=None isolates the test from a developer's real v4/.env
    return Settings(_env_file=None, **kw)


class TestTheAnthropicFallbackWarning:
    def _warn(self, caplog, **kw) -> str:
        with caplog.at_level(logging.WARNING):
            _settings(
                LLM_PROVIDER="anthropic",
                ANTHROPIC_API_KEY="sk-ant-x",
                OPENAI_API_KEY="sk-openai-x",
                CORTEX_V4_ENABLED=False,
                **kw,
            )
        return "\n".join(r.getMessage() for r in caplog.records)

    def test_it_names_openai_as_the_recipient(self, caplog):
        text = self._warn(caplog)
        assert "OpenAI" in text, (
            "the warning must name the vendor that actually receives the cluster data; "
            f"got: {text!r}"
        )

    def test_it_names_the_credential_that_is_actually_used(self, caplog):
        text = self._warn(caplog)
        assert "OPENAI_API_KEY" in text
        assert "ANTHROPIC_API_KEY" in text, "say that the Anthropic key is ignored"

    def test_it_names_the_endpoint_including_a_custom_base_url(self, caplog):
        """A self-hosted OPENAI_BASE_URL changes who receives the data — report the real one."""
        text = self._warn(caplog, OPENAI_BASE_URL="https://llm.internal.example/v1")
        assert "https://llm.internal.example/v1" in text
        assert "api.openai.com" not in text

    def test_the_default_endpoint_is_named_when_no_base_url_is_set(self, caplog):
        assert "https://api.openai.com/v1" in self._warn(caplog)

    def test_no_warning_when_cortex_is_enabled(self, caplog):
        with caplog.at_level(logging.WARNING):
            _settings(
                LLM_PROVIDER="anthropic",
                ANTHROPIC_API_KEY="sk-ant-x",
                CORTEX_V4_ENABLED=True,
            )
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "requires CORTEX_V4_ENABLED=true" not in joined
