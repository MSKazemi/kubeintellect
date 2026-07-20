"""Guard: the tool registry is the single source of truth for agent tools.

``main_agent.build_agent`` wires ``tools=ALL_TOOLS`` directly, so the registry
and the compiled graph can never drift. These tests just pin the expected tool
surface so an accidental removal is caught.
"""

from app.tools.registry import ALL_TOOLS


def _tool_names() -> set[str]:
    return {getattr(t, "name", getattr(t, "__name__", "")) for t in ALL_TOOLS}


def test_registry_exposes_all_seven_tools():
    assert len(ALL_TOOLS) == 7


def test_registry_includes_memory_playbook_snapshot():
    names = _tool_names()
    for expected in (
        "run_kubectl",
        "query_prometheus",
        "query_loki",
        "refresh_snapshot",
        "read_memory",
        "write_memory",
        "lookup_playbook",
    ):
        assert expected in names, f"{expected} missing from ALL_TOOLS ({names})"


def test_main_agent_uses_registry_list():
    """build_agent must feed the registry list to create_deep_agent unchanged."""
    from unittest.mock import patch

    import app.agent.main_agent as ma

    with patch.object(ma, "create_deep_agent") as mock_create, patch.object(
        ma, "get_coordinator_llm"
    ), patch.object(ma, "build_subagents"):
        ma.build_agent(checkpointer=None)

    assert mock_create.call_args.kwargs["tools"] is ALL_TOOLS
