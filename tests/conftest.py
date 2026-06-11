"""
Test configuration — stubs heavy infrastructure so app modules can be imported
and unit-tested without any running services, tokens, or a real cluster.

Loaded by pytest before any test module is imported.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

# ── Pydantic Settings — supply dummy values so Settings() validates ──────────
# Use os.environ[...] = (not setdefault) to force-override any values that
# might be present in the local .env file.  Tests that need auth enabled apply
# the auth_settings fixture explicitly.
os.environ.setdefault("LLM_PROVIDER", "openai")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

# Force auth off by default so tests without the auth_settings fixture get
# role="admin" without needing a Bearer token. settings.auth_enabled is True if
# ANY key tier OR the HMAC demo secret is set, so every auth input the local .env
# might define must be force-cleared here — including the superadmin tier and the
# HMAC demo-key secret added in v3.
os.environ["KUBEINTELLECT_SUPERADMIN_KEYS"] = ""
os.environ["KUBEINTELLECT_ADMIN_KEYS"] = ""
os.environ["KUBEINTELLECT_OPERATOR_KEYS"] = ""
os.environ["KUBEINTELLECT_READONLY_KEYS"] = ""
os.environ["DEMO_KEY_HMAC_SECRET"] = ""
os.environ["AUTH_BACKEND"] = "static"

# ── Stub langgraph postgres checkpointer ─────────────────────────────────────
# AsyncPostgresSaver.from_conn_string is called inside init_graph() at startup.
# Stub the module so no TCP connection is attempted — the real
# app.agent.main_agent module imports fine with this stub in place
# (connection only happens in init_graph(), not at module level).
_pg_saver = MagicMock()
_pg_saver.AsyncPostgresSaver = MagicMock()
sys.modules.setdefault("langgraph.checkpoint.postgres.aio", _pg_saver)
