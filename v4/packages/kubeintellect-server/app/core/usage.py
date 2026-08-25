"""Per-request token accounting.

The server used to surface no usage at all: nothing in `app/api/` or `app/cortex/` read
`usage_metadata`, and the SSE stream carried no usage frame, so a client had no way to learn
what a request cost. The consequence was not theoretical — an evaluation campaign recorded
`tokens == 0` on all 72 of its predictions, wrote off every cost number, and the figures had to
be reconstructed months later by joining archived Langfuse traces on `service.instance.id`. The
data existed the whole time; it just never came back down the wire.

WHY A CALLBACK AND NOT A WRAPPER
--------------------------------
Token usage has to be summed across every model call a request makes, and one request can make
many: triage, the coordinator's ReAct loop, a four-way subagent fan-out, verification, brief
generation. Wrapping each call site means finding all of them and then finding each new one
forever. A `BaseCallbackHandler` attached once to the run config sees them all, including calls
made by LangGraph nodes this module has never heard of.

WHY A MUTABLE OBJECT IN THE CONTEXTVAR
--------------------------------------
`asyncio.create_task` copies the context, so a task that *rebinds* a ContextVar changes only its
own copy. The subagent fan-out runs its branches as tasks, and their tokens are exactly what must
not be lost. So the var holds one mutable meter that is bound once per request and only ever
mutated — never reassigned inside the request. Rebinding it in a nested task would silently drop
that branch's tokens, which is the failure this module exists to prevent.
"""
from __future__ import annotations

import contextvars
from threading import Lock
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler


class UsageMeter:
    """Accumulates token counts for one request. Safe to mutate from several tasks."""

    __slots__ = ("_lock", "input_tokens", "output_tokens", "llm_calls")

    def __init__(self) -> None:
        self._lock = Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_calls = 0

    def add(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self._lock:
            self.input_tokens += max(0, int(input_tokens or 0))
            self.output_tokens += max(0, int(output_tokens or 0))
            self.llm_calls += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int]:
        with self._lock:
            return {
                "prompt_tokens": self.input_tokens,
                "completion_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "llm_calls": self.llm_calls,
            }


#: Bound once per request. Never rebind inside a nested task — see the module docstring.
_meter_var: contextvars.ContextVar[UsageMeter | None] = contextvars.ContextVar(
    "ki_usage_meter", default=None
)


def start_request_meter() -> UsageMeter:
    """Bind a fresh meter for this request and return it."""
    meter = UsageMeter()
    _meter_var.set(meter)
    return meter


def current_meter() -> UsageMeter | None:
    return _meter_var.get()


def current_usage() -> dict[str, int]:
    """Usage so far for this request, or a well-formed zero when nothing was metered.

    Zeros are returned rather than `None` so a caller never has to special-case the shape. They
    are still distinguishable from a genuine zero-cost request by `llm_calls`, which is 0 only
    when no model was called at all.
    """
    meter = _meter_var.get()
    return meter.as_dict() if meter is not None else UsageMeter().as_dict()


def _extract(response: Any) -> tuple[int, int]:
    """Pull (input, output) token counts out of an LLMResult.

    Providers disagree about where they put this, and the same provider disagrees with itself
    across versions, so all three known shapes are tried before giving up:

      * `generation.message.usage_metadata` — the modern, provider-neutral LangChain shape;
      * `llm_output["token_usage"]` — what the OpenAI/Azure integrations have long emitted;
      * `llm_output["usage"]` — the raw provider payload, when the integration passes it through.

    Returning (0, 0) is correct for a provider that reports nothing; `llm_calls` still
    increments, so "called the model 40 times and metered 0 tokens" stays visible as an
    instrumentation gap rather than reading as a free request.
    """
    for gens in getattr(response, "generations", None) or []:
        for gen in gens or []:
            usage = getattr(getattr(gen, "message", None), "usage_metadata", None)
            if usage:
                return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    out = getattr(response, "llm_output", None) or {}
    for key in ("token_usage", "usage"):
        usage = out.get(key) if isinstance(out, dict) else None
        if isinstance(usage, dict) and usage:
            prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
            completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
            return int(prompt or 0), int(completion or 0)
    return 0, 0


class TokenMeterCallback(BaseCallbackHandler):
    """Sums token usage for every model call made under one request.

    Attached alongside the Langfuse handler, so usage is reported to the client even when
    tracing is switched off — the campaign that lost its cost data *had* Langfuse enabled, and
    still shipped zeros, because nothing joined the trace back to the response.
    """

    #: LangChain will not silently drop this handler's exceptions; we must not raise any.
    raise_error = False

    def on_llm_end(self, response: Any, **_kwargs: Any) -> None:
        meter = _meter_var.get()
        if meter is None:
            return
        try:
            prompt, completion = _extract(response)
        except Exception:  # noqa: BLE001 - accounting must never break a request
            return
        meter.add(input_tokens=prompt, output_tokens=completion)
