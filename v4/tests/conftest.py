"""
Test configuration — stubs heavy infrastructure so app modules can be imported
and unit-tested without any running services, tokens, or a real cluster.

Loaded by pytest before any test module is imported.
"""
import importlib.util
import os
import sys
from unittest.mock import MagicMock

# ── Evaluation-harness tests ─────────────────────────────────────────────────
# These modules test the `evaluation/` research harness, which lives outside the
# published source tree. Skip collecting them when it is not importable, so the
# suite is green on a clean clone instead of erroring at collection time.
if importlib.util.find_spec("evaluation") is None:
    collect_ignore = [
        "test_aggregate.py",
        "test_compare.py",
        "test_evaluation_runner.py",
        "test_follow_up.py",
        "test_loki_telemetry.py",
        "test_signal_scorer.py",
    ]

# ── Pydantic Settings — supply dummy values so Settings() validates ──────────
# Use os.environ[...] = (not setdefault) to force-override any values that
# might be present in the local .env file.  Tests that need auth enabled apply
# the auth_settings fixture explicitly.
os.environ.setdefault("LLM_PROVIDER", "openai")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

# Force auth off by default so tests without the auth_settings fixture get
# role="admin" without needing a Bearer token.
os.environ["KUBEINTELLECT_ADMIN_KEYS"] = ""
os.environ["KUBEINTELLECT_OPERATOR_KEYS"] = ""
os.environ["KUBEINTELLECT_READONLY_KEYS"] = ""

# ── Stub langgraph postgres checkpointer ─────────────────────────────────────
# AsyncPostgresSaver.from_conn_string is called inside init_graph() at startup.
# Stub the module so no TCP connection is attempted — the real app.agent.workflow
# module imports fine with this stub in place (connection only happens in
# init_graph(), not at module level).
_pg_saver = MagicMock()
_pg_saver.AsyncPostgresSaver = MagicMock()
sys.modules.setdefault("langgraph.checkpoint.postgres.aio", _pg_saver)
