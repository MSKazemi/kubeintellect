"""Tool registry — single import point for all agent tools."""
from app.tools.helm_tool import run_helm
from app.tools.kubectl_tool import run_kubectl
from app.tools.loki_tool import query_loki
from app.tools.prometheus_tool import query_prometheus

ALL_TOOLS = [run_kubectl, run_helm, query_prometheus, query_loki]
