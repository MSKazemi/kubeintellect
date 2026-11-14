"""An unattended session is shown, on every model call, what it already tried and how each attempt ended.

Measured 2026-11-14 (`papers/v4-operator-r1/A3-LOOP-INVESTIGATION.md` §2c): in E3 the facts that would have ended the
loop were in the session's context — 83 model calls, 2.04 M input tokens of near-identical NotFound/422 pairs — but
nothing kept them in front of the model, and long-context research finds information in the middle of a long input
is used less reliably (Liu et al., TACL 2024). The tool keeps a short record of the session's writes and their
outcomes, written by code from kubectl's replies (never from the model), and the coordinator appends it to the
system message of every model call in an unattended session. Chat sessions are unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import app.agent.nodes.coordinator as coord_mod
from app.tools import kubectl_tool as kt

REJECTED = 'The request is invalid: patch: Invalid value: "map[...]": unrecognized type: string'
CM = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n  namespace: h3-2\ndata:\n  k: '{v}'\n"


def _write(value: str, rc: int):
    def fake_run(argv, *a, **k):
        return SimpleNamespace(
            returncode=rc,
            stdout="" if rc else "configmap/c configured\n",
            stderr=REJECTED if rc else "",
        )

    cfg = {
        "configurable": {
            "user_role": "operator",
            "hitl_bypass": True,
            "thread_id": "auto-led",
            "bound_namespace": "h3-2",
        }
    }
    with (
        mock.patch.object(kt.subprocess, "run", fake_run),
        mock.patch.object(kt, "_capture_rollback_point", lambda *a, **k: None),
    ):
        return kt.run_kubectl.invoke(
            {"command": "kubectl apply -f - -n h3-2", "stdin": CM.format(v=value)},
            config=cfg,
        )


def test_the_ledger_records_each_write_and_its_outcome():
    assert kt.session_ledger("auto-led") == ""
    _write("a", rc=1)
    _write("a", rc=1)  # the identical resend is refused by the repeat guard
    _write("b", rc=0)
    ledger = kt.session_ledger("auto-led")
    assert "recorded by the tool" in ledger
    assert ledger.count("kubectl apply -f - -n h3-2") == 3
    assert (
        "unrecognized type" in ledger
        and "Repeat refused" in ledger
        and "configured" in ledger
    )


class _Recording(GenericFakeChatModel):
    seen: list = []

    def bind_tools(self, tools, **kw):
        return self

    def _generate(self, messages, *args, **kwargs):
        _Recording.seen.append(list(messages))
        return super()._generate(messages, *args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("bypass", [True, False])
async def test_the_coordinator_puts_it_in_front_of_every_model_call(
    monkeypatch, bypass
):
    _write("a", rc=1)
    _Recording.seen = []
    model = _Recording(messages=iter([AIMessage(content="done")]))
    monkeypatch.setattr(coord_mod, "get_coordinator_llm", lambda: model)
    state = {
        "messages": [HumanMessage(content="[autonomous investigation] fix it")],
        "session_id": "auto-led",
        "findings": [],
        "memory_context": "",
        "cluster_snapshot": "",
    }
    await coord_mod.coordinator(
        state, config={"configurable": {"thread_id": "auto-led", "hitl_bypass": bypass}}
    )
    system = next(m for m in _Recording.seen[0] if isinstance(m, SystemMessage)).content
    if bypass:
        assert (
            "Writes this session already attempted" in system
            and "unrecognized type" in system
        )
    else:
        assert "Writes this session already attempted" not in system
