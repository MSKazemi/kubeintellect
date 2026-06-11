"""DeepAgents context schema for KubeIntellect (v3).

The DeepAgents middleware provides the full agent state (messages, todos,
files) automatically — we only need to declare the *context* fields that
ride on RunnableConfig.configurable and are read by tools (e.g. RBAC in
run_kubectl). Everything else lives in the virtual filesystem.
"""

from __future__ import annotations

from typing_extensions import TypedDict


class KubeIntellectContext(TypedDict, total=False):
    """Per-invocation context. Passed via config['configurable']."""

    session_id: str
    user_id: str
    user_role: str  # superadmin | admin | operator | readonly
    hitl_bypass: bool  # auto-approve session bypass for destructive verbs
    thread_id: str  # checkpointer thread key (mirrors session_id)
