"""Playbook library.

YAML-defined investigation playbooks for the top Kubernetes failure patterns.
Loaded once at import time into a frozen registry. The coordinator renders
matched playbooks into its system prompt to guide deterministic investigation.

Adding a new playbook: drop a new ``<name>.yaml`` next to this file with the
schema documented in ``loader.py``.
"""
from __future__ import annotations

from app.agent.playbooks.loader import (
    Playbook,
    get_playbook,
    list_playbooks,
    match_playbooks,
)

__all__ = ["Playbook", "get_playbook", "list_playbooks", "match_playbooks"]
