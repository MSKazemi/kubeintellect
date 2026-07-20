"""Tool registry — single import point for all agent tools.

This is the canonical list of tools wired into the coordinator graph. Keep it in
sync with the ``tools=`` argument in :func:`app.agent.main_agent.build_agent`,
which imports ``ALL_TOOLS`` from here so the two can never drift.
"""

from app.tools.kubectl_tool import run_kubectl
from app.tools.loki_tool import query_loki
from app.tools.memory_tool import read_memory, write_memory
from app.tools.playbook_tool import lookup_playbook
from app.tools.prometheus_tool import query_prometheus
from app.tools.snapshot_tool import refresh_snapshot

ALL_TOOLS = [
    run_kubectl,
    query_prometheus,
    query_loki,
    refresh_snapshot,
    read_memory,
    write_memory,
    lookup_playbook,
]
