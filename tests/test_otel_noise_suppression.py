"""Tests for the OTel noise-suppression filter and otel_guard utilities.

Two required cases per the decision doc:
  (a) Benign ValueError from opentelemetry/langfuse namespace IS suppressed.
  (b) Real OTel error with a different message is NOT suppressed.
"""

import logging
import pytest
from app.utils.logger_config import _OtelContextCleanupFilter
from app.utils.otel_guard import safe_otel_ctx


# ---------------------------------------------------------------------------
# _OtelContextCleanupFilter tests
# ---------------------------------------------------------------------------

def _make_record(name: str, message: str, level: int = logging.ERROR) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="",
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )
    return record


class TestOtelContextCleanupFilter:
    def setup_method(self):
        self.f = _OtelContextCleanupFilter()

    def test_suppresses_benign_otel_valueerror(self):
        """(a) Benign ValueError from opentelemetry namespace is suppressed."""
        record = _make_record(
            name="opentelemetry.context",
            message="Token was created in a different Context",
        )
        assert self.f.filter(record) is False

    def test_suppresses_benign_langfuse_valueerror(self):
        """(a) Benign ValueError from langfuse namespace is also suppressed."""
        record = _make_record(
            name="langfuse.callback",
            message="Token was created in a different Context",
        )
        assert self.f.filter(record) is False

    def test_does_not_suppress_different_message(self):
        """(b) Real OTel error with a different message is NOT suppressed."""
        record = _make_record(
            name="opentelemetry.sdk.trace",
            message="Span export failed: connection refused",
        )
        assert self.f.filter(record) is True

    def test_does_not_suppress_benign_message_from_unrelated_logger(self):
        """Benign text in a non-OTel logger is NOT suppressed (single-condition match rejected)."""
        record = _make_record(
            name="app.orchestration.routing",
            message="Token was created in a different Context",
        )
        assert self.f.filter(record) is True

    def test_does_not_suppress_unrelated_app_error(self):
        """Unrelated app error is never suppressed."""
        record = _make_record(
            name="app.core.llm_gateway",
            message="LLM call failed: timeout",
        )
        assert self.f.filter(record) is True


# ---------------------------------------------------------------------------
# safe_otel_ctx tests
# ---------------------------------------------------------------------------

class _GoodCtx:
    """Context manager that exits cleanly."""
    entered = False
    exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *args):
        self.exited = True


class _BenignErrorCtx:
    """Context manager whose __exit__ raises the benign OTel ValueError."""
    def __enter__(self):
        return self

    def __exit__(self, *args):
        raise ValueError("Token was created in a different Context")


class _RealErrorCtx:
    """Context manager whose __exit__ raises a different ValueError."""
    def __enter__(self):
        return self

    def __exit__(self, *args):
        raise ValueError("Something else went wrong")


class TestSafeOtelCtx:
    def test_normal_exit_passes_through(self):
        ctx = _GoodCtx()
        with safe_otel_ctx(ctx):
            pass
        assert ctx.entered
        assert ctx.exited

    def test_benign_otel_valueerror_suppressed(self):
        """Benign 'Token was created in a different Context' is swallowed."""
        with safe_otel_ctx(_BenignErrorCtx()):
            pass  # should not raise

    def test_different_valueerror_propagates(self):
        """A ValueError with a different message is NOT suppressed."""
        with pytest.raises(ValueError, match="Something else went wrong"):
            with safe_otel_ctx(_RealErrorCtx()):
                pass

    def test_inner_exception_re_raised(self):
        """An exception raised inside the block is always re-raised."""
        with pytest.raises(RuntimeError, match="inner"):
            with safe_otel_ctx(_GoodCtx()):
                raise RuntimeError("inner")
