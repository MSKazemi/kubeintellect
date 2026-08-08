"""Regression guard: the run config must actually reach the nodes and tools that ask for it.

`user_role` (RBAC) and `hitl_bypass` (the HITL gate) travel only in the LangGraph run
config. Both LangGraph and LangChain decide whether to inject it by *pattern-matching the
`config` parameter's annotation*, not by name:

- ``langgraph._internal._runnable.KWARGS_CONFIG_KEYS`` accepts ``RunnableConfig``,
  ``"RunnableConfig"``, ``Optional[RunnableConfig]`` or ``"Optional[RunnableConfig]"``.
  With ``from __future__ import annotations`` the annotation is a *string*, so the PEP-604
  spelling ``RunnableConfig | None`` matches nothing.
- ``langchain_core.tools.base._get_runnable_config_param`` is stricter still:
  ``type_ is RunnableConfig`` — anything optional fails.

A mismatch does not raise. The parameter simply stays ``None``, RBAC falls back to the
default role and the HITL gate silently becomes a no-op. These tests fail loudly instead.
"""
from __future__ import annotations

import inspect
import warnings

from app.agent.workflow import build_graph
from app.cortex.graph import build_cortex_graph


def _nodes_declaring_config(builder):
    """Yield (name, node) for every graph node whose function takes a `config` param."""
    for name, node in builder.nodes.items():
        runnable = node.runnable
        target = getattr(runnable, "afunc", None) or getattr(runnable, "func", None)
        if target is None:
            continue
        if "config" in inspect.signature(target).parameters:
            yield name, node


class TestGraphNodeConfigInjection:
    def test_v2_graph_nodes_receive_config(self):
        builder = build_graph()
        declaring = dict(_nodes_declaring_config(builder))
        assert "coordinator" in declaring, "coordinator must still take the run config"
        for name, node in declaring.items():
            assert "config" in node.runnable.func_accepts, (
                f"node {name!r} declares a `config` parameter but LangGraph will not inject "
                f"it — check the annotation spelling (see this module's docstring)"
            )

    def test_cortex_graph_nodes_receive_config(self):
        builder = build_cortex_graph()
        declaring = dict(_nodes_declaring_config(builder))
        assert declaring, "the cortex graph should have at least one config-taking node"
        for name, node in declaring.items():
            assert "config" in node.runnable.func_accepts, (
                f"cortex node {name!r} declares a `config` parameter but LangGraph will not "
                f"inject it — check the annotation spelling"
            )

    def test_building_the_graphs_emits_no_config_typing_warning(self):
        """LangGraph warns rather than raises on a `config` annotation it cannot match."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            build_graph()
            build_cortex_graph()
        offenders = [str(w.message) for w in caught if "'config' parameter" in str(w.message)]
        assert offenders == [], f"LangGraph rejected a config annotation: {offenders}"


class TestToolConfigInjection:
    def test_run_kubectl_config_param_is_detected(self):
        """LangChain injects the run config only on an exact `RunnableConfig` annotation."""
        from langchain_core.tools.base import _get_runnable_config_param

        from app.tools.kubectl_tool import run_kubectl

        assert _get_runnable_config_param(run_kubectl.func) == "config", (
            "run_kubectl no longer receives the run config — user_role and hitl_bypass "
            "would both fall back to their defaults, disabling the HITL gate"
        )
