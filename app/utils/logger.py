"""Structured logger setup."""
from __future__ import annotations

import logging
import sys
import time
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


# ── Filters ──────────────────────────────────────────────────────────────────


class _RequestIdFilter(logging.Filter):
    """Inject request_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        return True


class _HealthzFilter(logging.Filter):
    """Drop uvicorn access-log lines for the /healthz probe path."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/healthz" not in record.getMessage()


# ── Formatters ────────────────────────────────────────────────────────────────


class _JsonFormatter(logging.Formatter):
    """
    Emit one JSON object per line.

    Fields: time, level, logger, request_id, msg
    Optional extra fields are merged in when present on the record.
    """

    _EXTRA_FIELDS = ("duration_ms", "method", "path", "status", "user_id", "session_id")

    def format(self, record: logging.LogRecord) -> str:
        import json

        payload: dict = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "msg": record.getMessage(),
        }
        for field in self._EXTRA_FIELDS:
            val = getattr(record, field, None)
            if val is not None:
                payload[field] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


# ── Public API ────────────────────────────────────────────────────────────────


def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger under the 'kubeintellect' hierarchy.

    Usage:
        from app.utils.logger import get_logger
        logger = get_logger(__name__)
    """
    if not name.startswith("kubeintellect"):
        # Map 'app.foo.bar' → 'kubeintellect.foo.bar'
        suffix = name.removeprefix("app").lstrip(".")
        name = f"kubeintellect.{suffix}" if suffix else "kubeintellect"
    return logging.getLogger(name)


def setup_logging(name: str = "kubeintellect") -> logging.Logger:
    from app.core.config import settings

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    # ── Root app logger ───────────────────────────────────────────────────────
    root_logger = logging.getLogger(name)
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.addFilter(_RequestIdFilter())

        if settings.LOG_FORMAT == "json":
            handler.setFormatter(_JsonFormatter())
        else:
            fmt = "%(asctime)s [%(levelname)-8s] %(name)s [%(request_id)s] %(message)s"
            handler.setFormatter(logging.Formatter(fmt))

        root_logger.addHandler(handler)
        root_logger.setLevel(level)
        root_logger.propagate = False

    # ── Uvicorn loggers ───────────────────────────────────────────────────────
    # Access log: silence /healthz probe noise; keep everything else.
    uv_access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _HealthzFilter) for f in uv_access.filters):
        uv_access.addFilter(_HealthzFilter())

    # Error log: mirror to our handler so errors land in the same stream.
    uv_error = logging.getLogger("uvicorn.error")
    if not uv_error.handlers:
        uv_error.propagate = True  # let it bubble to the root uvicorn logger

    return root_logger


logger = setup_logging()
