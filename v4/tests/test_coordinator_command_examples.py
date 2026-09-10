"""Prompt examples must not teach unresolved command arguments."""
import re

from app.agent.nodes.coordinator import _COORDINATOR_SYSTEM


def test_command_examples_have_no_unresolved_metavariables():
    # Match lower- and upper-case placeholders, including --grace-period=<N>.
    # JSONPath braces and the literal output sentinel <none> are not arguments
    # on kubectl command lines and must not be confused with metavariables.
    hits = re.findall(
        r"(?:kubectl[^\n]*|--[a-z][a-z-]*=)<[A-Za-z][A-Za-z0-9_.-]*>[^\n]*",
        _COORDINATOR_SYSTEM,
    )
    assert hits == [], hits
