import json
import logging
import logging.handlers
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Request-ID context variable
# ---------------------------------------------------------------------------
# Set this at the start of each request (e.g. in middleware) and every log
# record emitted during that request will automatically carry the value.

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


# ---------------------------------------------------------------------------
# Logging filter — injects request_id into every record
# ---------------------------------------------------------------------------

class RequestIdFilter(logging.Filter):
    """Adds the current request_id context variable to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        return True


# ---------------------------------------------------------------------------
# JSON formatter — emits one JSON object per line
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """
    Structured JSON formatter.

    Each line is a JSON object with stable fields:
      timestamp, level, logger, request_id, message,
      module, function, line
    Plus exc_text when an exception is present.
    Any extra fields passed via logger.info(..., extra={...}) are merged in.
    """

    RESERVED = frozenset(
        logging.LogRecord(
            "", 0, "", 0, "", (), None
        ).__dict__.keys()
    ) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        obj: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.message,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Merge any caller-supplied extra fields
        for key, val in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                obj[key] = val

        if record.exc_info:
            obj["exc_text"] = self.formatException(record.exc_info)
        elif record.exc_text:
            obj["exc_text"] = record.exc_text

        return json.dumps(obj, default=str)


# ---------------------------------------------------------------------------
# OTel noise suppression filter
# ---------------------------------------------------------------------------

class _OtelContextCleanupFilter(logging.Filter):
    """Suppress the spurious OTel cross-context ValueError from opentelemetry/langfuse loggers.

    Root cause: OTel raises ``ValueError: Token was created in a different Context``
    when a span's __exit__ is called from a different asyncio Task than the one that
    created it (common with async generators).  The span IS recorded; only the cleanup
    token reset fails.  This error is therefore benign.

    Two suppression paths:
      1. The benign text appears directly in the log message (Langfuse path).
      2. The OTel SDK logs ``"Failed to detach context"`` with ``exc_info=True``
         where the attached exception IS the benign ValueError.  In this case the
         benign text is in exc_info, not the message body.

    Both checks require the logger namespace to start with "opentelemetry" or
    "langfuse" so records from unrelated loggers are never suppressed.

    This filter is attached both to the application logger's handlers AND directly
    to the ``opentelemetry`` and ``langfuse`` logger instances so that records are
    suppressed before they propagate to the root logger (and uvicorn's handlers).
    """

    _BENIGN_MSG = "Token was created in a different Context"
    _DETACH_MSG = "Failed to detach context"
    _NAMESPACES = ("opentelemetry", "langfuse")

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith(self._NAMESPACES):
            return True
        msg = record.getMessage()
        # Path 1: benign text is in the message body (Langfuse path).
        if self._BENIGN_MSG in msg:
            return False
        # Path 2: OTel SDK logs "Failed to detach context" with the benign
        # ValueError in exc_info.  Check the attached exception.
        if self._DETACH_MSG in msg and record.exc_info:
            exc = record.exc_info[1]
            if isinstance(exc, ValueError) and self._BENIGN_MSG in str(exc):
                return False
        return True


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------

def setup_logging(
    log_dir: str = "logs",
    app_name: str = "kubeintellect",
    log_level: str = "DEBUG",
    log_format: str = "text",
) -> logging.Logger:
    """
    Configure and return the named application logger.

    Args:
        log_dir:    Directory for rotating log files.
        app_name:   Logger name (shared across the process).
        log_level:  Minimum severity — DEBUG / INFO / WARNING / ERROR / CRITICAL.
        log_format: "text" for human-readable output, "json" for structured JSON.

    Returns:
        The configured logger instance.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d")
    log_file = log_path / f"{app_name}_{timestamp}.log"

    numeric_level = getattr(logging, log_level.upper(), logging.DEBUG)

    if log_format.lower() == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(request_id)s] %(name)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    _request_id_filter = RequestIdFilter()
    _otel_filter = _OtelContextCleanupFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_request_id_filter)
    file_handler.addFilter(_otel_filter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_request_id_filter)
    console_handler.addFilter(_otel_filter)

    logger = logging.getLogger(app_name)
    logger.setLevel(numeric_level)

    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        logger.info("Logging system initialized", extra={"log_format": log_format, "log_level": log_level})
        logger.debug("Log file: %s", log_file)

        # Attach the OTel filter directly to the opentelemetry and langfuse loggers so
        # their records are suppressed before propagating to the root logger (and
        # therefore uvicorn's handlers, which are not covered by the filter above).
        # This closes the gap where OTel SDK logs "Failed to detach context" with
        # exc_info=True via opentelemetry.context — those records never reach the
        # kubeintellect handlers, only the root logger's handlers.
        for _noisy_logger_name in ("opentelemetry", "langfuse"):
            logging.getLogger(_noisy_logger_name).addFilter(_otel_filter)

    return logger


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

