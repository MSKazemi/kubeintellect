"""Tool registry — single import point for all agent tools."""
from app.tools.kubectl_tool import run_kubectl
from app.tools.prometheus_tool import query_prometheus
from app.tools.loki_tool import query_loki

ALL_TOOLS = [run_kubectl, query_prometheus, query_loki]
