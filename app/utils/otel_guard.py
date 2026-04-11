"""
OTel context-cleanup guard utilities.

Background: when a Langfuse/OpenTelemetry span is created in one asyncio Task
context and its ``__exit__`` is called from a *different* asyncio Task (which
happens when an async generator crosses task-context boundaries), the OTel SDK
raises ``ValueError: Token was created in a different Context``.  The span
itself is recorded correctly — only the cleanup token reset fails.

As of Langfuse 4.x, the ``CallbackHandler._detach_observation`` method catches
this error natively (``except Exception: pass``) with an explicit comment that
it is expected and safe to ignore in async scenarios.  The structural root cause
is therefore handled at the SDK level for all ``app.astream()`` call-sites.

These guards remain as **defence-in-depth** for call-sites in our own code that
use Langfuse context managers directly (e.g. ``routing.py`` with
``langfuse.propagate_attributes``).  The ``_OtelContextCleanupFilter`` in
``app/utils/logger_config.py`` similarly remains as a second defence layer for
any future OTel SDK path that logs the error before suppressing it.

Usage (sync)::

    from app.utils.otel_guard import safe_otel_ctx

    with safe_otel_ctx(some_langfuse_span):
        ...

Usage (async)::

    from app.utils.otel_guard import async_safe_otel_ctx

    async with async_safe_otel_ctx(some_langfuse_span):
        ...
"""

from contextlib import contextmanager, asynccontextmanager
from typing import Any

_BENIGN_MSG = "Token was created in a different Context"


@contextmanager
def safe_otel_ctx(ctx: Any):
    """Sync context manager that suppresses the benign OTel cross-context ValueError."""
    ctx.__enter__()
    try:
        yield ctx
    except BaseException as exc:
        try:
            ctx.__exit__(type(exc), exc, exc.__traceback__)
        except ValueError as ve:
            if _BENIGN_MSG not in str(ve):
                raise
        raise
    else:
        try:
            ctx.__exit__(None, None, None)
        except ValueError as ve:
            if _BENIGN_MSG not in str(ve):
                raise


@asynccontextmanager
async def async_safe_otel_ctx(ctx: Any):
    """Async context manager that suppresses the benign OTel cross-context ValueError."""
    if hasattr(ctx, "__aenter__"):
        await ctx.__aenter__()
    else:
        ctx.__enter__()
    try:
        yield ctx
    except BaseException as exc:
        try:
            if hasattr(ctx, "__aexit__"):
                await ctx.__aexit__(type(exc), exc, exc.__traceback__)
            else:
                ctx.__exit__(type(exc), exc, exc.__traceback__)
        except ValueError as ve:
            if _BENIGN_MSG not in str(ve):
                raise
        raise
    else:
        try:
            if hasattr(ctx, "__aexit__"):
                await ctx.__aexit__(None, None, None)
            else:
                ctx.__exit__(None, None, None)
        except ValueError as ve:
            if _BENIGN_MSG not in str(ve):
                raise
