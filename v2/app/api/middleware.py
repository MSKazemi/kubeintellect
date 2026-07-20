"""HTTP middleware for KubeIntellect."""
from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.utils.logger import get_logger, request_id_var

logger = get_logger(__name__)

# Paths that are too noisy to log at INFO level (health probes, metrics scrapes).
_SILENT_PATHS = {"/healthz", "/metrics"}


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Log every HTTP request with method, path, status code, and duration.

    - Reads X-Request-ID from the incoming request (or generates one).
    - Injects it into the response as X-Request-ID so callers can correlate.
    - Binds it to the request_id_var context so all downstream log lines
      for this request carry the same ID.
    - Skips INFO-level logging for probe/metrics paths to avoid noise.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        token = request_id_var.set(req_id)

        path = request.url.path
        method = request.method
        t0 = time.monotonic()

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.error(
                f"{method} {path} — unhandled exception after {elapsed_ms:.1f}ms",
                exc_info=exc,
                extra={"method": method, "path": path, "duration_ms": round(elapsed_ms, 1)},
            )
            raise
        finally:
            request_id_var.reset(token)

        elapsed_ms = (time.monotonic() - t0) * 1000
        status = response.status_code

        if path not in _SILENT_PATHS:
            log_level = "warning" if status >= 400 else "info"
            getattr(logger, log_level)(
                f"{method} {path} {status} {elapsed_ms:.1f}ms",
                extra={
                    "method": method,
                    "path": path,
                    "status": status,
                    "duration_ms": round(elapsed_ms, 1),
                },
            )

        response.headers["X-Request-ID"] = req_id
        return response
