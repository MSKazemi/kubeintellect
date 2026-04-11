"""
Test configuration.

Stubs out heavy infrastructure (Postgres, K8s orchestration) so that app
modules can be imported and exercised without any running services or tokens.

This file is loaded by pytest before any test module is imported, which
guarantees the sys.modules patches below are in place before any app code
executes at module level.
"""
import os
import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Minimal env vars required by Pydantic Settings validation
#
# AZURE_OPENAI_ENDPOINT is typed `str` but defaults to None — Pydantic v2
# raises a ValidationError when no value is present.  Set dummy values so
# Settings() succeeds without a real .env file.
# ---------------------------------------------------------------------------
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test.openai.azure.com/")
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key-00000000000000000000000000000000")
os.environ.setdefault("AZURE_OPENAI_API_VERSION", "2024-02-01")
os.environ.setdefault("AZURE_PRIMARY_LLM_DEPLOYMENT_NAME", "gpt-4o")
os.environ.setdefault("SUPERVISOR_AZURE_DEPLOYMENT_NAME", "gpt-4o")
os.environ.setdefault("LLM_PROVIDER", "azure")

# ---------------------------------------------------------------------------
# Stub psycopg2
#
# chat_completions.py executes  `pg_checkpointer = get_checkpointer()`  at
# import time.  PostgresCheckpointer.__init__ calls ThreadedConnectionPool,
# which would attempt a real TCP connection.  Replace psycopg2 with a mock
# so that call is a no-op.
#
# OperationalError must be a real exception class so that the  except clause
# in _init_pool doesn't blow up.
# ---------------------------------------------------------------------------
_OperationalError = type("OperationalError", (Exception,), {})

_psycopg2 = MagicMock()
_psycopg2.OperationalError = _OperationalError

_psycopg2_pool = MagicMock()
_psycopg2_pool.ThreadedConnectionPool = MagicMock(return_value=MagicMock())

sys.modules.setdefault("psycopg2", _psycopg2)
sys.modules.setdefault("psycopg2.pool", _psycopg2_pool)
sys.modules.setdefault("psycopg2.extensions", MagicMock())

# ---------------------------------------------------------------------------
# Stub the orchestration workflow module
#
# chat_completions.py imports run_kubeintellect_workflow from it.  That
# import transitively pulls in kubernetes_tools, LangGraph graph builders,
# and LLM provider auth — none of which are needed for validation tests.
# ---------------------------------------------------------------------------
sys.modules.setdefault("app.orchestration.workflow", MagicMock())