_SENSITIVE_HEADERS = frozenset(
    ["authorization", "x-api-key", "api-key", "cookie", "set-cookie"]
)


def mask_headers(headers: dict) -> dict:
    """Return a copy of *headers* with sensitive values replaced by [REDACTED]."""
    return {
        k: "[REDACTED]" if k.lower() in _SENSITIVE_HEADERS else v
        for k, v in headers.items()
    }


def log_audit_event(
    logger: logging.Logger,
    action: str,
    *,
    actor: Optional[str] = None,
    resource_kind: Optional[str] = None,
    resource_name: Optional[str] = None,
    namespace: Optional[str] = None,
    outcome: str = "success",
    details: Optional[str] = None,
) -> None:
    """
    Emit a structured audit log entry for a sensitive or state-changing operation.

    Use this for any Kubernetes mutation (delete, scale, restart, apply, RBAC change)
    and for HITL approvals / tool registrations.

    Args:
        logger:        The module logger.
        action:        Short verb_noun label, e.g. "delete_pod", "scale_deployment".
        actor:         User or service account that triggered the action.
        resource_kind: Kubernetes resource kind, e.g. "Pod", "Deployment".
        resource_name: Name of the specific resource.
        namespace:     Kubernetes namespace.
        outcome:       "success" | "failure" | "denied" | "pending".
        details:       Optional freeform note (avoid including secrets).
    """
    extra: dict[str, Any] = {
        "audit": True,
        "action": action,
        "outcome": outcome,
    }
    if actor:
        extra["actor"] = actor
    if resource_kind:
        extra["resource_kind"] = resource_kind
    if resource_name:
        extra["resource_name"] = resource_name
    if namespace:
        extra["namespace"] = namespace
    if details:
        extra["details"] = details

    parts = [f"AUDIT action={action} outcome={outcome}"]
    if actor:
        parts.append(f"actor={actor}")
    if resource_kind:
        parts.append(f"kind={resource_kind}")
    if resource_name:
        parts.append(f"name={resource_name}")
    if namespace:
        parts.append(f"namespace={namespace}")
    if details:
        parts.append(f"details={details!r}")

    logger.info(" ".join(parts), extra=extra)


# ---------------------------------------------------------------------------
# Duration context manager
# ---------------------------------------------------------------------------

@contextmanager
def log_duration(logger: logging.Logger, operation: str, **ctx):
    """
    Context manager that logs how long *operation* took.

    Usage::

        with log_duration(logger, "workflow_execution", conversation_id=cid):
            await run_workflow(...)

    On success logs INFO with duration_ms.
    On exception logs ERROR with duration_ms and re-raises.
    """
    start = time.monotonic()
    try:
        yield
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "%s completed in %dms",
            operation,
            elapsed_ms,
            extra={"operation": operation, "duration_ms": elapsed_ms, **ctx},
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "%s failed after %dms: %s",
            operation,
            elapsed_ms,
            exc,
            exc_info=True,
            extra={"operation": operation, "duration_ms": elapsed_ms, **ctx},
        )
        raise


# ---------------------------------------------------------------------------
# Legacy helpers (kept for backward compatibility)
# ---------------------------------------------------------------------------

def log_function_entry_exit(logger):
    """Decorator to log function entry and exit."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            logger.debug("Entering function: %s", func.__name__)
            try:
                result = func(*args, **kwargs)
                logger.debug("Exiting function: %s", func.__name__)
                return result
            except Exception as e:
                logger.error("Error in function %s: %s", func.__name__, e, exc_info=True)
                raise
        return wrapper
    return decorator


def log_api_request(logger):
    """Decorator to log API requests."""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            request = kwargs.get("request") or (args[0] if args else None)
            if request:
                logger.info("API Request — %s %s", request.method, request.url.path)
            try:
                response = await func(*args, **kwargs)
                status = response.status_code if hasattr(response, "status_code") else "N/A"
                logger.info("API Response — status=%s", status)
                return response
            except Exception as e:
                logger.error("API Error: %s", e, exc_info=True)
                raise
        return wrapper
    return decorator


def log_error(logger, error_msg: str, exc_info=None):
    logger.error(error_msg, exc_info=exc_info)


def log_warning(logger, warning_msg: str):
    logger.warning(warning_msg)


def log_info(logger, info_msg: str):
    logger.info(info_msg)


def log_debug(logger, debug_msg: str):
    logger.debug(debug_msg)
